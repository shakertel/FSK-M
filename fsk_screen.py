import json, time, threading

from concurrent.futures import ThreadPoolExecutor

def unique_source_key(item):
    """
    Remove cloned versions of the same original title.
    """

    source_id = item.get("source_id")

    if source_id is not None:
        return str(source_id)

    return str(
        item.get("source_title")
        or item.get("title")
        or ""
    ).casefold().strip()

def candidate_score(item):
    """
    New FSK ranking:
    70 mood / 30 story
    OR 100 mood if no story was requested.
    """

    mood = float(item.get("mood_score", 0) or 0)
    story = float(item.get("story_score", 0) or 0)

    if story_requested:
        return round(
            min(70, max(0, mood))
            + min(30, max(0, story)),
            2
        )

    return round(
        min(100, max(0, mood)),
        2
    )

def candidate_sort_key(item):
    """
    Total relevance first.
    Audience experience breaks ties.
    Star is NOT an automatic ranking boost.
    """

    return (
        candidate_score(item),
        float(item.get("mood_score", 0) or 0),
        float(item.get("story_score", 0) or 0)
    )

def run_full(size, compact=False):
    QUEUE.resume()
    jobs = [(i, simulated[i:i+size]) for i in range(0, len(simulated), size)]
    lanes = [jobs[i::ACTIVE_LANES] for i in range(ACTIVE_LANES)]
    started = time.monotonic()
    guard = threading.Lock()
    stop = threading.Event()
    state = {offset:{'offset':offset,'size':len(batch),'status':'pending'} for offset,batch in jobs}
    completed = 0
    batch_items = {}
    failures = []
    stop_reasons = []

    def save_state():
        (OUTPUT_DIR/'screening_state.json').write_text(json.dumps(
            {'records':len(simulated),'batch_size':size,'query':SAMPLE_QUERY,
             'batches':list(state.values()),'stop_reasons':stop_reasons},
            ensure_ascii=False,indent=2),encoding='utf-8')

    print(f'🚀 REAL FILMS: {len(simulated):,} | batch={size} | batches={len(jobs)} | lanes={ACTIVE_LANES}',flush=True)
    print('Scoring:', '70 mood / 30 story' if story_requested else '100 mood / 0 story')
    print('Successful responses are reused from cache; provider pauses keep jobs pending.')
    save_state()

    def worker(lane_no, lane_jobs):
        nonlocal completed
        for offset,batch in lane_jobs:
            if stop.is_set(): break
            begin = time.monotonic()
            with guard:
                state[offset]['status']='running'
                save_state()
            try:
                raw = gemini_request(batch_prompt(batch,SAMPLE_QUERY,intent,compact=compact), 'candidate',len(batch))
                got = normalized_candidates(raw)
            except (SearchPaused,PermanentGeminiError) as exc:
                stop.set()
                with guard:
                    # This job was not successfully screened. Do not falsely mark it failed.
                    state[offset]['status']='pending'
                    state[offset]['pause_reason']=str(exc)
                    if str(exc) not in stop_reasons: stop_reasons.append(str(exc))
                    save_state()
                print(f'⏸️ LANE {lane_no+1} | offset={offset} | paused; pending work saved',flush=True)
                break
            except Exception as exc:
                duration=round(time.monotonic()-begin,2)
                with guard:
                    state[offset].update(status='failed',error=type(exc).__name__,seconds=duration)
                    failures.append({'offset':offset,'size':len(batch),'lane':lane_no,'error':type(exc).__name__})
                    save_state()
                print(f'❌ LANE {lane_no+1} | offset={offset} | actual processing error: {type(exc).__name__}',flush=True)
            else:
                duration=round(time.monotonic()-begin,2)
                with guard:
                    batch_items[offset] = got
                    completed+=1
                    state[offset].update(status='complete',seconds=duration,candidates=len(got))
                    save_state()
                    done=completed
                print(f'✅ [{done}/{len(jobs)}] LANE {lane_no+1} | offset={offset} | {duration}s | candidates={len(got)}',flush=True)

    try:
        with ThreadPoolExecutor(max_workers=ACTIVE_LANES) as executor:
            futures=[executor.submit(worker,i,lane) for i,lane in enumerate(lanes)]
            try:
                for future in futures: future.result()
            except KeyboardInterrupt:
                stop.set()
                QUEUE.pause('Stopped by user; successful responses are cached.')
                raise
    except KeyboardInterrupt:
        stop.set()
        QUEUE.pause('Stopped by user; successful responses are cached.')
        for value in state.values():
            if value['status']=='running':value['status']='pending'
        save_state()
        raise
    # Preserve original lane order so equal-score ties and cached judge groups are stable.
    all_items = [item for lane in lanes for offset,_ in lane for item in batch_items.get(offset,[])]
    unique={}
    for item in all_items:
        key=unique_source_key(item)
        if key and (key not in unique or candidate_sort_key(item)>candidate_sort_key(unique[key])):
            unique[key]=item
    top=sorted(unique.values(),key=candidate_sort_key,reverse=True)[:FINAL_CANDIDATE_LIMIT]
    pending=[value for value in state.values() if value['status']!='complete' and value['status']!='failed']
    report={'batch_size':size,'compact':compact,'records':len(simulated),'batch_count':len(jobs),
        'successful_batches':completed,'failed_batches':len(failures),'pending_batches':len(pending),
        'screened_records':sum(v['size'] for v in state.values() if v['status']=='complete'),
        'complete':completed==len(jobs),'raw_candidates':len(all_items),'unique_candidates':len(unique),
        'final_candidates':len(top),'starred_candidates':sum(x.get('star') is True for x in top),
        'seconds':round(time.monotonic()-started,2),'stop_reasons':stop_reasons,
        'scoring':'70_mood_30_story' if story_requested else '100_mood'}
    # Compatibility with the final judge; pending jobs remain retryable, distinctly tagged.
    retry_jobs=failures+[{'offset':x['offset'],'size':x['size'],'error':'pending_provider_pause','status':'pending'} for x in pending]
    (OUTPUT_DIR/'screening_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return top,report,retry_jobs
