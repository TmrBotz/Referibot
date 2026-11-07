import time
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

import config

_client = AsyncIOMotorClient(config.MONGO_URI)
db = _client[config.DB_NAME]

users = db.users          # {_id, name, active, has_delivered, products: {p<id>: {...}}}
products = db.products    # {_id, name, refers_required, msg_ids, fsubs, delete_min, protect, paused}
refers = db.refers        # {referrer, referred, product_id, status: pending|counted|left}
jr = db.join_requests     # {user_id, chat_id}
jobs = db.delete_jobs     # {chat_id, msg_ids, notice_id, delete_at}
meta = db.meta


def now():
    return datetime.now(timezone.utc)


async def init_indexes():
    await refers.create_index([("referred", 1), ("product_id", 1)], unique=True)
    await refers.create_index([("referrer", 1), ("product_id", 1), ("status", 1)])
    await jr.create_index([("user_id", 1), ("chat_id", 1)], unique=True)
    await jobs.create_index("delete_at")
    await users.create_index("active")
    await products.create_index("fsubs.chat_id")


async def next_pid():
    d = await meta.find_one_and_update(
        {"_id": "pid"}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    return d["n"]


# ---- settings (cached 10s) ----
_cfg = {"t": 0.0, "v": None}


async def get_settings():
    if _cfg["v"] and time.monotonic() - _cfg["t"] < 10:
        return _cfg["v"]
    d = await meta.find_one({"_id": "settings"}) or {}
    _cfg["v"] = {"delete_min": d.get("delete_min", 10), "refer_expiry": d.get("refer_expiry", False),
                 "welcome": d.get("welcome")}
    _cfg["t"] = time.monotonic()
    return _cfg["v"]


async def set_setting(key, value):
    await meta.update_one({"_id": "settings"}, {"$set": {key: value}}, upsert=True)
    _cfg["t"] = 0.0


# ---- product cache (5s) ----
_pc = {}


async def get_product(pid):
    c = _pc.get(pid)
    if c and time.monotonic() - c[0] < 5:
        return c[1]
    p = await products.find_one({"_id": pid})
    if p:
        _pc[pid] = (time.monotonic(), p)
    return p


def drop_cache(pid):
    _pc.pop(pid, None)
