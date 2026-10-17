import logging
import threading
from flask import Flask, request
import webhook_bridge
from telegram_bot import run_bot

web=Flask(__name__)
web.config['MAX_CONTENT_LENGTH']=1024*1024


@web.route('/')
def home():
    return 'FSK M is alive 🎬'


@web.route('/health')
def health():
    return {'status':'ok','service':'FSK M'}


@web.post('/telegram')
def telegram_webhook():
    return webhook_bridge.receive(request.get_json(silent=True),
        request.headers.get('X-Telegram-Bot-Api-Secret-Token'))


def start_web():
    web.run(host='0.0.0.0',port=10000)


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',force=True)
    for name in ('httpx','httpcore','google_genai','google.genai','werkzeug'):
        logging.getLogger(name).setLevel(logging.WARNING)
    threading.Thread(target=start_web,daemon=True).start()
    logging.getLogger(__name__).info('FSK M START | Flask port=10000')
    run_bot()
