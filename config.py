import os
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ["MONGO_URI"]
DB_NAME = os.environ.get("DB_NAME", "referbot")
ADMINS = [int(x) for x in os.environ.get("ADMINS", "").replace(",", " ").split()]
STORAGE_CHANNEL = int(os.environ["STORAGE_CHANNEL"])
LOG_CHANNEL = int(os.environ.get("LOG_CHANNEL", "0") or 0)
PORT = int(os.environ.get("PORT", "8080"))
