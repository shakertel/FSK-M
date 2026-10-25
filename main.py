import threading
from flask import Flask
from telegram_bot import run_bot
from dotenv import load_dotenv
load_dotenv()

# =========================
# Render Health Server
# =========================

web = Flask(__name__)


@web.route("/")
def home():
    return "FSK bot is alive 🎬"


@web.route("/health")
def health():
    return {
        "status": "ok",
        "service": "FSK bot"
    }


def start_web():
    # Render expects an open port
    web.run(
        host="0.0.0.0",
        port=10000
    )


# =========================
# Start
# =========================

if __name__ == "__main__":

    # Fake HTTP server for Render Web Service
    threading.Thread(
        target=start_web,
        daemon=True
    ).start()

    # Telegram bot
    run_bot()
