from pyrogram import Client
from pyrogram.enums import ParseMode
import config

app = Client(
    "referbot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    parse_mode=ParseMode.HTML,
    in_memory=True,
)
