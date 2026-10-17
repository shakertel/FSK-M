"""Original Telegram shell + the user's working movie Colab search. No response cache."""
import os, json, threading, time, re, random
from collections import defaultdict, Counter
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from supabase import create_client
from google import genai
from google.genai import types

MODEL = 'gemini-3.5-flash-lite'
BATCH_SIZE = 400
MAX_PER_BATCH = 10
FINAL_CANDIDATE_LIMIT = 100
MAX_PARALLEL_REQUESTS = 2
MAX_WAIT_PER_CALL_SECONDS = 900
API_TIMEOUT_MS = 120000

class SearchCancelled(Exception):
    pass



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

    def public_status(self):
            with self.lock:
                now = self.clock()
                return {str(n): {'status':h['status'], 'errors':h['errors'],
                                 'cooldown_seconds':round(max(0,h['next_at']-now),1)}
                        for n,h in self.health.items()}

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
                    self.pause_reason = (
                        "Retry time budget reached; "
                        "unfinished work remains pending."
                    )
                    raise SearchPaused(self.pause_reason)

                usable = [
                    (n, k) for n, k in self.keys
                    if self.health[n]["status"]
                    not in ("AUTH", "PERMISSION")
                ]

                if not usable:
                    self.pause_reason = (
                        "No usable key: authentication/access errors."
                    )
                    raise SearchPaused(self.pause_reason)

                # پایان مکث: دور جدید از ابتدای کلیدها
                if self.round_due is not None and now >= self.round_due:
                    self.failed_this_round.clear()
                    self.round_number += 1
                    self.round_due = None

                remaining = [
                    (n, k) for n, k in usable
                    if n not in self.failed_this_round
                ]

                ready = [
                    (n, k) for n, k in remaining
                    if not self.health[n]["busy"]
                    and self.health[n]["next_at"] <= now
                ]

                # هر دو لاین از همین استخر مشترک استفاده می‌کنند.
                # یک کلید هم‌زمان به دو درخواست اختصاص داده نمی‌شود.
                if (
                    self.round_due is None
                    and ready
                    and self.active < MAX_PARALLEL_REQUESTS
                ):
                    number, api_key = ready[0]
                    self.health[number]["busy"] = True
                    self.active += 1
                    return number, api_key

                # فقط وقتی همه خطا داده‌اند و تماس فعالی باقی نمانده:
                if (
                    not remaining
                    and self.active == 0
                    and self.round_due is None
                ):
                    wait = self.round_backoff
                    self.round_due = now + wait
                    self.round_backoff = min(60.0, wait * 2)

                    print(
                        f"⏳ همه کلیدهای قابل‌استفاده در دور "
                        f"{self.round_number} خطا دادند؛ "
                        f"شروع دوباره پس از {wait:.0f} ثانیه",
                        flush=True
                    )

                if self.round_due is not None:
                    reason = (
                        f"مکث بین دورها: "
                        f"{max(0, self.round_due - now):.0f}s"
                    )
                elif remaining and not ready:
                    reason = "انتظار تماس فعال یا Retry-After خود سرویس"
                else:
                    reason = "انتظار پایان تماس فعال"

            if now - last_log >= 20:
                print(
                    f"⏳ {stage}: {reason}؛ صف محفوظ است",
                    flush=True
                )
                last_log = now

            self.sleep(min(0.5, max(0.01, deadline - now)))

    def release(self, number, category=None, exc=None):
        now = self.clock()

        with self.lock:
            health = self.health[number]
            health["busy"] = False
            self.active = max(0, self.active - 1)

            if category is None:
                # کلید موفق همچنان در دسترس می‌ماند.
                health["status"] = "healthy"
                health["next_at"] = 0.0
                self.round_backoff = 5.0
                return 0.0

            health["errors"] += 1
            self.failed_this_round.add(number)

            if category in ("AUTH", "PERMISSION"):
                health["status"] = category
                return 0.0

            # انتظار ثابت قبلیِ ۶۰/۱۲۰ ثانیه حذف شده.
            # فقط زمان صریح اعلام‌شده سرویس برای همین کلید حفظ می‌شود.
            wait = (
                max(0.0, retry_delay(exc))
                if exc is not None else 0.0
            )
            health["status"] = "cooldown"
            health["next_at"] = now + wait
            return wait

INTENT_PROMPT = """
تو یک سیستم تحلیل نیاز کاربر برای پیشنهاد فیلم و سریال هستی.

جمله کاربر:
{query}

وظیفه تو فهمیدن «تجربه انسانی» و تجربه تماشایی
موردنظر کاربر است، نه فقط تبدیل کلمات به ژانر.

خیلی مهم:
اگر کاربر می‌گوید «من مریضم»، معنی آن این نیست
که سریال بیمارستانی یا پزشکی می‌خواهد.

بیماری کاربر ≠ ژانر پزشکی
فوت عزیز کاربر ≠ سریال جنایی

مثال:
برای «من مریضم و حالم خوب نیست»،
The Big C می‌تواند تطابق مستقیم تجربه انسانی باشد،
اما House M.D. یا The Good Doctor صرفاً
به دلیل پزشکی بودن تطابق مستقیم محسوب نمی‌شوند.

دو بخش را جداگانه استخراج کن:

1. حس‌وحال و تجربه تماشایی موردنظر:
امید، آرامش، شادی، طنز، اندوه، تنش، هیجان،
دلگرمی، همدلی، انگیزه، سبک یا سنگین بودن.

ترجیحات را فقط وقتی استخراج کن که
در درخواست کاربر شاهد دارند.

2. تجربه انسانی یا داستان مشخص:

اگر کاربر تجربه شخصی بیان کرده،
آن را با موضوع سطحی یا ژانر اشتباه نگیر.

اگر کاربر موضوع داستانی مشخصی درخواست کرده،
آن را دقیق استخراج کن.

اگر داستان خاصی نخواسته است،
story_requested باید false باشد.

خواسته‌های منفی و ممنوعیت‌ها را نیز استخراج کن.

هیچ ترجیح یا داستانی از خودت نساز.

خروجی فقط JSON:

{{
  "mood_requirements": [],
  "mood_exclusions": [],
  "story_requested": false,
  "story_requirements": [],
  "story_exclusions": [],
  "hard_constraints": [],
  "soft_preferences": [],
  "user_experience": "",
  "summary": ""
}}
"""

def normalize_intent(raw, query=""):

    if not isinstance(raw, dict):
        raw = {}

    def as_list(value):
        if isinstance(value, list):
            return [str(v) for v in value if v]
        if isinstance(value, str) and value.strip():
            return [value]
        return []

    story = as_list(raw.get("story_requirements"))

    requested = raw.get("story_requested")

    if not isinstance(requested, bool):
        requested = bool(story)

    return {
        "mood_requirements": as_list(
            raw.get("mood_requirements")
        ),
        "mood_exclusions": as_list(
            raw.get("mood_exclusions")
        ),
        "story_requested": requested,
        "story_requirements": story,
        "story_exclusions": as_list(
            raw.get("story_exclusions")
        ),
        "hard_constraints": as_list(
            raw.get("hard_constraints")
        ),
        "soft_preferences": as_list(
            raw.get("soft_preferences")
        ),
        "user_experience": str(
            raw.get("user_experience") or ""
        ),
        "summary": str(raw.get("summary") or ""),
        "original_query": query
    }

def make_payload(batch, compact=False):

    if compact:
        return [
            [
                s["sid"],
                s["title"],
                s.get("description"),
                s.get("recommendation_tags"),
                s.get("concepts"),
                s.get("special_phrases") or []
            ]
            for s in batch
        ]

    return [
        {
            "id": s["sid"],
            "title": s["title"],
            "description": s.get("description"),
            "recommendation_tags":
                s.get("recommendation_tags"),
            "imdb_rating": s.get("imdb_rating"),
            "concepts": s.get("concepts"),
            "special_phrases": s.get("special_phrases") or []
        }
        for s in batch
    ]

def batch_prompt(batch, query, intent, compact=False):

    parsed = normalize_intent(intent, query)
    story_requested = parsed["story_requested"]

    if story_requested:
        scoring = """
امتیاز:
mood_score حداکثر 70
story_score حداکثر 30

mood_score:
تطابق تجربه تماشایی با concepts و
recommendation_tags استخراج‌شده از کامنت‌ها.

story_score:
تطابق داستان و موقعیت‌های روایی با
داستان مشخصی که کاربر درخواست کرده است.
"""
        star_rule = """
star فقط وقتی true است که
هم حس‌وحال اثر طبق Concepts و Tags
و هم موقعیت داستانی آن به شکل مستقیم
با خواسته‌های کاربر تطابق قوی داشته باشند.
"""
    else:
        scoring = """
کاربر داستان مشخصی درخواست نکرده است.

mood_score حداکثر 100
story_score دقیقاً 0

تمام امتیاز بر اساس تطابق حس‌وحال و
تجربه تماشایی با Concepts و Tags است.
"""
        star_rule = """
star وقتی true است که تجربه تماشایی
اثر براساس Concepts و Tags تطابق
بسیار قوی با خواسته کاربر داشته باشد.
نبود درخواست داستانی نباید جریمه شود.
"""

    shape = (
        "هر ردیف به ترتیب "
        "[id,title,description,recommendation_tags,concepts,special_phrases] است."
        if compact else
        "DATA مجموعه‌ای از اشیای JSON است."
    )

    return f"""
کاربر:
{query}

Intent:
{json.dumps(parsed, ensure_ascii=False)}

از بین داده‌ها حداکثر {MAX_PER_BATCH}
فیلم یا سریال مناسب انتخاب کن.

قانون مهم FSK:

این موتور بر اساس تحلیل تجربه واقعی
تماشاگران از کامنت‌ها ساخته شده است.

recommendation_tags و concepts
حاوی گلواژه‌ها و مفاهیم استخراج‌شده
از نظرات مخاطبان هستند.

description خلاصه داستان است.

special_phrases عبارت‌های کوتاه عیناً استخراج‌شده از کامنت‌های همان اثر هستند.
آن‌ها را همراه concepts و recommendation_tags مرور کن و در همان معیار تجربه مخاطبان لحاظ کن.
برای عبارت‌ها جهت مثبت یا منفی از پیش فرض نکن؛ معنا، کنایه، نفی و ارتباط با خود اثر را بررسی کن.
درخواست دانلود، مسائل سایت و تعریف یا نقد فیلم دیگر را شاهد تجربه همین اثر حساب نکن.
وجود یک عبارت به‌تنهایی نظر غالب مخاطبان را ثابت نمی‌کند.

اول تجربه تماشایی و حس‌وحال درخواست
کاربر را با مفاهیم کامنت‌ها تطبیق بده.

سپس، اگر کاربر داستان خاصی خواسته،
داستان را نیز بررسی کن.

تطابق معنایی اهمیت دارد،
نه صرفاً تطابق کلمات یا ژانر.

مثال:
اگر کاربر در شرایط دشوار به دنبال امید است،
وجود مفاهیمی مانند امیدبخش، دلگرم‌کننده،
انرژی‌بخش یا مناسب روزهای سخت می‌تواند
شاهدی مرتبط باشد، مشروط بر اینکه واقعاً
در داده‌های همان اثر آمده باشد.

فقط خلاصه داستان را معیار قرار نده.

{scoring}

قانون بسیار مهم ⭐:

{star_rule}

صرفاً حال‌خوب‌کن بودن برای ستاره کافی نیست
اگر کاربر علاوه بر حس‌وحال، داستان مشخصی
هم درخواست کرده باشد.

IMDb در امتیاز دخالت نکند.

خواسته‌های منفی کاربر را جدی بگیر.

فقط عنوان موجود در DATA را خروجی بده.
شناسه SIM را دقیقاً حفظ کن.

برای هر اثر:

reason:
با زبان طبیعی توضیح بده چرا برای این
کاربر مناسب است.

وقتی Concepts و Tags پشتوانه دارند،
به تجربه مخاطبان اشاره کن.

مثلاً:
«در مفاهیم استخراج‌شده از نظرات،
این اثر حال‌خوب‌کن و امیدبخش توصیف شده...»

اما این فقط مثال است.
هر ادعایی باید با داده همان اثر سازگار باشد.

اگر شواهد کامنتی وجود ندارند،
آن‌ها را اختراع نکن.

audience_evidence:
تا چهار برچسب، مفهوم یا عبارت ویژه مرتبط را
عیناً از داده‌های همان اثر بیاور.

story_evidence:
اگر داستان مرتبطی وجود دارد،
دلیل داستانی را کوتاه توضیح بده.

star_reason:
در صورت ستاره‌دار بودن، دلیل تطابق
همه ابعاد درخواست‌شده را بنویس.

از بین آثار مناسب، بهترین‌ها را انتخاب کن.
به آثار کمترمشهور اما دارای شواهد معنایی
قوی نیز توجه کن.

خروجی فقط JSON Array:

[
  {{
    "title": "",
    "mood_score": 0,
    "story_score": 0,
    "star": false,
    "star_reason": "",
    "reason": "",
    "audience_evidence": [],
    "story_evidence": ""
  }}
]

{shape}

DATA:
{json.dumps(
    make_payload(batch, compact),
    ensure_ascii=False,
    separators=(",", ":")
)}
"""

def _clamp(value, maximum):
    try:
        return max(
            0.0,
            min(float(maximum), float(value or 0))
        )
    except (TypeError, ValueError):
        return 0.0

def normalized_candidates(got, by_sim_title, intent, SAMPLE_QUERY):

    if isinstance(got, dict):
        got = got.get(
            "results",
            got.get("candidates", [])
        )

    if not isinstance(got, list):
        return []

    current_intent = normalize_intent(
        intent,
        SAMPLE_QUERY
    )

    story_requested = current_intent["story_requested"]

    mood_max = 70 if story_requested else 100
    story_max = 30 if story_requested else 0

    output = []
    seen = set()

    for item in got:

        if not isinstance(item, dict):
            continue

        title = item.get("title")

        if title not in by_sim_title:
            continue

        if title in seen:
            continue

        seen.add(title)

        meta = by_sim_title[title]

        mood = _clamp(
            item.get("mood_score"),
            mood_max
        )

        story = _clamp(
            item.get("story_score"),
            story_max
        )

        evidence = item.get("audience_evidence") or []

        if not isinstance(evidence, list):
            evidence = []

        result = dict(item)

        result.update({
            "sid": meta["sid"],
            "source_id": meta["source_id"],
            "source_title": meta["source_title"],

            "mood_score": mood,
            "story_score": story,
            "total_score": round(mood + story, 2),

            # Compatibility with old notebook functions
            "experience_score": mood,
            "preference_score": story,

            "star": item.get("star") is True,
            "star_reason": str(
                item.get("star_reason") or ""
            ),
            "reason": str(item.get("reason") or ""),
            "audience_evidence": evidence,
            "story_evidence": str(
                item.get("story_evidence") or ""
            )
        })

        output.append(result)

    return output

def fsk_score(value, maximum):
    try:
        return round(
            max(0, min(float(maximum), float(value or 0))),
            2
        )
    except (TypeError, ValueError):
        return 0.0

def fsk_text(value):
    return str(value or "").strip()

def fsk_list(value):
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []

def fsk_chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]

def build_final_prompt(items, SAMPLE_QUERY, intent, MOOD_MAX, STORY_MAX):

    return f"""
تو داور نهایی موتور پیشنهاد فیلم FSK هستی.

در این سیستم، آثار از میان پایگاه بزرگی
از فیلم‌ها و سریال‌ها انتخاب شده‌اند.

مبنای اصلی پیشنهاد، تجربه مخاطبان است
که قبلاً از نظرات واقعی آن‌ها به شکل
Concepts و Recommendation Tags استخراج شده.

----------------------------------------
USER REQUEST
----------------------------------------

{SAMPLE_QUERY}

----------------------------------------
INTENT
----------------------------------------

{json.dumps(intent, ensure_ascii=False)}

----------------------------------------
YOUR JOB
----------------------------------------

برای تک‌تک کاندیدهای ورودی:

1. تطابق واقعی آن‌ها با درخواست را بررسی کن.

2. تصمیم قبلی را کورکورانه تأیید نکن.

3. تجربه مخاطبان و داستان را جداگانه در نظر بگیر.

4. دلیل پیشنهاد را بررسی و در صورت نیاز
   طبیعی‌تر و دقیق‌تر بازنویسی کن.

5. تصمیم مستقل درباره ستاره بگیر.

6. هیچ کاندیدی را از پاسخ حذف نکن.
   درباره همه آن‌ها تصمیم ثبت کن.

----------------------------------------
SCORING
----------------------------------------

این آثار قبلاً امتیازدهی شده‌اند.

وزن فعلی:

Audience / Mood: {MOOD_MAX}
Story: {STORY_MAX}

در این مرحله امتیاز جدید نساز.
امتیازها در پایتون نگهداری می‌شوند.

وظیفه اصلی تو بررسی کیفیت،
تناسب، هشدارها و ستاره است.

----------------------------------------
VERY IMPORTANT: NUANCED JUDGMENT
----------------------------------------

فقط سه حالت داریم:

strong:
اثر با ابعاد مهم درخواست تطابق قوی دارد.

partial:
اثر با بخشی از خواسته کاربر تطابق دارد،
اما یک یا چند محدودیت یا تفاوت مهم هم دارد.

unsuitable:
اثر تضاد اساسی با درخواست دارد،
یا تقریباً هیچ ارتباط معناداری با
تجربه یا موضوع خواسته‌شده ندارد.

نکته بسیار مهم:

PARTIAL به معنی نامناسب نیست.

اگر اثر از نظر حس‌وحال عالی است اما
داستان دقیقاً منطبق نیست، می‌تواند
partial باشد.

اگر داستان بسیار مرتبط است اما
فضای اثر قدری تلخ‌تر از خواسته کاربر است،
می‌تواند partial باشد.

این آثار نباید بی‌دلیل حذف شوند.

----------------------------------------
SAD VS DEPRESSING
----------------------------------------

این تفاوت را جدی بگیر:

غمگین بودن ≠ افسرده‌کننده بودن
گریه‌آور بودن ≠ ناامیدکننده بودن
سوگواری ≠ تجربه کاملاً تلخ
کمدی تلخ ≠ نبود امید

ممکن است اثری هم غمگین باشد
و هم امیدبخش و تسکین‌دهنده.

اگر کاربر گفته:
«خیلی افسرده‌کننده نباشه»

فقط وجود برچسب «گریه‌آور»
برای حذف قطعی کافی نیست.

باید ببینی آیا:

- غم بر کل تجربه غالب است؟
- اثر امید و دلگرمی نیز منتقل می‌کند؟
- طنز، سنگینی داستان را متعادل می‌کند؟
- اثر در مجموع ممکن است بیش از اندازه
  برای کاربر سنگین باشد؟

اگر پاسخ قطعی نیست، partial بده
و هشدار مشخص ثبت کن.

برای مثال:

«داستان بسیار منطبق است، اما به‌دلیل
سوگ و لحظات اشک‌آور ممکن است
کمی سنگین‌تر از انتظار شما باشد.»

اما این مثال را بی‌دلیل برای
آثار دیگر تکرار نکن.

----------------------------------------
STORY VS EXPERIENCE
----------------------------------------

تطابق داستانی دقیق یعنی:
آن موقعیت یا تجربه واقعاً بخش مهمی
از روایت اثر باشد.

تفاوت مهم:

- تغییر مکان زندگی
- ورود به شغل جدید
- داشتن یک ماجرای تازه
- روبه‌رو شدن با چالش معمولی

این‌ها لزوماً به معنی
«شروع دوباره پس از شکست جدی» نیستند.

ستاره را به خاطر شباهت‌های
سطحی داستانی صادر نکن.

----------------------------------------
STAR RULE
----------------------------------------

star=true فقط برای تطابق واقعاً قوی.

اگر کاربر داستان مشخصی خواسته،
تمام موارد زیر باید برقرار باشند:

1. شواهد مخاطبان از تطابق قوی حس‌وحال
   و تجربه تماشایی پشتیبانی کنند.

2. داستان مستقیماً با موقعیت
   خواسته‌شده ارتباط داشته باشد.

3. هیچ تعارض مهم و حل‌نشده‌ای
   با خواسته‌های کاربر وجود نداشته باشد.

4. بتوانی دلیل ستاره را
   مشخص، مستند و بدون اغراق توضیح دهی.

اگر وضعیت partial است،
star باید false باشد.

اگر داستان مشخصی درخواست نشده،
ستاره می‌تواند بر اساس
تطابق بسیار قوی تجربه تماشایی باشد.

امتیاز بالا به‌تنهایی ستاره نمی‌سازد.

----------------------------------------
AUDIENCE EVIDENCE
----------------------------------------

audience_evidence:
شواهد کامنتی استخراج‌شده در
مرحله غربالگری است.

از این شواهد برای توصیف
تجربه مخاطبان استفاده کن.

اگر شواهد فقط می‌گویند «سرگرم‌کننده»،
نتیجه نگیر که اثر حتماً
«امیدبخش و ضد افسردگی» است.

از خودت نظر مخاطب،
نقل‌قول یا آمار نساز.

concepts و special_phrases داده‌های اصلی همان اثر هستند؛ هر دو را همراه audience_evidence مرور کن.
special_phrases نقل‌قول‌های کوتاه از کامنت‌ها هستند و جهت قطعی از پیش ندارند.
معنا، کنایه، نفی و ارتباط عبارت با خود اثر را بسنج؛ حرف مربوط به سایت یا اثر دیگر را شاهد حساب نکن.
اگر عبارت با برداشت قبلی تعارض دارد، آن را در داوری و هشدار لحاظ کن؛ عبارت منفرد نماینده نظر غالب نیست.


----------------------------------------
REASON
----------------------------------------

reason باید یک یا دو جمله طبیعی باشد.

ترجیحاً ابتدا توضیح بده که
مفاهیم استخراج‌شده از نظرات،
چه تجربه‌ای را نشان می‌دهند.

سپس ارتباط داستانی را توضیح بده.

اگر تطابق نسبی است،
تفاوت مهم را پنهان نکن.

warning:
فقط برای partial یا مواردی
که احتیاط واقعاً لازم است.

برای strong می‌تواند خالی باشد.

برای unsuitable:
rejection_reason را به طور واضح بنویس.

----------------------------------------
OUTPUT REQUIREMENTS
----------------------------------------

تمام عنوان‌های ورودی را برگردان.

برای هر عنوان دقیقاً یک تصمیم.

نام‌ها را تغییر نده.
شناسه SIM را حفظ کن.

فقط JSON معتبر:

{{
  "results": [
    {{
      "title": "",
      "match_level": "strong",
      "star": false,
      "reason": "",
      "warning": "",
      "star_reason": "",
      "rejection_reason": ""
    }}
  ]
}}

match_level فقط یکی از این‌ها:
strong
partial
unsuitable

----------------------------------------
CANDIDATES
----------------------------------------

{json.dumps(items, ensure_ascii=False)}
"""

def judge_group(items, gemini_request, build_final_prompt, stage="final_judge_v2"):

    raw = gemini_request(
        build_final_prompt(items),
        stage,
        len(items)
    )

    if isinstance(raw, dict):
        rows = raw.get("results", [])
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    if not isinstance(rows, list):
        rows = []

    allowed = {x["title"] for x in items}
    found = {}

    for row in rows:

        if not isinstance(row, dict):
            continue

        title = row.get("title")
        level = row.get("match_level")

        if title not in allowed:
            continue

        if level not in (
            "strong",
            "partial",
            "unsuitable"
        ):
            continue

        if title in found:
            continue

        found[title] = row

    return found

_judge_group = judge_group

class FSKEngine:
    def __init__(self):
        self.sb=create_client(os.environ['SUPABASE_URL'],os.environ['SUPABASE_SECRET_KEY'])
        self.keys=[(i,os.environ[f'GEMINI_KEY_{i}']) for i in range(1,15) if os.getenv(f'GEMINI_KEY_{i}')]
        if not self.keys:raise RuntimeError('No GEMINI_KEY_* configured')
        movies=self._load_all('movies','id,title,description,recommendation_tags,imdb_rating,year,countries,genres,poster_url,actors,directors_creators,runtime_minutes,digimoviez_url,imdb_url,analysis_status,comments_analyzed')
        concepts=self._load_all('movie_concepts','id,series_id,concept')
        phrases=self._load_all('movie_special_phrases','id,series_id,phrase')
        cmap=defaultdict(list);pmap=defaultdict(list)
        for row in concepts:
            if row.get('concept'):cmap[str(row['series_id'])].append(row['concept'])
        for row in phrases:
            if row.get('phrase'):pmap[str(row['series_id'])].append(row['phrase'])
        self.movie_lookup={str(m['id']):m for m in movies}
        counts=Counter(m['title'] for m in movies)
        self.simulated=[]
        for movie in movies:
            movie['concepts']=cmap.get(str(movie['id']),[])
            movie['special_phrases']=pmap.get(str(movie['id']),[])
            item=dict(movie,sid=str(movie['id']),source_id=str(movie['id']),source_title=movie['title'])
            if counts[movie['title']]>1:
                item['title']=movie['title']+' [SIM-REAL-'+str(movie['id'])+']'
            self.simulated.append(item)
        self.by_sim_title={m['title']:m for m in self.simulated}
        if len(self.by_sim_title)!=len(movies):raise RuntimeError('Ambiguous movie identifiers')
        self.movies=movies
        self.series=movies  # Original telegram_bot.py reads engine.series for its startup log.
        print(f'FSK ENGINE READY | {len(movies)} movies | batch=400 | lanes=2',flush=True)

    def _load_all(self,table,columns):
        if table not in {'movies','movie_concepts','movie_special_phrases'}:
            raise RuntimeError('Table outside movie-only read routes')
        rows=[];offset=0
        expected=self.sb.table(table).select('id',count='exact',head=True).execute().count
        if expected is None:raise RuntimeError('Exact database count unavailable')
        while len(rows)<expected:
            page=(self.sb.table(table).select(columns).order('id')
                  .range(offset,offset+999).execute().data or [])
            if not page:raise RuntimeError(f'Incomplete read: {table}: {len(rows)}/{expected}')
            rows.extend(page);offset+=len(page)
        if len(rows)!=expected or len({str(r['id']) for r in rows})!=expected:
            raise RuntimeError(f'Dataset changed during load: {table}')
        print(f'LOADED {table}: {len(rows)}',flush=True)
        return rows

    def _check(self,cancel_event):
        if cancel_event is not None and cancel_event.is_set():raise SearchCancelled()

    def _gemini_request(self,prompt,stage,batch_size,queue,cancel_event):
        self._check(cancel_event)
        deadline=queue.clock()+MAX_WAIT_PER_CALL_SECONDS
        transient=0;attempt=0
        while transient<max(40,len(self.keys)*4):
            self._check(cancel_event)
            number,key=queue.acquire(deadline,stage)
            client=None;started=time.monotonic();attempt+=1
            print(f'GEMINI {stage} | KEY {number} | rows={batch_size} | attempt={attempt}',flush=True)
            try:
                client=genai.Client(api_key=key,http_options={'timeout':API_TIMEOUT_MS,'retry_options':{'attempts':1}})
                response=client.models.generate_content(model=MODEL,contents=prompt,
                    config=types.GenerateContentConfig(response_mime_type='application/json',temperature=.1))
                result=parse_json(response.text)
            except Exception as exc:
                category=classify(exc)
                wait=queue.release(number,category,exc)
                print(f'GEMINI KEY {number} | {category} | backoff={wait:.0f}s',flush=True)
                if category in ('MODEL','INPUT'):
                    queue.pause(category+': check model/request')
                    raise PermanentGeminiError(queue.pause_reason) from None
                if category not in ('AUTH','PERMISSION'):transient+=1
            else:
                queue.release(number)
                print(f'GEMINI {stage} | KEY {number} DONE {time.monotonic()-started:.2f}s',flush=True)
                self._check(cancel_event)
                return result
            finally:
                if client is not None:
                    try:client.close()
                    except Exception:pass
        queue.pause('Gemini temporary retry budget reached')
        raise SearchPaused(queue.pause_reason)

    def _screen(self,query,intent,queue,request,cancel_event):
        jobs=[(i,self.simulated[i:i+BATCH_SIZE]) for i in range(0,len(self.simulated),BATCH_SIZE)]
        lanes=[jobs[i::2] for i in range(2)]
        stop=threading.Event()
        def worker(lane_no,lane_jobs):
            collected=[]
            for offset,batch in lane_jobs:
                self._check(cancel_event)
                if stop.is_set():break
                print(f'BATCH START | lane={lane_no+1} | offset={offset} | size={len(batch)}',flush=True)
                try:
                    raw=request(batch_prompt(batch,query,intent,compact=False),'candidate',len(batch))
                    got=normalized_candidates(raw,self.by_sim_title,intent,query)
                except Exception:
                    stop.set()
                    raise
                collected.extend(got)
                print(f'BATCH DONE | lane={lane_no+1} | offset={offset} | candidates={len(got)}',flush=True)
            return collected
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(worker,i,lane) for i,lane in enumerate(lanes)]
            # Same lane order as the supplied Colab; tie ordering does not depend on response speed.
            all_items=[item for future in futures for item in future.result()]
        def ranking(item):
            mood=float(item.get('mood_score',0) or 0)
            story=float(item.get('story_score',0) or 0)
            score=(min(70,max(0,mood))+min(30,max(0,story))) if intent['story_requested'] else min(100,max(0,mood))
            return (round(score,2),mood,story)
        unique={}
        for item in all_items:
            key=str(item.get('source_id') or item.get('source_title') or item.get('title') or '').strip()
            if key and (key not in unique or ranking(item)>ranking(unique[key])):unique[key]=item
        return sorted(unique.values(),key=ranking,reverse=True)[:FINAL_CANDIDATE_LIMIT]

    def search(self, user_query, cancel_event=None):
        self._check(cancel_event)
        search_start=time.monotonic()
        queue=SharedRoundQueue(self.keys)
        original_sleep=queue.sleep
        def sleep(seconds):
            self._check(cancel_event)
            if cancel_event is None:original_sleep(seconds)
            else:cancel_event.wait(seconds)
            self._check(cancel_event)
        queue.sleep=sleep
        def gemini_request(prompt,stage,batch_size=0):
            return self._gemini_request(prompt,stage,batch_size,queue,cancel_event)
        SAMPLE_QUERY=user_query
        print('=== SEARCH START ===',flush=True)
        raw=gemini_request(INTENT_PROMPT.format(query=user_query),'intent')
        if not isinstance(raw,dict):raise RuntimeError('Gemini intent is not an object')
        intent=normalize_intent(raw,user_query)
        story_requested=intent['story_requested']
        MOOD_MAX=70 if story_requested else 100
        STORY_MAX=30 if story_requested else 0
        FINAL_LIMIT=50
        JUDGE_CHUNK_SIZE=12
        RETRY_CHUNK_SIZE=4
        by_sim_title=self.by_sim_title
        candidates=self._screen(user_query,intent,queue,gemini_request,cancel_event)
        final_prompt=partial(build_final_prompt,SAMPLE_QUERY=SAMPLE_QUERY,intent=intent,MOOD_MAX=MOOD_MAX,STORY_MAX=STORY_MAX)
        call_judge=partial(_judge_group,gemini_request=gemini_request,build_final_prompt=final_prompt)
        def judge_group(items,stage='final_judge_v2'):
            return call_judge(items,stage=stage)
        judge_payload = []
        candidate_lookup = {}

        for c in candidates:

            sim_title = fsk_text(c.get("title"))

            if not sim_title:
                continue

            if sim_title in candidate_lookup:
                continue

            original = by_sim_title.get(sim_title, {})

            mood = fsk_score(
                c.get("mood_score"),
                MOOD_MAX
            )

            story = (
                fsk_score(
                    c.get("story_score"),
                    STORY_MAX
                )
                if story_requested else 0.0
            )

            item = {
                "title": sim_title,
                "mood_score": mood,
                "story_score": story,
                "total_score": round(mood + story, 2),
                "preliminary_star": c.get("star") is True,
                "reason": fsk_text(c.get("reason")),
                "star_reason": fsk_text(c.get("star_reason")),
                "audience_evidence": fsk_list(
                    c.get("audience_evidence")
                ),
                "story_evidence": fsk_text(
                    c.get("story_evidence")
                ),
                "concepts": original.get("concepts") or [],
                "special_phrases": original.get("special_phrases") or []
            }

            judge_payload.append(item)

            candidate_lookup[sim_title] = {
                "candidate": c,
                "original": original,
                "payload": item
            }

        started = time.monotonic()

        judgments = {}
        failed_groups = []

        groups = list(
            fsk_chunks(
                judge_payload,
                JUDGE_CHUNK_SIZE
            )
        )

        print("\nJUDGING", len(judge_payload), "CANDIDATES")
        print("GROUPS:", len(groups))

        for i, group in enumerate(groups, 1):

            print(
                f"\nGROUP {i}/{len(groups)} | "
                f"INPUT {len(group)}",
                flush=True
            )

            try:

                got = judge_group(group)
                judgments.update(got)

                missing = [
                    x for x in group
                    if x["title"] not in judgments
                ]

                print(
                    "RECEIVED:", len(got),
                    "| MISSING:", len(missing)
                )

                if missing:

                    for smaller in fsk_chunks(
                        missing,
                        RETRY_CHUNK_SIZE
                    ):

                        try:
                            retry = judge_group(
                                smaller,
                                "final_judge_v2_retry"
                            )
                            judgments.update(retry)

                        except Exception as exc:
                            print(
                                "RETRY FAILED:",
                                str(exc)[:150]
                            )

            except Exception as exc:

                failed_groups.append({
                    "group": i,
                    "error": str(exc)[:300]
                })

                print(
                    "FAILED:",
                    str(exc)[:150]
                )

        elapsed = round(
            time.monotonic() - started,
            2
        )


        # =====================================================
        # BUILD ALL JUDGMENTS
        # =====================================================

        all_judged_results = []

        for item in judge_payload:

            title = item["title"]

            if title not in judgments:
                continue

            verdict = judgments[title]
            info = candidate_lookup[title]

            previous = info["candidate"]
            original = info["original"]

            level = verdict["match_level"]

            evidence = item["audience_evidence"]
            story_evidence = item["story_evidence"]

            # Keep Gemini verdict, with safeguards.
            star = (
                verdict.get("star") is True
                and level == "strong"
            )

            if story_requested:
                star = (
                    star
                    and bool(evidence)
                    and bool(story_evidence)
                    and item["mood_score"] >= 56
                    and item["story_score"] >= 24
                )
            else:
                star = (
                    star
                    and bool(evidence)
                    and item["mood_score"] >= 85
                )

            reason = fsk_text(
                verdict.get("reason")
                or item["reason"]
            )

            warning = fsk_text(
                verdict.get("warning")
            )

            rejection_reason = fsk_text(
                verdict.get("rejection_reason")
            )

            # Never use a missing rejection reason
            # to fabricate an explanation.
            if level == "unsuitable" and not rejection_reason:
                rejection_reason = reason

            row = {
                "title": (
                    original.get("source_title")
                    or previous.get("source_title")
                    or title
                ),
                "sim_title": title,
                "source_id": (
                    original.get("source_id")
                    or previous.get("source_id")
                ),
                "mood_score": item["mood_score"],
                "story_score": item["story_score"],
                "total_score": item["total_score"],
                "score": item["total_score"],
                "experience_score": item["mood_score"],
                "preference_score": item["story_score"],
                "match_level": level,
                "suitable": level != "unsuitable",
                "star": star,
                "star_reason": (
                    fsk_text(verdict.get("star_reason"))
                    if star else ""
                ),
                "reason": reason,
                "warning": warning,
                "rejection_reason": rejection_reason,
                "audience_evidence": evidence,
                "story_evidence": story_evidence,
                "preliminary_star": item["preliminary_star"]
            }

            all_judged_results.append(row)


        # =====================================================
        # DEDUPLICATE AND RANK
        # =====================================================

        level_priority = {
            "strong": 2,
            "partial": 1,
            "unsuitable": 0
        }

        # Scores are not secretly changed.
        # Rank by weighted score.
        # For equal scores, prioritize stronger match levels.

        all_judged_results.sort(
            key=lambda x: (
                x["total_score"],
                level_priority[x["match_level"]],
                x["mood_score"],
                x["story_score"]
            ),
            reverse=True
        )

        unique = {}

        for row in all_judged_results:

            key = str(
                row.get("source_id")
                or row["sim_title"]
            )

            if key not in unique:
                unique[key] = row

        all_judged_results = list(unique.values())

        # Only truly unsuitable titles are excluded
        # from recommendations. Partial matches remain.

        recommendable = [
            x for x in all_judged_results
            if x["match_level"] != "unsuitable"
        ]

        rejected_results = [
            x for x in all_judged_results
            if x["match_level"] == "unsuitable"
        ]

        final_results = recommendable[:FINAL_LIMIT]

        # Future Telegram filters
        starred_results = [
            x for x in final_results
            if x["star"]
        ]

        nonstarred_results = [
            x for x in final_results
            if not x["star"]
        ]
        self._check(cancel_event)
        missing=[x['title'] for x in judge_payload if x['title'] not in judgments]
        if missing:
            raise RuntimeError(f'Gemini final judgments missing: {len(missing)}')
        fields=('year','countries','genres','poster_url','imdb_rating','description',
                'actors','directors_creators','runtime_minutes','digimoviez_url','imdb_url')
        out=[]
        for row in final_results:
            db=self.movie_lookup[str(row['source_id'])]
            out.append({**row,**{field:db.get(field) for field in fields}})
        print(f'=== SEARCH FINISHED | {len(out)} RESULTS | {time.monotonic()-search_start:.2f}s ===',flush=True)
        return out

