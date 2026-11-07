import time

from pymongo.errors import DuplicateKeyError
from pyrogram import filters
from pyrogram.enums import ChatMemberStatus

from ui import B, KB

import core
from client import app
from core import DEFAULT_WELCOME, bar, esc, flow, leaderboard_text, show
from db import get_product, get_settings, jr, now, products, refers, users

_last = {}


def throttled(uid, gap=1.5):
    t = time.monotonic()
    if t - _last.get(uid, 0) < gap:
        return True
    if len(_last) > 20000:
        _last.clear()
    _last[uid] = t
    return False


async def touch_user(u):
    await users.update_one(
        {"_id": u.id},
        {"$set": {"name": u.first_name or "User", "active": True},
         "$setOnInsert": {"joined": now(), "products": {}}},
        upsert=True,
    )


async def register(uid, pid, ref):
    """Create per-product record (first time only) and a pending refer."""
    valid = None
    if ref and ref != uid and await users.find_one({"_id": ref, f"products.p{pid}": {"$exists": True}}, {"_id": 1}):
        valid = ref
    r = await users.update_one(
        {"_id": uid, f"products.p{pid}": {"$exists": False}},
        {"$set": {f"products.p{pid}": {"referred_by": valid, "fsub_done": False, "delivered": False, "at": now()}}},
    )
    if r.modified_count and valid:
        try:
            await refers.insert_one({"referrer": valid, "referred": uid, "product_id": pid,
                                     "status": "pending", "at": now()})
        except DuplicateKeyError:
            pass


@app.on_message(filters.command("start") & filters.private)
async def start(c, m):
    uid = m.from_user.id
    await touch_user(m.from_user)
    arg = m.command[1] if len(m.command) > 1 else ""

    if arg.startswith(("p_", "r_")):
        parts = arg.split("_")
        try:
            pid = int(parts[1])
            ref = int(parts[2]) if arg.startswith("r_") and len(parts) > 2 else None
        except (ValueError, IndexError):
            return await m.reply_text("❌ Invalid link.")
        p = await get_product(pid)
        if not p or p.get("paused"):
            return await m.reply_text("⏸ Ye product abhi available nahi hai.")
        await register(uid, pid, ref)
        return await flow(m, uid, pid)

    u = await users.find_one({"_id": uid}, {"products": 1})
    rows = []
    for k in list((u or {}).get("products") or {})[:10]:
        p = await get_product(int(k[1:]))
        if p:
            rows.append([B(f"🎁 {p['name'][:30]}", callback_data=f"ref:{p['_id']}")])
    name = esc(m.from_user.first_name)
    welcome = (await get_settings()).get("welcome") or DEFAULT_WELCOME
    extra = "\n\n📂 <b>Aapke products</b>" if rows else ""
    kb = KB(rows) if rows else None
    try:
        await m.reply_text(welcome.replace("{name}", name) + extra, reply_markup=kb)
    except Exception:
        await m.reply_text(DEFAULT_WELCOME.replace("{name}", name) + extra, reply_markup=kb)


@app.on_callback_query(filters.regex(r"^(chk|ref):(\d+)$"))
async def flow_cb(c, q):
    if throttled(q.from_user.id):
        return await q.answer("⏳ Thoda ruko...")
    kind, pid = q.matches[0].group(1), int(q.matches[0].group(2))
    uid = q.from_user.id
    await touch_user(q.from_user)
    await register(uid, pid, None)
    st = await flow(q, uid, pid, fast=(kind == "ref"))
    if st == "fsub" and kind == "chk":
        await q.answer("❌ Abhi bhi kuch channels join nahi kiye.", show_alert=True)
    else:
        await q.answer()


@app.on_callback_query(filters.regex(r"^lb:(\d+)$"))
async def lb_cb(c, q):
    pid = int(q.matches[0].group(1))
    await show(q, await leaderboard_text(pid), KB([[B("⬅️ Back", callback_data=f"ref:{pid}")]]))
    await q.answer()


# ---------------- channel events ----------------
@app.on_chat_join_request()
async def join_request(c, req):
    if await products.count_documents({"fsubs.chat_id": req.chat.id}, limit=1):
        await jr.update_one({"user_id": req.from_user.id, "chat_id": req.chat.id},
                            {"$set": {"at": now()}}, upsert=True)


@app.on_chat_member_updated()
async def member_updated(c, upd):
    """Refer expiry: referred user left a fsub channel -> reduce referrer's count (toggle in settings)."""
    if not (await get_settings())["refer_expiry"]:
        return
    old, new = upd.old_chat_member, upd.new_chat_member
    if old is None and new is None:
        return
    gone = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    left = (new is None) or (new.status in gone)
    if not left or (old is not None and old.status in gone):
        return
    user = (new or old).user
    if user.is_bot:
        return
    chat = upd.chat.id
    pids = [p["_id"] async for p in products.find({"fsubs.chat_id": chat}, {"_id": 1})]
    if not pids:
        return
    await jr.delete_many({"user_id": user.id, "chat_id": chat})
    for pid in pids:
        d = await refers.find_one_and_update(
            {"referred": user.id, "product_id": pid, "status": "counted"},
            {"$set": {"status": "left"}},
        )
        if d:
            p = await get_product(pid)
            n = await core.count_refs(pid, d["referrer"])
            try:
                await app.send_message(
                    d["referrer"],
                    f"⚠️ Ek referred user ne channel leave kar diya.\n<b>{esc(p['name'])}</b>: {bar(n, p['refers_required'])}",
                )
            except Exception:
                pass
