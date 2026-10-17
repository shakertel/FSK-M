import asyncio
import html
import io
import logging
import os
import threading
import time
import uuid
import signal
import webhook_bridge
from urllib.parse import urlparse
import requests
from PIL import Image, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from engine import FSKEngine, SearchCancelled, SearchIncomplete

log = logging.getLogger(__name__)
PAGE_SIZE = 3
WELCOME = ('🎬 FSK M — سڵاو، بەخێر بێیت!\n\n'
           'حس‌وحال و فیلمی که دنبالش هستید را بنویسید؛ مثلاً:\n'
           '«شکست عشقی خوردم، یه فیلم مرتبط می‌خوام که سرگرمم کنه و حالم بهتر شه.»')
BUSY_MESSAGE = 'فعلا مشغول است.'
engine = None
states = {}
poster_ids = {}
current_job = None


def busy():
    return current_job is not None


def reserve_job(key, st):
    # Called synchronously by the event-loop handler before its first await.
    global current_job
    if current_job is not None:
        return False
    current_job = (key, st)
    return True


def release_job(st):
    global current_job
    if current_job is not None and current_job[1] is st:
        current_job = None


def identity(update):
    return (update.effective_chat.id, update.effective_user.id)


def button(text, action, st, position=None):
    suffix = '' if position is None else ':'+str(position)
    return InlineKeyboardButton(text, callback_data=f'{action}:{st["session"]}{suffix}')


def keyboard(*rows):
    return InlineKeyboardMarkup(list(rows))


def clear(key):
    old = states.pop(key, None)
    if old and old.get('event'):
        old['event'].set()


def new_state(key, **kwargs):
    clear(key)
    st = {'session':uuid.uuid4().hex[:12], 'updated':time.monotonic(),
          'page_lock':asyncio.Lock(), **kwargs}
    states[key] = st
    return st


def active(key, st):
    return states.get(key) is st and not (st.get('event') and st['event'].is_set())


def clip(value, units):
    s = str(value or '')
    if len(s.encode('utf-16-le'))//2 <= units:
        return s
    return s.encode('utf-16-le')[:max(0,units-1)*2].decode('utf-16-le',errors='ignore')+'…'


def clean_list(value):
    return ' '.join((', '.join(str(v) for v in value if v) if isinstance(value,(list,tuple)) else str(value or '')).split())


def compact_info(r):
    parts = [clean_list(r.get('countries')),str(r.get('year') or ''),clean_list(r.get('genres'))]
    if r.get('imdb_rating'):
        parts.append(str(r['imdb_rating'])+' IMDb')
    if r.get('runtime_minutes'):
        parts.append(str(r['runtime_minutes'])+' دقیقه')
    actors = clean_list(r.get('actors'))
    directors = clean_list(r.get('directors_creators'))
    if actors:
        parts.append(actors)
    if directors:
        parts.append('کارگردان: '+directors)
    return ' • '.join(' '.join(x.split()) for x in parts if x)


def build_caption(index, r):
    # Cut plain text first, then escape: never slice HTML entities or tags.
    title = clip(f'{index}. '+('⭐ ' if r.get('star') else '')+str(r.get('title') or 'Unknown'), 110)
    parts = [compact_info(r)]
    for label, field, limit in [('📖','description',245),('🎯','reason',245),
                                 ('⚠️','warning',100),('⭐','star_reason',100)]:
        if r.get(field) and (field != 'star_reason' or r.get('star')):
            parts.append(label+' '+clip(r[field],limit))
    body = clip('\n\n'.join(p for p in parts if p), 3800)
    return '<b>'+html.escape(title)+'</b>\n\n'+html.escape(body)


def links(r):
    buttons = []
    for label, field in [('IMDb','imdb_url'),('صفحه فیلم','digimoviez_url'),('پوستر','poster_url')]:
        url = r.get(field)
        if isinstance(url,str) and urlparse(url).scheme == 'https' and urlparse(url).hostname:
            buttons.append(InlineKeyboardButton(label,url=url))
    return keyboard(buttons) if buttons else None


def safe_error(exc):
    message=str(exc)
    for name in ['TELEGRAM_TOKEN','SUPABASE_SECRET_KEY']+[f'GEMINI_KEY_{i}' for i in range(1,15)]:
        value=os.getenv(name)
        if value:message=message.replace(value,'[REDACTED]')
    return clip(message,250)


def make_small_poster(url):
    if not isinstance(url,str) or urlparse(url).scheme not in ('https','http'):
        return None
    parsed=urlparse(url)
    base_headers={'User-Agent':'Mozilla/5.0','Accept':'image/avif,image/webp,image/*,*/*;q=0.8'}
    for with_referer in (False,True):
        headers=dict(base_headers)
        if with_referer:headers['Referer']=f'{parsed.scheme}://{parsed.netloc}/'
        try:
            with requests.get(url,timeout=(4,6),stream=True,headers=headers) as response:
                status=response.status_code
                log.info('POSTER_HTTP | host=%s | status=%s | referer=%s',parsed.hostname,status,with_referer)
                if status==403 and not with_referer:
                    continue
                response.raise_for_status()
                data=bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data)>8*1024*1024:raise ValueError('Poster too large')
            with Image.open(io.BytesIO(data)) as img:
                if img.width*img.height>25_000_000:raise ValueError('Poster dimensions too large')
                image=ImageOps.exif_transpose(img).convert('RGB')
                image.thumbnail((360,540))
                output=io.BytesIO();image.save(output,format='JPEG',quality=82)
                return output.getvalue()
        except Exception as exc:
            log.warning('POSTER_DOWNLOAD_FAIL | host=%s | type=%s | detail=%s',
                parsed.hostname,type(exc).__name__,safe_error(exc))
            return None
    return None


async def start(update, context):
    new_state(identity(update), stage='awaiting_query')
    await update.effective_message.reply_text(WELCOME)


async def cancel(update, context):
    new_state(identity(update), stage='awaiting_query')
    await update.effective_message.reply_text('جستجو لغو شد. درخواست جدیدتان را بفرستید.')


async def text_handler(update, context):
    key = identity(update)
    log.info('USER_MESSAGE | chat=%s | busy=%s',key[0],busy())
    st = states.get(key)
    if st and st.get('stage') == 'searching':
        await update.effective_message.reply_text(BUSY_MESSAGE,
            reply_markup=keyboard([button('لغو ❌','cancel',st)]))
        return
    query = (update.effective_message.text or '').strip()
    if not query:
        return
    if len(query)>4000:
        await update.effective_message.reply_text('درخواست را در کمتر از ۴۰۰۰ کاراکتر بنویسید.')
        return
    st = new_state(key, stage='confirm', query=query)
    await update.effective_message.reply_text('درخواست شما:\n\n'+query+'\n\nجستجو کنم؟',
        reply_markup=keyboard([button('تأیید و جستجو ✅','confirm',st)],
                              [button('لغو ❌','cancel',st)]))


async def send_page(key, st, context, expected_pos):
    async with st['page_lock']:
        if not active(key,st) or st.get('stage')!='results' or st['pos']!=expected_pos:
            return
        end = min(st['pos']+PAGE_SIZE,len(st['results']))
        for idx in range(st['pos'],end):
            if not active(key,st):
                return
            row = st['results'][idx]
            caption = build_caption(idx+1,row)
            url = html.unescape(str(row.get('poster_url') or '').strip())
            sent = None
            separate_text = len(html.unescape(caption).encode('utf-16-le'))//2 > 1000
            caption_args=dict(caption=(clip(row.get('title'),110) if separate_text else caption),
                parse_mode=(None if separate_text else 'HTML'),reply_markup=links(row))
            # Telegram may fetch a public image even when this server gets HTTP 403.
            attempts=[]
            if poster_ids.get(url):attempts.append(('cached_file',poster_ids[url]))
            if isinstance(url,str) and urlparse(url).scheme in ('https','http'):
                attempts.append(('telegram_url',url))
            for route,payload in attempts:
                if not active(key,st):return
                try:
                    sent=await context.bot.send_photo(chat_id=key[0],photo=payload,
                        **caption_args,read_timeout=20,write_timeout=30,connect_timeout=10,pool_timeout=10)
                    log.info('POSTER_SENT | route=%s | title=%s',route,row.get('title'))
                    break
                except Exception as exc:
                    if route=='cached_file':poster_ids.pop(url,None)
                    log.warning('POSTER_SEND_FAIL | route=%s | type=%s | detail=%s',
                        route,type(exc).__name__,safe_error(exc))
            if sent is None:
                photo=await asyncio.to_thread(make_small_poster,url)
                if not active(key,st):return
                if photo:
                    try:
                        sent=await context.bot.send_photo(chat_id=key[0],
                            photo=InputFile(photo,filename='poster.jpg'),**caption_args,
                            read_timeout=20,write_timeout=30,connect_timeout=10,pool_timeout=10)
                        log.info('POSTER_SENT | route=uploaded_file | title=%s',row.get('title'))
                    except Exception as exc:
                        log.warning('POSTER_UPLOAD_FAIL | type=%s | detail=%s',type(exc).__name__,safe_error(exc))
            if sent is not None and sent.photo:
                if len(poster_ids)>=2000:poster_ids.pop(next(iter(poster_ids)))
                poster_ids[url]=sent.photo[-1].file_id
            if sent is None or separate_text:
                await context.bot.send_message(chat_id=key[0],text=caption,parse_mode='HTML',
                    disable_web_page_preview=True,reply_markup=links(row))
            # Advance after each successful send, retaining progress if a later send fails.
            st['pos']=idx+1
        st['updated']=time.monotonic()
        if active(key,st):
            rows = []
            if end<len(st['results']):
                rows.append([button('۳ تای بعدی ▶️','next',st,end)])
            rows.append([button('جستجوی جدید 🔎','new',st),button('کافیه ✅','enough',st)])
            await context.bot.send_message(chat_id=key[0],
                text=f'{end} مورد از {len(st["results"])} پیشنهاد نمایش داده شد.',reply_markup=keyboard(*rows))


async def search_task(key, st, context):
    event = st['event']
    started=time.monotonic()
    last_stage=['starting']
    def progress(stage):
        last_stage[0]=stage
        log.info('SEARCH_STAGE | session=%s | stage=%s',st['session'],stage)
    async def watchdog_log():
        while True:
            await asyncio.sleep(30)
            log.info('SEARCH_RUNNING | session=%s | stage=%s | elapsed=%.0fs',
                st['session'],last_stage[0],time.monotonic()-started)
    log.info('SEARCH_BEGIN | session=%s',st['session'])
    pulse=asyncio.create_task(watchdog_log())
    try:
        results = await asyncio.to_thread(engine.search,st['query'],event,progress)
        release_job(st)
        if not active(key,st):
            return
        st.update(stage='results',results=results,pos=0)
        if not results:
            st['stage']='awaiting_query'
            await context.bot.send_message(chat_id=key[0],text='جستجو کامل شد؛ پیشنهاد مناسبی پیدا نشد. درخواست را تغییر دهید.')
            return
        log.info('SEARCH_DONE | session=%s | results=%s | seconds=%.1f',st['session'],len(results),time.monotonic()-started)
        await send_page(key,st,context,0)
    except SearchCancelled:
        pass
    except Exception as exc:
        release_job(st)
        is_current = states.get(key) is st
        event.set()
        if is_current:
            st['stage']='awaiting_query'
            if isinstance(exc,(SearchIncomplete,asyncio.TimeoutError)):
                message='جستجو کامل نشد؛ پاسخ‌های موفق کش شدند. همان درخواست را دوباره بفرستید تا ادامه دهد.'
            else:
                message='جستجو با خطا متوقف شد. درخواست را دوباره بفرستید؛ پاسخ‌های موفق محفوظ‌اند.'
            log.warning('SEARCH_STOPPED | session=%s | type=%s | detail=%s',st['session'],type(exc).__name__,safe_error(exc))
            await context.bot.send_message(chat_id=key[0],text=message)
    finally:
        release_job(st)
        log.info('SEARCH_RELEASED | session=%s | seconds=%.1f',st['session'],time.monotonic()-started)
        pulse.cancel()
        try: await pulse
        except asyncio.CancelledError: pass


async def buttons(update, context):
    q = update.callback_query
    key = identity(update)
    parts = (q.data or '').split(':')
    st = states.get(key)
    if len(parts)<2 or not st or parts[1]!=st['session']:
        await q.answer('این جستجو دیگر فعال نیست.')
        await q.message.reply_text('سرور دوباره راه‌اندازی شده یا این جستجو تمام شده؛ درخواستتان را دوباره بفرستید.')
        return
    action=parts[0]
    log.info('BUTTON | action=%s | session=%s',action,st['session'])
    if action=='confirm' and st['stage']=='confirm':
        if not reserve_job(key,st):
            await q.answer(BUSY_MESSAGE)
            await q.message.reply_text(BUSY_MESSAGE)
            return
    try:
        await q.answer()
    except Exception:
        if action=='confirm':
            release_job(st)
        raise
    if action in ('cancel','new','enough'):
        new_state(key,stage='awaiting_query')
        await q.message.reply_text(WELCOME if action=='new' else 'باشه؛ درخواست جدیدتان را بفرستید.')
    elif action=='confirm' and st['stage']=='confirm':
        st.update(stage='searching',event=threading.Event())
        try:
            await q.message.edit_text('🔎 درخواست ثبت شد.',
                reply_markup=keyboard([button('لغو جستجو ❌','cancel',st)]))
            context.application.create_task(search_task(key,st,context),update=update)
        except Exception:
            release_job(st)
            st['stage']='confirm'
            raise
    elif action=='next' and st['stage']=='results' and len(parts)==3:
        # Launch separately so downloading posters never blocks /cancel.
        context.application.create_task(send_page(key,st,context,int(parts[2])),update=update)


async def error_handler(update, context):
    log.error('Telegram operation failed: %s',type(context.error).__name__)


async def serve_webhook(app,url):
    stop=asyncio.Event()
    loop=asyncio.get_running_loop()
    for signum in (signal.SIGINT,signal.SIGTERM):
        try:loop.add_signal_handler(signum,stop.set)
        except (NotImplementedError,RuntimeError):pass
    async with app:
        await app.start()
        webhook_bridge.attach(app,loop)
        try:
            await app.bot.set_webhook(url=url.rstrip('/')+'/telegram',
                secret_token=webhook_bridge.secret(),allowed_updates=['message','callback_query'],
                max_connections=4,drop_pending_updates=False)
            log.info('BOT_READY | mode=webhook | engine_movies=%s',len(engine.movies))
            await stop.wait()
        finally:
            webhook_bridge.detach()
            for state in states.values():
                if state.get('event'):state['event'].set()
            await app.stop()


async def polling_ready(app):
    log.info('BOT_READY | mode=polling | engine_movies=%s',len(engine.movies))


def run_bot():
    global engine
    token=os.environ['TELEGRAM_TOKEN']
    log.info('DATABASE_LOADING')
    engine=FSKEngine()
    log.info('DATABASE_READY | movies=%s',len(engine.movies))
    url=os.getenv('WEBHOOK_URL') or os.getenv('RENDER_EXTERNAL_URL','')
    use_webhook=bool(url) and os.getenv('BOT_MODE','auto').lower()!='polling'
    builder=Application.builder().token(token).connect_timeout(15).read_timeout(40).write_timeout(30)
    if use_webhook:
        builder=builder.updater(None)
    else:
        builder=builder.post_init(polling_ready)
    app=builder.build()
    app.add_handler(CommandHandler('start',start))
    app.add_handler(CommandHandler('cancel',cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,text_handler))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_error_handler(error_handler)
    if use_webhook:
        if not url.startswith('https://'):
            raise RuntimeError('WEBHOOK_URL must start with https://')
        log.info('BOT_STARTING | mode=webhook')
        asyncio.run(serve_webhook(app,url))
    else:
        log.info('BOT_STARTING | mode=polling')
        app.run_polling(drop_pending_updates=False,bootstrap_retries=3)
