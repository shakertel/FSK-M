import os
import asyncio
import threading
import time
import html
import io
import requests
from PIL import Image

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from telegram.error import TelegramError

from engine import FSKEngine, SearchCancelled


# ============================================================
# CONFIG
# ============================================================

SEARCH_TIMEOUT = 15 * 60
PAGE_SIZE = 3

WELCOME = (
    "سلام به ربات فیلم سرچر کردستان خوش آمدید! 🎬\n\n"
    "حس و حال و فیلمی که دنبالش هستینو مثل مثال تایپ کنید و ارسال کنین همینجا:\n\n"
    "«الان حالم خوب نیست شکست عشقی خوردم دنبال یه فیلمم "
    "که بتونم سرگرم بشم و حالم خوب شه.»\n\n"
    "حالا درخواست خودتان را بفرستید."
)


# ============================================================
# GLOBALS
# ============================================================

engine = None

states = {}
jobs = {}
locks = {}


# ============================================================
# HELPERS
# ============================================================

def keyboard(*rows):

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text,
                    callback_data=data
                )
                for text, data in row
            ]
            for row in rows
        ]
    )


def touch(uid):

    states.setdefault(
        uid,
        {}
    )["last_activity"] = time.monotonic()


def clear_user(uid):

    ev = jobs.pop(
        uid,
        None
    )

    if ev:
        ev.set()

    states.pop(
        uid,
        None
    )


def esc(x):

    return html.escape(
        str(
            x
            if x is not None
            else ""
        )
    )


def clean_list(value, limit=4):

    if not value:
        return ""

    if isinstance(
        value,
        (list, tuple)
    ):

        return ", ".join(
            str(x)
            for x in value[:limit]
            if x
        )

    return str(value)


# ============================================================
# COMPACT METADATA
# ============================================================

def compact_info(x):

    parts = []


    # Country
    countries = clean_list(
        x.get("countries"),
        limit=3
    )

    if countries:
        parts.append(
            countries
        )


    # IMDb
    imdb = x.get(
        "imdb_rating"
    )

    if imdb is not None:
        parts.append(
            f"{imdb} IMDb"
        )


    # Genres
    genres = clean_list(
        x.get("genres"),
        limit=4
    )

    if genres:
        parts.append(
            genres
        )


    # Year
    year = x.get(
        "year"
    )

    if year:
        parts.append(
            str(year)
        )


    # Seasons
    seasons = x.get(
        "seasons_count"
    )

    if seasons:

        try:
            seasons = int(
                seasons
            )

            parts.append(
                f"{seasons} فصل"
            )

        except Exception:

            parts.append(
                str(seasons)
            )


    return " • ".join(
        parts
    )


# ============================================================
# /START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    uid = update.effective_user.id

    clear_user(
        uid
    )

    states[uid] = {
        "stage": "awaiting_query",
        "last_activity": time.monotonic()
    }

    await update.effective_message.reply_text(
        WELCOME,
        reply_markup=keyboard(
            [
                (
                    " کاکه لغو ؟❌",
                    "cancel"
                )
            ]
        )
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    uid = update.effective_user.id

    clear_user(
        uid
    )

    states[uid] = {
        "stage": "awaiting_query",
        "last_activity": time.monotonic()
    }

    msg = (
        "جستجو لغو شد.\n\n"
        + WELCOME
    )


    if update.callback_query:

        await update.callback_query.answer()

        await update.callback_query.message.reply_text(
            msg,
            reply_markup=keyboard(
                [
                    (
                        "لغو ❌",
                        "cancel"
                    )
                ]
            )
        )

    else:

        await update.effective_message.reply_text(
            msg,
            reply_markup=keyboard(
                [
                    (
                        "لغو ❌",
                        "cancel"
                    )
                ]
            )
        )


# ============================================================
# USER TEXT
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    uid = update.effective_user.id

    st = states.get(
        uid
    )


    if (
        st
        and st.get("stage")
        == "searching"
    ):

        await update.message.reply_text(
            "جستجوی قبلی هنوز در حال انجام است. "
            "برای توقف، «لغو» را بزنید.",
            reply_markup=keyboard(
                [
                    (
                        "لغو جستجو ❌",
                        "cancel"
                    )
                ]
            )
        )

        return


    q = (
        update.message.text
        or ""
    ).strip()


    if not q:
        return


    states[uid] = {
        "stage": "confirm",
        "query": q,
        "last_activity": time.monotonic()
    }


    await update.message.reply_text(

        "درخواست شما:\n\n"
        + q
        + "\n\nآیا این پیام نهایی شماست کاکه گیان؟؟",

        reply_markup=keyboard(

            [
                (
                    "تأیید و جستجو بژی کوردیستان✅",
                    "confirm"
                )
            ],

            [
                (
                    "لغو ❌",
                    "cancel"
                )
            ]
        )
    )



def make_small_poster(url, idx):
    if not url:
        return None

    try:
        r = requests.get(
            url,
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        r.raise_for_status()

        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((180, 270))

        bio = io.BytesIO()
        img.save(
            bio,
            format="JPEG",
            quality=68,
            optimize=True
        )
        bio.seek(0)

        return InputFile(
            bio,
            filename=f"poster_{idx}.jpg"
        )

    except Exception as e:
        print("POSTER PREP ERROR", repr(e), flush=True)
        return None


def build_caption(idx, x):
    star = "⭐ " if x.get("star") else ""
    title = esc(x.get("title", "Unknown"))
    info = esc(compact_info(x))
    description = esc(x.get("description", ""))
    reason = esc(x.get("reason", ""))

    parts = [f"<b>{idx}. {star}{title}</b>"]

    if info:
        parts.append(info)

    if description:
        parts.append(f"📖 {description}")

    if reason:
        parts.append(f"🎯 {reason}")

    if x.get("star") and x.get("star_reason"):
        parts.append("⭐ " + esc(x.get("star_reason")))

    caption = "\n\n".join(parts)

    # Photo captions are limited, so keep it safely under 1024 chars.
    if len(caption) > 1000:
        caption = caption[:997].rstrip() + "..."

    return caption


# ============================================================
# SHOW RESULTS
# ============================================================

async def show_page(
    chat_id,
    uid,
    context
):
    st = states.get(uid)

    if (
        not st
        or st.get("stage") != "results"
    ):
        return

    results = st["results"]
    pos = st.get("pos", 0)

    end = min(
        pos + PAGE_SIZE,
        len(results)
    )

    for idx in range(pos, end):
        x = results[idx]

        caption = build_caption(
            idx + 1,
            x
        )

        poster_file = make_small_poster(
            x.get("poster_url"),
            idx + 1
        )

        if poster_file:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=poster_file,
                    caption=caption,
                    parse_mode="HTML"
                )
                continue

            except Exception as e:
                print(
                    "SEND PHOTO/CAPTION ERROR",
                    repr(e),
                    flush=True
                )

        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    st["pos"] = end
    touch(uid)

    if end < len(results):
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"{end} مورد از {len(results)} پیشنهاد "
                "نمایش داده شد. ادامه بدهم؟"
            ),
            reply_markup=keyboard(
                [("۳ تای بعدی ▶️", "next")],
                [("کافیه ✅", "enough"), ("لغو ❌", "cancel")]
            )
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="تمام پیشنهادهای این جستجو نمایش داده شد.",
            reply_markup=keyboard(
                [("جستجوی جدید 🔎", "new"), ("کافیه ✅", "enough")]
            )
        )


# ============================================================
# SEARCH THREAD
# ============================================================

async def run_search(
    query,
    ev
):

    return await asyncio.to_thread(
        engine.search,
        query,
        ev
    )


# ============================================================
# CONFIRM SEARCH
# ============================================================

async def confirm_search(
    query_obj,
    context
):

    uid = query_obj.from_user.id

    chat_id = (
        query_obj.message.chat_id
    )


    if uid in jobs:

        await query_obj.answer(
            "یک جستجو در حال اجراست."
        )

        return


    await query_obj.answer()


    ev = threading.Event()

    jobs[uid] = ev


    states[uid][
        "stage"
    ] = "searching"

    touch(
        uid
    )


    await query_obj.message.reply_text(

        "🔎 جستجو شروع شد. "
        "ممکن است چند دقیقه طول بکشد.پیروز بیت ❤️",

        reply_markup=keyboard(
            [
                (
                    "لغو جستجو ❌",
                    "cancel"
                )
            ]
        )
    )


    try:

        results = await asyncio.wait_for(

            run_search(
                states[uid]["query"],
                ev
            ),

            timeout=SEARCH_TIMEOUT
        )


        if (
            ev.is_set()
            or uid not in states
        ):

            return


        if not results:

            raise RuntimeError(
                "نتیجه‌ای پیدا نشد"
            )


        jobs.pop(
            uid,
            None
        )


        original_query = (
            states[uid]["query"]
        )


        states[uid] = {

            "stage":
                "results",

            "query":
                original_query,

            "results":
                results,

            "pos":
                0,

            "last_activity":
                time.monotonic()
        }


        await query_obj.message.reply_text(
            f"✅ {len(results)} پیشنهاد آماده شد."
        )


        await show_page(
            chat_id,
            uid,
            context
        )


    except asyncio.TimeoutError:

        ev.set()

        jobs.pop(
            uid,
            None
        )

        states[uid] = {
            "stage": "awaiting_query",
            "last_activity": time.monotonic()
        }


        await context.bot.send_message(

            chat_id=chat_id,

            text=(
                "⏱ جستجو بعد از ۱۵ دقیقه "
                "متوقف شد تا ربات گیر نکند. "
                "درخواست جدیدتان را بفرستید."
            )
        )


    except SearchCancelled:

        jobs.pop(
            uid,
            None
        )


    except Exception as e:

        ev.set()

        jobs.pop(
            uid,
            None
        )


        states[uid] = {
            "stage": "awaiting_query",
            "last_activity": time.monotonic()
        }


        print(
            "SEARCH ERROR",
            repr(e),
            flush=True
        )


        await context.bot.send_message(

            chat_id=chat_id,

            text=(
                "جستجو با خطا متوقف شد و "
                "ربات دوباره آماده است. "
                "درخواستتان را دوباره بفرستید."
            )
        )


# ============================================================
# BUTTONS
# ============================================================

async def buttons(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    q = update.callback_query

    uid = q.from_user.id

    data = q.data


    # Cancel
    if data == "cancel":

        await cancel(
            update,
            context
        )

        return


    # Enough
    if data == "enough":

        await q.answer()

        clear_user(
            uid
        )

        states[uid] = {
            "stage": "awaiting_query",
            "last_activity": time.monotonic()
        }

        await q.message.reply_text(
            "باشه 🌷 هر وقت خواستید "
            "درخواست جدیدتان را بفرستید."
        )

        return


    # New search
    if data == "new":

        await q.answer()

        clear_user(
            uid
        )

        states[uid] = {
            "stage": "awaiting_query",
            "last_activity": time.monotonic()
        }

        await q.message.reply_text(

            WELCOME,

            reply_markup=keyboard(
                [
                    (
                        "لغو ❌",
                        "cancel"
                    )
                ]
            )
        )

        return


    st = states.get(
        uid
    )


    if not st:

        await q.answer(
            "این جستجو دیگر فعال نیست."
        )

        return


    touch(
        uid
    )


    # Confirm
    if (
        data == "confirm"
        and st.get("stage")
        == "confirm"
    ):

        await confirm_search(
            q,
            context
        )

        return


    # Next page
    if (
        data == "next"
        and st.get("stage")
        == "results"
    ):

        await q.answer()

        await show_page(
            q.message.chat_id,
            uid,
            context
        )

        return


    await q.answer(
        "این دکمه دیگر فعال نیست."
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    print(
        "TELEGRAM ERROR",
        repr(
            context.error
        ),
        flush=True
    )


# ============================================================
# RUN BOT
# ============================================================

def run_bot():

    global engine


    token = os.getenv(
        "TELEGRAM_TOKEN"
    )


    if not token:

        raise RuntimeError(
            "TELEGRAM_TOKEN missing"
        )


    print(
        "Loading FSK database...",
        flush=True
    )


    engine = FSKEngine()


    print(
        f"FSK READY | "
        f"{len(engine.series)} titles loaded | "
        f"timeout=15m",
        flush=True
    )


    app = (
        Application
        .builder()
        .token(token)
        .build()
    )


    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    app.add_handler(
        CommandHandler(
            "cancel",
            cancel
        )
    )


    app.add_handler(
        CallbackQueryHandler(
            buttons
        )
    )


    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )


    app.add_error_handler(
        error_handler
    )


    print(
        "FSK bot started",
        flush=True
    )


    app.run_polling(
        drop_pending_updates=True
    )
