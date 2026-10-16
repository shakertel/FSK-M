import json

import time

from pathlib import Path

TEST_SIZE = 400

FINAL_LIMIT = 50

JUDGE_CHUNK_SIZE = 12

RETRY_CHUNK_SIZE = 4

if not isinstance(globals().get("full_results"), dict):
    raise RuntimeError("نتایج غربالگری در این runtime موجود نیست؛ سلول ۷ را بررسی کن.")

result_key = TEST_SIZE if TEST_SIZE in full_results else str(TEST_SIZE)

if result_key not in full_results:
    available = {str(k): len(v) for k, v in full_results.items()}
    raise RuntimeError(
        f"نتایج بچ {TEST_SIZE} پیدا نشد. نتایج موجود: {available}. "
        "اگر غربالگری انجام شده، اندازه بچ را با خروجی آن تطبیق بده؛ دوباره اجرا نکن."
    )

candidates = full_results[result_key]

report = next(
    (x for x in reversed(globals().get("full_reports", []))
     if str(x.get("batch_size")) == str(TEST_SIZE)),
    None
)

if not report:
    raise RuntimeError(f"گزارش غربالگری بچ {TEST_SIZE} موجود نیست.")

if not report.get("complete"):
    raise RuntimeError(
        f"غربالگری بچ {TEST_SIZE} کامل نیست؛ "
        f"موفق={report.get('successful_batches', '?')}، "
        f"ناموفق={report.get('failed_batches', '?')}، "
        f"درانتظار={report.get('pending_batches', '?')}. "
        "ابتدا غربالگری را از کش ادامه بده."
    )

if not isinstance(candidates, list):
    raise RuntimeError("ساختار نتایج غربالگری باید list باشد.")

print(f"✅ Screening batch={TEST_SIZE} | candidates={len(candidates)} | complete=True")

if not candidates:
    print("غربالگری کامل بوده ولی کاندیدی انتخاب نشده؛ تماس داوری انجام نمی‌شود.")

story_requested = bool(intent.get("story_requested"))

MOOD_MAX = 70 if story_requested else 100

STORY_MAX = 30 if story_requested else 0

OUTPUT_DIR = Path(OUTPUT_DIR)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

print("=" * 75)

print("FSK FINAL JUDGE V2")

print("=" * 75)

print("QUERY:", SAMPLE_QUERY)

print("CANDIDATES:", len(candidates))

print("SCORING:", MOOD_MAX, "/", STORY_MAX)

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

def build_final_prompt(items):

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

def judge_group(items, stage="final_judge_v2"):

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

level_priority = {
    "strong": 2,
    "partial": 1,
    "unsuitable": 0
}

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

recommendable = [
    x for x in all_judged_results
    if x["match_level"] != "unsuitable"
]

rejected_results = [
    x for x in all_judged_results
    if x["match_level"] == "unsuitable"
]

final_results = recommendable[:FINAL_LIMIT]

starred_results = [
    x for x in final_results
    if x["star"]
]

nonstarred_results = [
    x for x in final_results
    if not x["star"]
]

final_outputs = globals().get("final_outputs", {})

final_outputs[TEST_SIZE] = final_results

missing_titles = [
    x["title"]
    for x in judge_payload
    if x["title"] not in judgments
]

complete = not missing_titles

counts = {
    level: sum(
        x["match_level"] == level
        for x in all_judged_results
    )
    for level in ("strong", "partial", "unsuitable")
}

print("\n" + "=" * 75)

print("FSK FINAL JUDGE V2 REPORT")

print("=" * 75)

print("Input candidates:", len(judge_payload))

print("Judged candidates:", len(judgments))

print("Missing judgments:", len(missing_titles))

print("STRONG:", counts["strong"])

print("PARTIAL:", counts["partial"])

print("UNSUITABLE:", counts["unsuitable"])

print("Final recommendations:", len(final_results))

print("Starred:", len(starred_results))

print("Non-starred:", len(nonstarred_results))

print("Final judge time:", elapsed, "seconds")

print(
    "Screening + Final:",
    round(report["seconds"] + elapsed, 2),
    "seconds"
)

print("Intent and repair time not included.")

print("JUDGMENT COMPLETE:", complete)

if not complete:
    print("MISSING:", missing_titles)

print("\n" + "=" * 75)

print("TOP 20 — USER-FACING")

print("=" * 75)

for i, x in enumerate(final_results[:20], 1):

    symbol = "⭐ " if x["star"] else ""
    level = x["match_level"]

    print(
        f"\n{i}. {symbol}{x['title']}"
    )
    print(
        f"Score: {x['total_score']}/100"
        f" | Level: {level}"
    )
    print("دلیل:", x["reason"])

    if x["warning"]:
        print("⚠️ نکته:", x["warning"])

    if x["star"]:
        print("دلیل ستاره:", x["star_reason"])

    print("-" * 60)

def save_json(filename, data):

    path = OUTPUT_DIR / filename

    path.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            default=str
        ),
        encoding="utf-8"
    )

    return path

saved = {}

saved["all"] = save_json(
    "fsk_final_v2_all.json",
    final_results
)

saved["starred"] = save_json(
    "fsk_final_v2_starred.json",
    starred_results
)

saved["nonstarred"] = save_json(
    "fsk_final_v2_nonstarred.json",
    nonstarred_results
)

saved["rejected"] = save_json(
    "fsk_final_v2_rejected.json",
    rejected_results
)

saved["audit"] = save_json(
    "fsk_final_v2_full_audit.json",
    all_judged_results
)

saved["missing"] = save_json(
    "fsk_final_v2_missing.json",
    missing_titles
)

print("\nFILES SAVED:")

for name, path in saved.items():
    print(name, ":", path)

print("\nFSK FINAL JUDGE V2 FINISHED")
