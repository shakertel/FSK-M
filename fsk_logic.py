import json

MAX_PER_BATCH = 10

FINAL_CANDIDATE_LIMIT = 100

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

def normalized_candidates(got):

    if isinstance(got, dict):
        got = got.get(
            "results",
            got.get("candidates", [])
        )

    if not isinstance(got, list):
        return []

    current_intent = normalize_intent(
        globals().get("intent", {}),
        globals().get("SAMPLE_QUERY", "")
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

def prelim(candidate):

    current_intent = normalize_intent(
        globals().get("intent", {}),
        globals().get("SAMPLE_QUERY", "")
    )

    mood_max = (
        70 if current_intent["story_requested"]
        else 100
    )

    story_max = (
        30 if current_intent["story_requested"]
        else 0
    )

    return round(
        _clamp(
            candidate.get("mood_score"), mood_max
        )
        +
        _clamp(
            candidate.get("story_score"), story_max
        ),
        2
    )
