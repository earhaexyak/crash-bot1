import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("8275263205:AAGVDQzrx2iAjfywohTFlWCgLRQy114aaeI", "8275263205:AAGVDQzrx2iAjfywohTFlWCgLRQy114aaeI")

# Telegram user IDs allowed to use admin commands inside the bot (comma-separated)
ADMIN_IDS = {
    int(x) for x in os.getenv("7993513720", "").replace(" ", "").split(",") if x
}

STARTING_BALANCE = float(os.getenv("STARTING_BALANCE", "1000"))
MIN_BET = float(os.getenv("MIN_BET", "10"))
MAX_BET = float(os.getenv("MAX_BET", "100000"))
CURRENCY_NAME = os.getenv("CURRENCY_NAME", "Coin")   # virtual currency label shown to users

# Mini App (the in-Telegram web page). MUST be a public HTTPS url - Telegram
# refuses to open WebApp buttons pointing at http:// or localhost.
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.example/")
WEBAPP_API_PORT = int(os.getenv("WEBAPP_API_PORT", "8090"))

# Flask admin panel
ADMIN_PANEL_USER = os.getenv("ADMIN_PANEL_USER", "admin")
ADMIN_PANEL_PASS = os.getenv("ADMIN_PANEL_PASS", "change_me_now")
ADMIN_PANEL_SECRET = os.getenv("ADMIN_PANEL_SECRET", "change_this_flask_secret_key")
ADMIN_PANEL_PORT = int(os.getenv("ADMIN_PANEL_PORT", "8080"))
