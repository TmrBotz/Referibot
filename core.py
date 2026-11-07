import asyncio
import html
import logging
from datetime import timedelta
from urllib.parse import quote

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, MessageNotModified, UserNotParticipant
from pyrogram.types import CallbackQuery
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KB

import config
from client import app
from db import get_product, get_settings, jobs, jr, now, products, refers, users

log = logging.getLogger("core")
BOT = {"username": ""}
MEDALS = ["🥇", "🥈", "🥉"]


# ---------------- helpers ----------------
def esc(t):
    return html.escape(str(t or ""))


def bar(n, need):
    need = max(need, 1)
    n = min(n, need)
    f = round(n / need * 10)
    return "▓" * f + "░" * (10 - f) + f" {n}/{need}"


def plink(pid):
    return f"https://t.me/{BOT['username']}?start=p_{pid}"


def rlink(pid, uid):
    return f"https://t.me/{BOT['username']}?start=r_{pid}_{uid}"


async def show(target, text, kb=None):
    """Edit if callback, reply if message."""
    try:
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=kb)
        else:
            await target.reply_text(text, reply_markup=kb)
    except MessageNotModified:
        pass


async def log_ch(text):
    if config.LOG_CHANNEL:
        try:
            await app.send_message(config.LOG_CHANNEL, text)
        except Exception:
            pass


# ---------------- fsub ----------------
async def is_joined(uid, f):
    try:
        m = await app.get_chat_member(f["chat_id"], uid)
        st = m.status
        if st == ChatMemberStatus.RESTRICTED:
            ok = bool(getattr(m, "is_member", True))
        else:
            ok = st not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
        if ok:
            return True
    except UserNotParticipant:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await is_joined(uid, f)
    except Exception as e:
        # bot admin issue etc. -> don't block users
        log.warning("fsub check failed %s: %s", f["chat_id"], e)
        return True
    if f["mode"] == "request":
        return bool(await jr.find_one({"user_id": uid, "chat_id": f["chat_id"]}))
    return False


async def missing_fsubs(uid, p):
    fs = p.get("fsubs", [])
    if not fs:
        return []
    res = await asyncio.gather(*(is_joined(uid, f) for f in fs))
    return [f for f, ok in zip(fs, res) if not ok]


# ---------------- refers / delivery ----------------
async def count_refs(pid, uid):
    return await refers.count_documents({"referrer": uid, "product_id": pid, "status": "counted"})


async def send_content(uid, p):
    sent = []
    ids = p["msg_ids"]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        while True:
            try:
                res = await app.copy_messages(
                    uid, config.STORAGE_CHANNEL, chunk, protect_content=bool(p.get("protect"))
                )
                break
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
        sent += [m.id for m in (res if isinstance(res, list) else [res])]

    mins = p.get("delete_min")
    if mins is None:
        mins = (await get_settings())["delete_min"]
    if mins and sent:
        n = await app.send_message(
            uid, f"⏳ Ye content <b>{mins} min</b> me delete ho jayega. Abhi save/forward kar lo."
        )
        await jobs.insert_one({
            "chat_id": uid, "msg_ids": sent, "notice_id": n.id,
            "delete_at": now() + timedelta(minutes=mins),
        })


async def try_deliver(uid, pid, fsub_ok=False, announce=False):
    p = await get_product(pid)
    if not p or p.get("paused"):
        return False
    u = await users.find_one({"_id": uid}, {f"products.p{pid}": 1})
    rec = ((u or {}).get("products") or {}).get(f"p{pid}")
    if not rec or rec.get("delivered"):
        return False
    if await count_refs(pid, uid) < p["refers_required"]:
        return False
    if not fsub_ok and await missing_fsubs(uid, p):
        return False

    # atomic claim -> only one delivery ever
    r = await users.update_one(
        {"_id": uid, f"products.p{pid}.delivered": {"$ne": True}},
        {"$set": {f"products.p{pid}.delivered": True, f"products.p{pid}.fsub_done": True, "has_delivered": True}},
    )
    if not r.modified_count:
        return False
    try:
        if announce:
            await app.send_message(uid, f"🎉 Refer complete! <b>{esc(p['name'])}</b> unlock ho gaya 👇")
        await send_content(uid, p)
        return True
    except Exception as e:
        log.error("delivery failed uid=%s pid=%s: %s", uid, pid, e)
        await users.update_one({"_id": uid}, {"$set": {f"products.p{pid}.delivered": False}})
        await log_ch(f"❌ Delivery failed\nuser: <code>{uid}</code>\nproduct: {pid}\n<code>{esc(e)}</code>")
        return False


async def on_fsub_done(uid, pid):
    """User finished fsub for this product -> count his referral for the referrer."""
    await users.update_one(
        {"_id": uid, f"products.p{pid}.fsub_done": {"$ne": True}},
        {"$set": {f"products.p{pid}.fsub_done": True}},
    )
    d = await refers.find_one_and_update(
        {"referred": uid, "product_id": pid, "status": {"$in": ["pending", "left"]}},
        {"$set": {"status": "counted", "counted_at": now()}},
    )
    if not d:
        return
    ref = d["referrer"]
    p = await get_product(pid)
    need = p["refers_required"]
    n = await count_refs(pid, ref)
    if await try_deliver(ref, pid, announce=True):
        return
    msg = f"🎉 Ek refer complete!\n<b>{esc(p['name'])}</b>: {bar(n, need)}"
    if n >= need:
        msg += "\n\nTarget poora! Content ke liye product ke channels join karke link dobara kholo."
    try:
        await app.send_message(ref, msg)
    except Exception:
        pass


async def advance(uid, pid):
    """Returns (state, product, extra). state: off | fsub | delivered | already | refer"""
    p = await get_product(pid)
    if not p or p.get("paused"):
        return "off", p, None
    miss = await missing_fsubs(uid, p)
    if miss:
        return "fsub", p, miss
    await on_fsub_done(uid, pid)
    if await try_deliver(uid, pid, fsub_ok=True):
        return "delivered", p, None
    u = await users.find_one({"_id": uid}, {f"products.p{pid}": 1})
    rec = ((u or {}).get("products") or {}).get(f"p{pid}") or {}
    if rec.get("delivered"):
        return "already", p, None
    return "refer", p, await count_refs(pid, uid)


# ---------------- user screens ----------------
def fsub_kb(pid, miss):
    rows, row = [], []
    for f in miss:
        icon = "📨" if f["mode"] == "request" else "📢"
        row.append(B(f"{icon} {f['title'][:24]}", url=f["link"]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([B("✅ Joined", callback_data=f"chk:{pid}")])
    return KB(rows)


def refer_view(p, uid, n):
    pid, need = p["_id"], p["refers_required"]
    link = rlink(pid, uid)
    text = (
        f"🎁 <b>{esc(p['name'])}</b>\n\n"
        f"👥 Refer: {bar(n, need)}\n\n"
        f"{need} dost ko apna link bhejo. Wo product ke channels join karte hi refer count hoga, "
        f"aur target poora hote hi content apne aap mil jayega.\n\n"
        f"🔗 <code>{link}</code>"
    )
    share = f"https://t.me/share/url?url={quote(link)}&text={quote('🎁 Free ' + p['name'] + ' paao! Is link se start karo 👇')}"
    kb = KB([
        [B("📤 Share", url=share)],
        [B("🔄 Refresh", callback_data=f"ref:{pid}"), B("🏆 Leaderboard", callback_data=f"lb:{pid}")],
    ])
    return text, kb


async def flow(target, uid, pid):
    st, p, x = await advance(uid, pid)
    if st == "off":
        await show(target, "⏸ Ye product abhi available nahi hai.")
    elif st == "fsub":
        txt = (
            f"🔒 <b>{esc(p['name'])}</b>\n\n"
            f"Content paane ke liye pehle neeche ke <b>{len(x)}</b> channel join karo, fir ✅ Joined dabao.\n"
            f"📨 = Request mode (sirf join request bhejna kaafi hai)."
        )
        await show(target, txt, fsub_kb(pid, x))
    elif st == "refer":
        t, kb = refer_view(p, uid, x)
        await show(target, t, kb)
    elif st == "delivered":
        await show(target, f"🎉 <b>{esc(p['name'])}</b> unlock ho gaya! Content bhej diya gaya 👇")
    else:
        await show(target, f"✅ <b>{esc(p['name'])}</b> aap pehle hi le chuke ho (ek baar hi milta hai).")
    return st


async def leaderboard_text(pid):
    p = await get_product(pid)
    if not p:
        return "❌ Product nahi mila."
    rows = await refers.aggregate([
        {"$match": {"product_id": pid, "status": "counted"}},
        {"$group": {"_id": "$referrer", "c": {"$sum": 1}}},
        {"$sort": {"c": -1}},
        {"$limit": 10},
    ]).to_list(10)
    if not rows:
        return f"🏆 <b>{esc(p['name'])}</b>\n\nAbhi koi refer nahi hua."
    ids = [r["_id"] for r in rows]
    names = {u["_id"]: u.get("name", "User") async for u in users.find({"_id": {"$in": ids}}, {"name": 1})}
    lines = []
    for i, r in enumerate(rows):
        medal = MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{medal} {esc(names.get(r['_id'], 'User'))[:20]} — <b>{r['c']}</b>")
    return f"🏆 <b>{esc(p['name'])}</b> — Top Referrers\n\n" + "\n".join(lines)


# ---------------- delete timer loop ----------------
async def delete_loop():
    while True:
        try:
            due = await jobs.find({"delete_at": {"$lte": now()}}).limit(100).to_list(100)
            for j in due:
                try:
                    ids = j["msg_ids"]
                    for i in range(0, len(ids), 100):
                        await app.delete_messages(j["chat_id"], ids[i:i + 100])
                    if j.get("notice_id"):
                        await app.edit_message_text(j["chat_id"], j["notice_id"], "🗑 Content delete ho gaya.")
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                    continue
                except Exception as e:
                    log.debug("delete job err: %s", e)
                await jobs.delete_one({"_id": j["_id"]})
        except Exception as e:
            log.error("delete_loop: %s", e)
        await asyncio.sleep(30)
