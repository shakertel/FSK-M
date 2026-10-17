import json, re, time, threading
from google.genai import types

import random

class SearchPaused(RuntimeError):
    """Provider unavailable: keep the current and unstarted jobs pending."""

class PermanentGeminiError(RuntimeError):
    pass

def classify(exc):
    code = getattr(exc, 'code', None)
    text = str(exc).upper()
    if code == 401 or '401' in text or 'UNAUTHENTICATED' in text or 'API_KEY_INVALID' in text:
        return 'AUTH'
    if code == 403 or '403' in text or 'PERMISSION_DENIED' in text:
        return 'PERMISSION'
    if code == 429 or '429' in text or 'RESOURCE_EXHAUSTED' in text:
        return 'QUOTA'
    if code == 404 or '404' in text or 'NOT_FOUND' in text:
        return 'MODEL'
    if code == 400 or 'INVALID_ARGUMENT' in text:
        return 'INPUT'
    if code in (500, 502, 503, 504) or any(t in text for t in ('503', '502', '504', 'UNAVAILABLE', 'INTERNAL_SERVER_ERROR')):
        return 'BUSY'
    if isinstance(exc, (ValueError, json.JSONDecodeError)):
        return 'JSON'
    return 'NETWORK'

def parse_json(txt):
    txt = (txt or '').replace('```json','').replace('```','').strip()
    starts = [i for i in (txt.find('{'), txt.find('[')) if i >= 0]
    if not starts:
        raise ValueError('No JSON start')
    return json.JSONDecoder().raw_decode(txt[min(starts):])[0]

def retry_delay(exc):
    # Prefer the provider's RetryInfo/Retry-After; never retry earlier than requested.
    values = []
    try:
        response = getattr(exc, 'response', None)
        header = response.headers.get('Retry-After') if response is not None else None
        if header:
            try: values.append(float(header))
            except ValueError:
                from email.utils import parsedate_to_datetime
                values.append(max(0.0, parsedate_to_datetime(header).timestamp() - time.time()))
    except Exception:
        pass
    text = str(getattr(exc, 'details', '')) + ' ' + str(exc)
    for pattern in [r"retryDelay[\"']?\s*[:=]\s*[\"']?(\d+(?:\.\d+)?)s", r'retry in\s+(\d+(?:\.\d+)?)\s*s']:
        values.extend(float(v) for v in re.findall(pattern, text, re.I))
    return max(values, default=0.0)

def daily_quota_explicit(exc):
    text = str(exc).lower()
    return any(x in text for x in ('requestsperday', 'tokensperday', 'per_day', 'daily quota', 'per-day'))

class GeminiQueue:
    def __init__(self, configured, clock=None, sleeper=None):
        self.clock = clock or time.monotonic
        self.sleep = sleeper or time.sleep
        self.lock = threading.Lock()
        self.keys = []
        seen = set()
        for number, value in configured:
            if value in seen:
                print(f'Key {number}: duplicate value; only one slot is used.', flush=True)
                continue
            seen.add(value)
            self.keys.append((number, value))
        self.health = {n: {'status':'ready','next_at':0.0,'errors':0,'busy':False} for n,_ in self.keys}
        self.pointer = 0
        self.active = 0
        self.next_start = 0.0
        self.global_until = 0.0
        self.slow_until = 0.0
        self.pause_reason = ''
        self.overload_streak = 0

    def resume(self):
        with self.lock:
            self.pause_reason = ''

    def pause(self, reason):
        with self.lock:
            if not self.pause_reason: self.pause_reason = reason

    def acquire(self, deadline, stage):
        last_log = -1e30
        while True:
            now = self.clock()
            reason = ''
            with self.lock:
                if self.pause_reason:
                    raise SearchPaused(self.pause_reason)
                if now >= deadline:
                    self.pause_reason = 'Waiting/retry budget reached; successful responses saved. Resume later.'
                    raise SearchPaused(self.pause_reason)
                usable = [(n,k) for n,k in self.keys if self.health[n]['status'] not in ('AUTH','PERMISSION','DAILY_QUOTA')]
                if not usable:
                    self.pause_reason = 'No usable key remains: authentication, permission, or explicit daily quota. Review key status.'
                    raise SearchPaused(self.pause_reason)
                limit = 1 if now < self.slow_until else MAX_PARALLEL_REQUESTS
                ready = [(n,k) for n,k in usable if not self.health[n]['busy'] and self.health[n]['next_at'] <= now]
                gate = max(self.global_until, self.next_start)
                if ready and self.active < limit and now >= gate:
                    number, value = ready[self.pointer % len(ready)]
                    self.pointer += 1
                    self.health[number]['busy'] = True
                    self.active += 1
                    self.next_start = now + MIN_REQUEST_START_GAP
                    return number, value
                reason = 'provider backoff' if now < self.global_until else ('key cooldown' if not ready else 'request pacing / active request')
                delay = max(0.0, gate - now)
                if not ready:
                    free_times = [self.health[n]['next_at'] for n,_ in usable if not self.health[n]['busy']]
                    if free_times: delay = max(delay, min(free_times) - now)
            if now - last_log >= 20:
                print(f'⏳ {stage}: waiting ({reason}); queue retained; next check ≤2s; known delay={max(0,delay):.0f}s', flush=True)
                last_log = now
            self.sleep(min(2.0, max(0.05, deadline - now)))

    def release(self, number, category=None, exc=None):
        now = self.clock()
        with self.lock:
            health = self.health[number]
            health['busy'] = False
            self.active = max(0, self.active - 1)
            if category is None:
                health['status'] = 'healthy'
                health['next_at'] = 0.0
                self.overload_streak = 0
                return 0.0
            health['errors'] += 1
            if category in ('AUTH','PERMISSION'):
                health['status'] = category
                return 0.0
            if category == 'QUOTA' and daily_quota_explicit(exc):
                health['status'] = 'DAILY_QUOTA'
                return 0.0
            supplied = retry_delay(exc) if exc is not None else 0.0
            if category == 'QUOTA':
                wait = max(supplied, 60.0 * 2 ** min(health['errors'] - 1, 2))
            elif category == 'BUSY':
                self.overload_streak += 1
                wait = max(supplied, min(120.0, 20.0 * 2 ** min(self.overload_streak - 1, 3)))
            else:
                wait = max(supplied, min(30.0, 2.0 * 2 ** min(health['errors'] - 1, 4)))
            wait += random.uniform(0.0, 2.0)
            health['status'] = 'cooldown'
            health['next_at'] = now + wait
            if category in ('QUOTA','BUSY'):
                # Shared conservative gate also helps when several keys share a project.
                self.global_until = max(self.global_until, now + wait)
                self.slow_until = max(self.slow_until, now + wait + 120.0)
            return wait

    def public_status(self):
        with self.lock:
            now = self.clock()
            return {str(n): {'status':h['status'], 'errors':h['errors'],
                             'cooldown_seconds':round(max(0,h['next_at']-now),1)}
                    for n,h in self.health.items()}

# Two live calls may run concurrently, each using a distinct key.
ATTEMPT_GAP_SECONDS = 0.0

class SharedRoundQueue(GeminiQueue):
    def __init__(self, configured, clock=None, sleeper=None):
        super().__init__(configured, clock, sleeper)
        self.failed_this_round = set()
        self.round_number = 1
        self.round_due = None
        self.round_backoff = 5.0

    def acquire(self, deadline, stage):
        last_log = -1e30
        while True:
            now = self.clock()
            with self.lock:
                if self.pause_reason:
                    raise SearchPaused(self.pause_reason)
                if now >= deadline:
                    raise SearchPaused('Explicit deadline reached')
                usable = [(i,n,k) for i,(n,k) in enumerate(self.keys)
                          if self.health[n]['status'] not in ('AUTH','PERMISSION')]
                if not usable:
                    self.pause_reason = 'No usable key: authentication/access errors.'
                    raise SearchPaused(self.pause_reason)
                if self.round_due is not None and now >= self.round_due:
                    self.failed_this_round.clear()
                    self.round_number += 1
                    self.round_due = None
                    self.pointer = 0
                remaining = [(i,n,k) for i,n,k in usable if n not in self.failed_this_round]
                if not remaining and self.active == 0 and self.round_due is None:
                    wait = self.round_backoff
                    self.round_due = now + wait
                    self.round_backoff = min(60.0, wait*2)
                    print(f'⏳ All usable keys failed in round {self.round_number}; retry in {wait:.0f}s',flush=True)
                ready = [(i,n,k) for i,n,k in remaining
                         if not self.health[n]['busy'] and self.health[n]['next_at'] <= now]
                if (self.round_due is None and self.active < 2 and ready and now >= self.next_start):
                    i,number,key = min(ready,key=lambda item:(item[0]-self.pointer)%len(self.keys))
                    self.pointer = (i+1)%len(self.keys)
                    self.health[number]['busy'] = True
                    self.active += 1
                    return number,key
                if now-last_log >= 20:
                    print(f'⏳ {stage}: waiting for active attempt / provider retry',flush=True)
                    last_log = now
            self.sleep(min(.25,max(.01,deadline-now)))

    def release(self, number, category=None, exc=None):
        now = self.clock()
        with self.lock:
            health = self.health[number]
            health['busy'] = False
            self.active = max(0,self.active-1)
            # No artificial delay on healthy responses; provider retry delays remain.
            self.next_start = max(self.next_start,now+ATTEMPT_GAP_SECONDS)
            if category is None:
                health['status'] = 'healthy'
                health['next_at'] = 0.0
                self.round_backoff = 5.0
                return ATTEMPT_GAP_SECONDS
            health['errors'] += 1
            self.failed_this_round.add(number)
            if category in ('AUTH','PERMISSION'):
                health['status'] = category
                return ATTEMPT_GAP_SECONDS
            wait = max(0.0,retry_delay(exc)) if exc is not None else 0.0
            health['status'] = 'cooldown'
            health['next_at'] = now+wait
            return max(wait,ATTEMPT_GAP_SECONDS)
