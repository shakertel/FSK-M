import os
import asyncio
import threading
import time
import html
import io
import re
import math
import secrets
from urllib.parse import urlparse
import requests
from PIL import Image, ImageOps

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
SORT_OPTIONS = {
    "default": "ترتیب فعلی (بهترین)",
    "imdb": "نمره IMDb",
    "match": "تطابق بالاتر با درخواست ⭐",
    "newest": "فیلم‌های جدیدتر",
}


def _sort_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace("٫", "."))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _match_score(item):
    for field in ("total_score", "score"):
        value = _sort_number(item.get(field))
        if value is not None:
            return value
    return None


def order_final_results(results, mode):
    """Order completed Gemini results for display only; stable on ties."""
    items = list(results)
    if mode == "match":
        if not any(_match_score(item) is not None for item in items):
            return sorted(items, key=lambda item: item.get("star") is True, reverse=True)
        def key(item):
            score = _match_score(item)
            return (score is not None, score if score is not None else 0,
                    item.get("star") is True if score is None else False)
        return sorted(items, key=key, reverse=True)
    if mode in ("imdb", "newest"):
        field = "imdb_rating" if mode == "imdb" else "year"
        def key(item):
            value = _sort_number(item.get(field))
            valid = value is not None and value > 0
            return (valid, value if valid else 0)
        return sorted(items, key=key, reverse=True)
    return items

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


    token = secrets.token_hex(4)
    states[uid] = {
        "stage": "choose_sort",
        "query": q,
        "sort_token": token,
        "last_activity": time.monotonic()
    }
    await update.message.reply_text(
        "نتایج نهایی بر چه اساس نمایش داده شوند؟",
        reply_markup=keyboard(
            *[[(label, f"sort:{token}:{mode}")]
              for mode, label in SORT_OPTIONS.items()],
            [("لغو ❌", "cancel")]
        )
    )



def _one_line(value):
    return ' '.join(str(value or '').split())


def _names(value):
    if not value:
        return ''
    if isinstance(value,(list,tuple)):
        names=[]
        for item in value:
            if isinstance(item,dict):item=item.get('name') or item.get('title') or ''
            text=_one_line(item)
            if text:names.append(text)
        return ', '.join(names)
    return _one_line(value)


def compact_info(x):
    # Keep the original order; all metadata is one plain-text line.
    parts=[]
    country=_one_line(clean_list(x.get('countries'),limit=3))
    genres=_one_line(clean_list(x.get('genres'),limit=4))
    if country:parts.append(country)
    if x.get('imdb_rating') is not None:parts.append(f"{x['imdb_rating']} IMDb")
    if genres:parts.append(genres)
    if x.get('year'):parts.append(_one_line(x['year']))
    actors=_names(x.get('actors'))
    directors=_names(x.get('directors_creators'))
    if actors:parts.append('بازیگران: '+actors)
    if directors:parts.append('کارگردان: '+directors)
    return ' • '.join(parts)


def _utf16_length(text):
    return len(str(text).encode('utf-16-le'))//2


def _visible_length(markup):
    # Strip only our real tags, then decode escaped user/database text.
    return _utf16_length(html.unescape(re.sub(r'<[^>]*>','',markup)))


def _take_text(text,units):
    prefix=text.encode('utf-16-le')[:units*2].decode('utf-16-le',errors='ignore')
    cut=len(prefix)
    if cut<len(text):
        boundary=max(prefix.rfind('\n'),prefix.rfind(' '))
        if boundary>len(prefix)*.7:cut=boundary
    if not cut:
        raise ValueError('Insufficient room for a text character')
    return text[:cut].rstrip(),text[cut:].lstrip()


def _card_header(idx,x):
    title=f"{idx}. "+('⭐ ' if x.get('star') else '')+str(x.get('title') or 'Unknown')
    info=compact_info(x)
    return '<b>'+esc(title)+'</b>'+('\n\n'+esc(info) if info else '')


def _card_sections(x):
    sections=[]
    for label,field in [('خلاصه داستان','description'),('دلیل پیشنهاد','reason')]:
        text=str(x.get(field) or '').strip()
        if text:sections.append((label,text))
    evidence=x.get('audience_evidence') or []
    if isinstance(evidence,str):evidence=[evidence]
    if isinstance(evidence,(list,tuple)):
        evidence=[str(v).strip() for v in evidence[:4] if str(v).strip()]
        if evidence:sections.append(('شواهد مخاطبان','\n'.join('• '+v for v in evidence)))
    if x.get('warning'):sections.append(('نکته',str(x['warning']).strip()))
    if x.get('star') and x.get('star_reason'):
        sections.append(('دلیل ستاره',str(x['star_reason']).strip()))
    return sections


def _quote(label,text):
    return '<b>'+esc(label)+':</b>\n<blockquote expandable>'+esc(text)+'</blockquote>'


def build_caption(idx,x):
    # Never cut generated HTML: it can break tags/entities and even fail photo sending.
    return '\n\n'.join([_card_header(idx,x)]+[_quote(label,text) for label,text in _card_sections(x)])


def _message_parts(header,sections,limit=4096):
    # Expandable quotes still count against Telegram's character limit.
    parts=[]
    current=''
    if header:
        if _visible_length(header)<=limit:
            current=header
        else:
            plain=html.unescape(re.sub(r'<[^>]*>','',header))
            while plain:
                piece,plain=_take_text(plain,limit)
                parts.append(esc(piece))
    for label,text in sections:
        text=str(text).strip()
        continued=False
        while text:
            heading=label+(' (ادامه)' if continued else '')
            budget=limit-_utf16_length(heading+':\n')
            if budget<2:raise ValueError('Section heading too long')
            piece,remainder=_take_text(text,budget)
            block=_quote(heading,piece)
            combined=(current+'\n\n' if current else '')+block
            if _visible_length(combined)>limit:
                if current:parts.append(current)
                current=block
            else:
                current=combined
            text=remainder
            continued=True
    if current:parts.append(current)
    if any(_visible_length(part)>limit for part in parts):
        raise ValueError('Message too long')
    return parts


def _poster_url(value):
    url=html.unescape(str(value or '').strip())
    if url.startswith('//'):url='https:'+url
    return url if urlparse(url).scheme in ('https','http') and urlparse(url).hostname else ''


def _poster_error(stage,url,exc):
    detail=str(exc)
    for name in ['TELEGRAM_TOKEN','SUPABASE_SECRET_KEY']+[f'GEMINI_KEY_{i}' for i in range(1,15)]:
        secret=os.getenv(name)
        if secret:detail=detail.replace(secret,'[REDACTED]')
    print(f'POSTER {stage} | host={urlparse(url).hostname} | {type(exc).__name__}: {detail[:250]}',flush=True)


def make_small_poster(url,idx):
    url=_poster_url(url)
    if not url:return None
    parsed=urlparse(url)
    # A normal browser referer can be required by a host's hotlink policy.
    for with_referer in (False,True):
        headers={'User-Agent':'Mozilla/5.0','Accept':'image/avif,image/webp,image/*,*/*;q=0.8'}
        if with_referer:headers['Referer']=f'{parsed.scheme}://{parsed.netloc}/'
        try:
            with requests.get(url,timeout=(4,8),headers=headers,stream=True) as response:
                print(f'POSTER HTTP | host={parsed.hostname} | status={response.status_code} | type={response.headers.get('Content-Type','unknown')} | referer={with_referer}',flush=True)
                if response.status_code==403 and not with_referer:continue
                response.raise_for_status()
                raw=bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw)>10*1024*1024:raise ValueError('Image download exceeds 10 MiB')
            with Image.open(io.BytesIO(raw)) as source:
                if source.width*source.height>25_000_000:raise ValueError('Image dimensions too large')
                image=ImageOps.exif_transpose(source).convert('RGB')
                image.thumbnail((360,540))
                output=io.BytesIO();image.save(output,'JPEG',quality=82,optimize=True)
                output.seek(0)
            return InputFile(output,filename=f'poster_{idx}.jpg')
        except Exception as exc:
            _poster_error('DOWNLOAD FAILED',url,exc)
            return None
    return None


async def _send_movie_card(chat_id,context,idx,x):
    header=_card_header(idx,x)
    sections=_card_sections(x)
    full=build_caption(idx,x)
    if _visible_length(full)<=1024:
        photo_caption=full
        remaining=[]
    elif _visible_length(header)<=1024:
        photo_caption=header
        remaining=_message_parts('',sections)
    else:
        photo_caption='<b>'+esc(str(x.get('title') or 'Unknown'))+'</b>'
        if _visible_length(photo_caption)>1024:photo_caption=''
        remaining=_message_parts(header,sections)
    url=_poster_url(x.get('poster_url'))
    photo_sent=False
    if url:
        # Telegram can sometimes retrieve a public URL that this server cannot fetch.
        try:
            await context.bot.send_photo(chat_id=chat_id,photo=url,caption=photo_caption,
                parse_mode='HTML',read_timeout=20,write_timeout=30,connect_timeout=10,pool_timeout=10)
            photo_sent=True
            print('POSTER SENT | route=telegram_url',flush=True)
        except Exception as exc:
            _poster_error('TELEGRAM URL FAILED',url,exc)
        if not photo_sent:
            poster_file=await asyncio.to_thread(make_small_poster,url,idx)
            if poster_file:
                try:
                    await context.bot.send_photo(chat_id=chat_id,photo=poster_file,caption=photo_caption,
                        parse_mode='HTML',read_timeout=20,write_timeout=30,connect_timeout=10,pool_timeout=10)
                    photo_sent=True
                    print('POSTER SENT | route=uploaded_jpeg',flush=True)
                except Exception as exc:
                    _poster_error('FILE UPLOAD FAILED',url,exc)
    texts=remaining if photo_sent else _message_parts(header,sections)
    for index,text in enumerate(texts):
        kwargs={}
        if not photo_sent and url and index==len(texts)-1:
            kwargs['reply_markup']=InlineKeyboardMarkup([[InlineKeyboardButton('🖼 دیدن پوستر',url=url)]])
        await context.bot.send_message(chat_id=chat_id,text=text,parse_mode='HTML',
            disable_web_page_preview=True,**kwargs)


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

        await _send_movie_card(chat_id,context,idx+1,x)

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


        sort_mode = states[uid].get("sort_mode", "default")
        results = order_final_results(results, sort_mode)

        states[uid] = {
            "sort_mode": sort_mode,

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


    # Presentation preference stays out of the query and Gemini prompts.
    if data and data.startswith("sort:"):
        parts = data.split(":")
        if (len(parts) != 3 or st.get("stage") != "choose_sort"
                or parts[1] != st.get("sort_token") or parts[2] not in SORT_OPTIONS):
            await q.answer("این دکمه دیگر فعال نیست.")
            return
        mode = parts[2]
        st.update(stage="confirm", sort_mode=mode)
        st.pop("sort_token", None)
        await q.answer()
        await q.message.reply_text(
            "درخواست شما:\n\n" + st["query"]
            + "\n\nترتیب نمایش: " + SORT_OPTIONS[mode]
            + "\n\nآیا این پیام نهایی شماست کاکه گیان؟؟",
            reply_markup=keyboard(
                [("تأیید و جستجو بژی کوردیستان✅", "confirm")],
                [("لغو ❌", "cancel")]
            )
        )
        return

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
