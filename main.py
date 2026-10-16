import logging
from telegram_bot import run_bot

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    # HTTP request URLs can include the Telegram token.
    for name in ('httpx','httpcore','google_genai','google.genai'):
        logging.getLogger(name).setLevel(logging.WARNING)
    run_bot()
