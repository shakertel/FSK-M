"""Movie search adapter. Notebook prompts and scoring live in fsk_*.py unchanged."""
import hashlib
import json
import os
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from supabase import create_client
from google import genai
from google.genai import types
from fsk_queue import SharedRoundQueue, SearchPaused, PermanentGeminiError, classify, parse_json

MODEL = 'gemini-3.5-flash-lite'
BATCH_SIZE = 400
BASE = Path(__file__).resolve().parent

class SearchCancelled(SearchPaused):
    pass

class SearchIncomplete(RuntimeError):
    pass

def write_json(path, value):
    temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temp.replace(path)

class FSKEngine:
    def __init__(self, sb=None, configured_keys=None, data_dir=None, queue_factory=None, wait_fn=None):
        self.keys = configured_keys if configured_keys is not None else [
            (i, os.environ[f'GEMINI_KEY_{i}']) for i in range(1,15) if os.getenv(f'GEMINI_KEY_{i}')]
        if not self.keys:
            raise RuntimeError('GEMINI_KEY_1 ... GEMINI_KEY_14 missing')
        self.root = Path(data_dir or os.getenv('DATA_DIR', './data')).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # One GPU-free search at a time, two API lanes within that search.
        self.search_lock = threading.Lock()
        self.queue_factory = queue_factory or SharedRoundQueue
        self.wait_fn = wait_fn
        self.next_api_start = 0.0
        loader = dict(sb=sb if sb is not None else create_client(
            os.environ['SUPABASE_URL'], os.environ['SUPABASE_SECRET_KEY']),
            defaultdict=defaultdict, Counter=Counter, hashlib=hashlib, json=json)
        exec(compile((BASE/'fsk_dataset.py').read_text(), 'fsk_dataset.py', 'exec'), loader)
        self.data = {k:loader[k] for k in ('movies','movie_lookup','simulated','by_sim_title','DATASET_SHA')}
        self.movies = self.data['movies']
        self.dataset_sha = self.data['DATASET_SHA']
        print(f'FSK M READY | movies={len(self.movies)} | batch=400 | logical_lanes=2 | live_api=1 | gap=3s', flush=True)

    def search(self, user_query, cancel_event=None, progress=None):
        cancel_event = cancel_event or threading.Event()
        started = time.monotonic()
        deadline = float('inf')
        def check():
            if cancel_event.is_set():
                raise SearchCancelled('Search cancelled')
        acquired = False
        while not acquired:
            check()
            acquired = self.search_lock.acquire(timeout=0.2)
        try:
            return self._search(str(user_query).strip(), check, cancel_event, progress, started, deadline)
        finally:
            self.search_lock.release()

    def _search(self, query, check, cancel_event, progress, started, deadline):
        check()
        context = {'project':'FSK M', 'query':query, 'model':MODEL, 'dataset_sha':self.dataset_sha, 'batch_size':400}
        fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        run = self.root/'runs'/fingerprint[:20]/uuid.uuid4().hex[:12]
        run.mkdir(parents=True)
        cache = self.root/'gemini_cache'
        cache.mkdir(exist_ok=True)
        write_json(run/'context.json', context)
        ns = dict(self.data, MODEL=MODEL, SAMPLE_QUERY=query, OUTPUT_DIR=run,
                  ACTIVE_LANES=2, FINAL_CANDIDATE_LIMIT=100, ThreadPoolExecutor=ThreadPoolExecutor,
                  SearchPaused=SearchPaused, PermanentGeminiError=PermanentGeminiError)
        queue = self.queue_factory(self.keys)
        queue.next_start = self.next_api_start
        def release_attempt(number, category=None, exc=None):
            wait = queue.release(number, category, exc)
            self.next_api_start = queue.next_start
            return wait
        def cancellable_sleep(seconds):
            check()
            if self.wait_fn is None:
                cancel_event.wait(seconds)
            else:
                self.wait_fn(cancel_event, seconds)
            check()
        queue.sleep = cancellable_sleep
        ns['QUEUE'] = queue
        calls = []
        api_lock = threading.Lock()
        hits = [0]
        def snapshot():
            with api_lock:
                write_json(run/'api_calls.json', calls)
                write_json(run/'key_status.json', queue.public_status())
        def notify(stage):
            if progress:
                progress(stage)
        def validate(result, stage):
            if stage == 'intent':
                valid = isinstance(result, dict)
            elif stage == 'candidate':
                valid = isinstance(result, list) or (isinstance(result, dict) and
                    isinstance(result.get('results', result.get('candidates')), list))
            else:
                valid = isinstance(result, list) or (isinstance(result, dict) and
                    isinstance(result.get('results'), list))
            if not valid:
                raise ValueError('Unexpected JSON structure for '+stage)
            return result
        def response_complete(prompt, stage, result):
            if not stage.startswith('final_judge'):
                return True
            items = json.loads(prompt.rsplit('CANDIDATES\n----------------------------------------\n\n',1)[1])
            allowed = {x['title'] for x in items}
            verdicts = result.get('results',[]) if isinstance(result,dict) else result
            found = {r.get('title') for r in verdicts if isinstance(r,dict)
                     and r.get('match_level') in ('strong','partial','unsuitable')}
            return allowed <= found
        def request(prompt, stage, batch_size=0):
            check()
            signature = hashlib.sha256((MODEL+'\0'+prompt).encode()).hexdigest()
            path = cache/(signature+'.json')
            if path.exists():
                try:
                    result = validate(json.loads(path.read_text(encoding='utf-8')), stage)
                    if not response_complete(prompt,stage,result):
                        raise ValueError('Incomplete cached judgment; retry live')
                    with api_lock: hits[0] += 1
                    print(f'CACHE | {stage} | rows={batch_size}', flush=True)
                    return result
                except (OSError, ValueError):
                    pass
            notify(stage)
            while True:
                check()
                number, key = queue.acquire(deadline, stage)
                client = None
                begin = time.monotonic()
                released = False
                try:
                    timeout_ms = 120000  # Per-attempt network timeout; retries have no total time/count cap.
                    client = genai.Client(api_key=key, http_options={
                        'timeout':timeout_ms, 'retry_options':{'attempts':1}})
                    response = client.models.generate_content(model=MODEL, contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type='application/json', temperature=0.1))
                    result = validate(parse_json(response.text), stage)
                except Exception as exc:
                    category = classify(exc)
                    wait = release_attempt(number, category, exc)
                    released = True
                    with api_lock:
                        calls.append({'stage':stage,'key':number,'batch_size':batch_size,'success':False,
                                      'error':category,'seconds':round(time.monotonic()-begin,2)})
                    snapshot()
                    print(f'API | {stage} | KEY {number} | {category} | retry_after={wait:.1f}s', flush=True)
                    check()
                    if category in ('MODEL','INPUT'):
                        queue.pause(category + ': check model/request configuration')
                        raise PermanentGeminiError(queue.pause_reason) from None
                else:
                    release_attempt(number)
                    released = True
                    usage = getattr(response, 'usage_metadata', None)
                    with api_lock:
                        calls.append({'stage':stage,'key':number,'batch_size':batch_size,'success':True,
                            'seconds':round(time.monotonic()-begin,2),
                            'input_tokens':getattr(usage,'prompt_token_count',None),
                            'output_tokens':getattr(usage,'candidates_token_count',None)})
                    # Save even if cancelled while an HTTP request was in flight.
                    if response_complete(prompt,stage,result):
                        write_json(path, result)
                    snapshot()
                    check()
                    return result
                finally:
                    if not released:
                        release_attempt(number)
                    if client is not None:
                        try: client.close()
                        except Exception: pass
        ns['gemini_request'] = request
        exec(compile((BASE/'fsk_logic.py').read_text(), 'fsk_logic.py', 'exec'), ns)
        notify('intent')
        raw = request(ns['INTENT_PROMPT'].format(query=query), 'intent', 0)
        if not isinstance(raw, dict):
            raise SearchIncomplete('Intent response is not a JSON object')
        ns['intent'] = ns['normalize_intent'](raw, query)
        ns['story_requested'] = ns['intent']['story_requested']
        write_json(run/'intent.json', ns['intent'])
        exec(compile((BASE/'fsk_screen.py').read_text(), 'fsk_screen.py', 'exec'), ns)
        top, report, failures = ns['run_full'](400, compact=False)
        check()
        write_json(run/'candidates_400_mood70.json', top)
        write_json(run/'failures_400_mood70.json', failures)
        if not report['complete']:
            raise SearchIncomplete(f"Incomplete screening: {report['successful_batches']}/{report['batch_count']}; retry uses cached successes")
        ns.update(full_results={400:top}, full_reports=[report], report=report)
        notify('final_judge')
        while True:
            exec(compile((BASE/'fsk_judge.py').read_text(), 'fsk_judge.py', 'exec'), ns)
            check()
            if ns['complete']:
                break
            if queue.pause_reason:
                raise SearchIncomplete(queue.pause_reason)
            print(f"Final judgments missing: {len(ns['missing_titles'])}; automatic recovery continues",flush=True)
            notify('final_judge')
            cancellable_sleep(3.0)
        fields = ('year','countries','genres','poster_url','imdb_rating','description','actors',
                  'directors_creators','runtime_minutes','digimoviez_url','imdb_url')
        results = []
        for row in ns['final_results']:
            db = self.data['movie_lookup'][str(row['source_id'])]
            results.append({**row, **{f:db.get(f) for f in fields}})
        final_context = {**context, 'fingerprint':fingerprint, 'complete':True,
                         'seconds':round(time.monotonic()-started,2),'cache_hits':hits[0],
                         'results':len(results),'api_attempts':len(calls)}
        write_json(run/'final_context.json', final_context)
        write_json(run/'results.json', results)
        print('SEARCH COMPLETE | '+json.dumps(final_context,ensure_ascii=False),flush=True)
        notify('complete')
        return results
