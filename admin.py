import asyncio
import logging
import time
from datetime import timedelta

from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, InputUserDeactivated, PeerIdInvalid, UserIsBlocked
from pyrogram.types import InlineKeyboardButton as B
from pyrogram.types import InlineKeyboardMarkup as KB

import config
from client import app
from core import esc, leaderboard_text, log_ch, plink, show
from db import (drop_cache, get_settings, jobs, next_pid, now, products, refers,
                set_setting, users)

log = logging.getLogger("admin")
A = filters.user(config.ADMINS)
ST = {}      # admin wizard states
RUN = {}     # running broadcasts
TASKS = set()


def cb(pattern):
    return app.on_callback_query(filters.regex(pattern) & A)


CANCEL = KB([[B("❌ Cancel", callback_data="cancel")]])
DONE_KB = KB([[B("✅ Done", callback_data="cdone")], [B("❌ Cancel", callback_data="cancel")]])
HOME = KB([[B("🏠 Home", callback_data="adm")]])
PANEL = KB([
    [B("📦 Products", callback_data="apl:0"), B("📢 Broadcast", callback_data="bc")],
    [B("📊 Stats", callback_data="ast"), B("⚙️ Settings", callback_data="aset")],
])


async def del_storage(ids):
    for i in range(0, len(ids), 100):
        try:
            await app.delete_messages(config.STORAGE_CHANNEL, ids[i:i + 100])
        except Exception:
            pass


async def resolve_chat(m):
    ch = getattr(m, "forward_from_chat", None) or getattr(getattr(m, "forward_origin", None), "chat", None)
    if ch:
        return ch
    t = (m.text or "").strip()
    if not t:
        return None
    try:
        return await app.get_chat(int(t) if t.lstrip("-").isdigit() else t)
    except Exception:
        return None


async def build_fsub(chat, mode):
    me = await app.get_chat_member(chat.id, "me")
    if me.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        raise ValueError("Bot us channel me admin nahi hai")
    link = await app.create_chat_invite_link(chat.id, name="ReferBot", creates_join_request=(mode == "request"))
    return {"chat_id": chat.id, "title": chat.title or str(chat.id), "mode": mode, "link": link.invite_link}


# ================= panel =================
@app.on_message(filters.command("admin") & filters.private & A)
async def admin_cmd(c, m):
    ST.pop(m.from_user.id, None)
    await m.reply_text("🛠 <b>Admin Panel</b>", reply_markup=PANEL)


@cb(r"^(adm|cancel)$")
async def adm_cb(c, q):
    s = ST.pop(q.from_user.id, None)
    if s and s.get("items"):
        await del_storage([cid for _, cid in s["items"]])
    await show(q, "🛠 <b>Admin Panel</b>", PANEL)
    await q.answer()


# ================= products list / card =================
@cb(r"^apl:(\d+)$")
async def plist(c, q):
    page, per = int(q.matches[0].group(1)), 5
    total = await products.count_documents({})
    items = await products.find().sort("_id", 1).skip(page * per).limit(per).to_list(per)
    rows = [[B(f"{'⏸ ' if p.get('paused') else ''}#{p['_id']} {p['name'][:30]}", callback_data=f"pm:{p['_id']}")]
            for p in items]
    nav = []
    if page > 0:
        nav.append(B("⬅️", callback_data=f"apl:{page - 1}"))
    if (page + 1) * per < total:
        nav.append(B("➡️", callback_data=f"apl:{page + 1}"))
    if nav:
        rows.append(nav)
    rows += [[B("➕ Add Product", callback_data="aadd")], [B("🏠 Home", callback_data="adm")]]
    await show(q, f"📦 <b>Products</b> ({total})", KB(rows))
    await q.answer()


async def card(pid):
    p = await products.find_one({"_id": pid})
    if not p:
        return "❌ Product nahi mila.", HOME
    started = await users.count_documents({f"products.p{pid}": {"$exists": True}})
    delivered = await users.count_documents({f"products.p{pid}.delivered": True})
    counted = await refers.count_documents({"product_id": pid, "status": "counted"})
    fs = "\n".join(f"  • {esc(f['title'])} ({f['mode']})" for f in p["fsubs"]) or "  —"
    mins = p.get("delete_min")
    timer = "Global" if mins is None else ("Off" if mins == 0 else f"{mins} min")
    text = (
        f"📦 <b>#{pid} {esc(p['name'])}</b>"
        f"{'  ⏸ <i>paused</i>' if p.get('paused') else ''}\n\n"
        f"🎯 Refer target: <b>{p['refers_required']}</b>\n"
        f"📁 Content: <b>{len(p['msg_ids'])}</b> items\n"
        f"📢 FSub:\n{fs}\n"
        f"⏳ Delete timer: <b>{timer}</b>\n"
        f"🔒 Protect content: <b>{'ON' if p.get('protect') else 'OFF'}</b>\n\n"
        f"👥 Started: {started} | 🎁 Delivered: {delivered} | ✅ Refers: {counted}"
    )
    kb = KB([
        [B("✏️ Name", callback_data=f"pe:{pid}:name"), B("🎯 Refers", callback_data=f"pe:{pid}:refers")],
        [B("📦 Content", callback_data=f"pe:{pid}:content"), B("📢 FSub", callback_data=f"pe:{pid}:fsub")],
        [B("⏳ Timer", callback_data=f"pe:{pid}:timer"),
         B(f"🔒 Protect: {'ON' if p.get('protect') else 'OFF'}", callback_data=f"ptog:{pid}:protect")],
        [B("▶️ Resume" if p.get("paused") else "⏸ Pause", callback_data=f"ptog:{pid}:pause"),
         B("🏆 Leaderboard", callback_data=f"plb:{pid}")],
        [B("🔗 Link", callback_data=f"plk:{pid}"), B("🗑 Delete", callback_data=f"pdel:{pid}")],
        [B("⬅️ Back", callback_data="apl:0")],
    ])
    return text, kb


@cb(r"^pm:(\d+)$")
async def pm(c, q):
    ST.pop(q.from_user.id, None)
    t, k = await card(int(q.matches[0].group(1)))
    await show(q, t, k)
    await q.answer()


@cb(r"^plk:(\d+)$")
async def plk(c, q):
    await q.message.reply_text(f"🔗 <code>{plink(int(q.matches[0].group(1)))}</code>")
    await q.answer()


@cb(r"^plb:(\d+)$")
async def plb(c, q):
    pid = int(q.matches[0].group(1))
    await show(q, await leaderboard_text(pid), KB([[B("⬅️ Back", callback_data=f"pm:{pid}")]]))
    await q.answer()


@cb(r"^ptog:(\d+):(protect|pause)$")
async def ptog(c, q):
    pid, f = int(q.matches[0].group(1)), q.matches[0].group(2)
    key = "protect" if f == "protect" else "paused"
    p = await products.find_one({"_id": pid})
    if p:
        await products.update_one({"_id": pid}, {"$set": {key: not p.get(key)}})
        drop_cache(pid)
    t, k = await card(pid)
    await show(q, t, k)
    await q.answer()


# ---- delete ----
@cb(r"^pdel:(\d+)$")
async def pdel(c, q):
    pid = int(q.matches[0].group(1))
    await show(q, f"⚠️ Product <b>#{pid}</b> delete karna hai? Sab refers aur users ka is product ka data hat jayega.",
               KB([[B("✅ Haan, delete", callback_data=f"pdelc:{pid}"), B("⬅️ Nahi", callback_data=f"pm:{pid}")]]))
    await q.answer()


@cb(r"^pdelc:(\d+)$")
async def pdelc(c, q):
    pid = int(q.matches[0].group(1))
    p = await products.find_one({"_id": pid})
    if p:
        await del_storage(p["msg_ids"])
    await products.delete_one({"_id": pid})
    await refers.delete_many({"product_id": pid})
    await users.update_many({}, {"$unset": {f"products.p{pid}": ""}})
    drop_cache(pid)
    await show(q, f"🗑 Product #{pid} delete ho gaya.", HOME)
    await q.answer()


# ================= edit fields =================
PROMPT = {
    "name": ("e_name", "📝 Naya naam bhejo:"),
    "refers": ("e_refers", "🎯 Naya refer target (number) bhejo:"),
    "timer": ("e_timer", "⏳ Delete timer (minutes) bhejo.\n<code>0</code> = off, <code>g</code> = global default"),
}


@cb(r"^pe:(\d+):(name|refers|timer|content|fsub)$")
async def pe(c, q):
    uid, pid, f = q.from_user.id, int(q.matches[0].group(1)), q.matches[0].group(2)
    if f == "fsub":
        t, k = await fsub_view(pid)
        await show(q, t, k)
        return await q.answer()
    if f == "content":
        sent = await q.message.edit_text(
            "📦 Naya content bhejo (text/image/media, ek ek karke).\nPurana content replace ho jayega.\n\nHo jaye to ✅ Done dabao.",
            reply_markup=DONE_KB)
        ST[uid] = {"act": "e_content", "pid": pid, "items": [], "st": sent}
        return await q.answer()
    act, txt = PROMPT[f]
    ST[uid] = {"act": act, "pid": pid}
    await show(q, txt, KB([[B("⬅️ Back", callback_data=f"pm:{pid}")]]))
    await q.answer()


# ================= fsub manage =================
async def fsub_view(pid):
    p = await products.find_one({"_id": pid})
    if not p:
        return "❌ Product nahi mila.", HOME
    rows = [[B(f"❌ {f['title'][:25]} ({f['mode']})", callback_data=f"fsrm:{pid}:{i}")]
            for i, f in enumerate(p["fsubs"])]
    rows += [[B("➕ Add Channel", callback_data=f"fsadd:{pid}")], [B("⬅️ Back", callback_data=f"pm:{pid}")]]
    return f"📢 <b>FSub — {esc(p['name'])}</b>\n\nChannel pe tap karo to remove ho jayega.", KB(rows)


@cb(r"^fsrm:(\d+):(\d+)$")
async def fsrm(c, q):
    pid, idx = int(q.matches[0].group(1)), int(q.matches[0].group(2))
    p = await products.find_one({"_id": pid})
    if p and idx < len(p["fsubs"]):
        fs = p["fsubs"]
        fs.pop(idx)
        await products.update_one({"_id": pid}, {"$set": {"fsubs": fs}})
        drop_cache(pid)
    t, k = await fsub_view(pid)
    await show(q, t, k)
    await q.answer()


FS_PROMPT = ("📢 Channel ka <b>@username / ID</b> bhejo, ya channel se koi message <b>forward</b> karo.\n"
             "Bot us channel me admin hona chahiye.")


@cb(r"^fsadd:(\w+)$")
async def fsadd(c, q):
    uid, t = q.from_user.id, q.matches[0].group(1)
    if t == "draft":
        s = ST.get(uid)
        if not s:
            return await q.answer("Session expire. /admin se dobara karo.", show_alert=True)
        s["act"], s["target"] = "fs_chat", "draft"
    else:
        ST[uid] = {"act": "fs_chat", "target": int(t)}
    await show(q, FS_PROMPT, CANCEL)
    await q.answer()


@cb(r"^fsm:(normal|request)$")
async def fsm(c, q):
    uid, mode = q.from_user.id, q.matches[0].group(1)
    s = ST.get(uid)
    if not s or s.get("act") != "fs_chat" or "chat" not in s:
        return await q.answer("Session expire. /admin se dobara karo.", show_alert=True)
    try:
        f = await build_fsub(s["chat"], mode)
    except Exception as e:
        return await q.answer(f"❌ {e}"[:190], show_alert=True)
    if s["target"] == "draft":
        if all(x["chat_id"] != f["chat_id"] for x in s["d"]["fsubs"]):
            s["d"]["fsubs"].append(f)
        s["act"] = "a_fsub"
        s.pop("chat", None)
        t, k = fsub_draft_view(s)
        await show(q, t, k)
    else:
        pid = s["target"]
        await products.update_one({"_id": pid, "fsubs.chat_id": {"$ne": f["chat_id"]}}, {"$push": {"fsubs": f}})
        drop_cache(pid)
        ST.pop(uid, None)
        t, k = await fsub_view(pid)
        await show(q, t, k)
    await q.answer()


# ================= add product wizard =================
@cb(r"^aadd$")
async def aadd(c, q):
    ST[q.from_user.id] = {"act": "a_name", "d": {"fsubs": []}}
    await show(q, "📝 Product ka <b>naam</b> bhejo:", CANCEL)
    await q.answer()


def fsub_draft_view(s):
    fs = s["d"]["fsubs"]
    lines = "\n".join(f"• {esc(f['title'])} ({f['mode']})" for f in fs) or "— koi nahi"
    return (f"📢 <b>FSub channels</b>\n\n{lines}\n\nChannel add karo ya Done dabao (bina fsub bhi chalega).",
            KB([[B("➕ Add Channel", callback_data="fsadd:draft")],
                [B("✅ Done", callback_data="fdone")], [B("❌ Cancel", callback_data="cancel")]]))


@cb(r"^cdone$")
async def cdone(c, q):
    uid = q.from_user.id
    s = ST.get(uid)
    if not s or s.get("act") not in ("a_content", "e_content"):
        return await q.answer("Session expire. /admin se dobara karo.", show_alert=True)
    if not s["items"]:
        return await q.answer("❌ Pehle kuch content bhejo.", show_alert=True)
    new_ids = [cid for _, cid in sorted(s["items"])]
    if s["act"] == "a_content":
        s["act"] = "a_fsub"
        t, k = fsub_draft_view(s)
        await show(q, t, k)
    else:
        pid = s["pid"]
        p = await products.find_one({"_id": pid})
        await products.update_one({"_id": pid}, {"$set": {"msg_ids": new_ids}})
        drop_cache(pid)
        if p:
            await del_storage(p["msg_ids"])
        ST.pop(uid, None)
        t, k = await card(pid)
        await show(q, t, k)
    await q.answer()


@cb(r"^fdone$")
async def fdone(c, q):
    s = ST.get(q.from_user.id)
    if not s or s.get("act") != "a_fsub":
        return await q.answer("Session expire. /admin se dobara karo.", show_alert=True)
    d = s["d"]
    text = (f"📋 <b>Preview</b>\n\nNaam: <b>{esc(d['name'])}</b>\nRefer: <b>{d['refers_required']}</b>\n"
            f"Content: <b>{len(s['items'])}</b> items\nFSub: <b>{len(d['fsubs'])}</b> channel\n\nConfirm karein?")
    await show(q, text, KB([[B("✅ Confirm", callback_data="cfm"), B("❌ Cancel", callback_data="cancel")]]))
    await q.answer()


@cb(r"^cfm$")
async def cfm(c, q):
    uid = q.from_user.id
    s = ST.get(uid)
    if not s or s.get("act") != "a_fsub":
        return await q.answer("Session expire. /admin se dobara karo.", show_alert=True)
    d = s["d"]
    pid = await next_pid()
    await products.insert_one({
        "_id": pid, "name": d["name"], "refers_required": d["refers_required"],
        "msg_ids": [cid for _, cid in sorted(s["items"])], "fsubs": d["fsubs"],
        "delete_min": None, "protect": False, "paused": False, "created": now(),
    })
    ST.pop(uid, None)
    await show(q, f"✅ <b>Product #{pid} ban gaya!</b>\n\n🔗 Link:\n<code>{plink(pid)}</code>",
               KB([[B("📦 Manage", callback_data=f"pm:{pid}")], [B("🏠 Home", callback_data="adm")]]))
    await q.answer()


# ================= stats / settings =================
@cb(r"^ast$")
async def stats(c, q):
    total = await users.count_documents({})
    act = await users.count_documents({"active": True})
    dl = await users.count_documents({"has_delivered": True})
    pc = await products.count_documents({})
    rc = await refers.count_documents({"status": "counted"})
    await show(q, (f"📊 <b>Stats</b>\n\n👥 Users: <b>{total}</b> (active {act})\n📦 Products: <b>{pc}</b>\n"
                   f"🎁 Delivered users: <b>{dl}</b>\n✅ Counted refers: <b>{rc}</b>"), HOME)
    await q.answer()


async def settings_view():
    s = await get_settings()
    t = (f"⚙️ <b>Settings</b>\n\n⏳ Global delete timer: <b>{str(s['delete_min']) + ' min' if s['delete_min'] else 'Off'}</b>\n"
         f"🔁 Refer expiry (channel leave par count kam): <b>{'ON' if s['refer_expiry'] else 'OFF'}</b>")
    kb = KB([[B("⏳ Set Timer", callback_data="aset_t")],
             [B(f"🔁 Refer expiry: {'ON' if s['refer_expiry'] else 'OFF'}", callback_data="aset_e")],
             [B("🏠 Home", callback_data="adm")]])
    return t, kb


@cb(r"^aset$")
async def aset(c, q):
    t, k = await settings_view()
    await show(q, t, k)
    await q.answer()


@cb(r"^aset_t$")
async def aset_t(c, q):
    ST[q.from_user.id] = {"act": "set_timer"}
    await show(q, "⏳ Global delete timer (minutes) bhejo. <code>0</code> = off", CANCEL)
    await q.answer()


@cb(r"^aset_e$")
async def aset_e(c, q):
    s = await get_settings()
    await set_setting("refer_expiry", not s["refer_expiry"])
    t, k = await settings_view()
    await show(q, t, k)
    await q.answer()


# ================= broadcast =================
TLABEL = {"all": "All users", "done": "Completed users", "pend": "Pending users"}


def bc_query(t):
    q = {"active": True}
    if t == "done":
        q["has_delivered"] = True
    elif t == "pend":
        q["has_delivered"] = {"$ne": True}
    elif isinstance(t, int):
        q[f"products.p{t}"] = {"$exists": True}
    return q


async def bc_panel(s):
    t = s["target"]
    label = TLABEL.get(t) if isinstance(t, str) else f"Product #{t}"
    n = await users.count_documents(bc_query(t))
    text = (f"📢 <b>Broadcast</b>\n\n🎯 Target: <b>{label}</b> ({n} users)\n"
            f"📌 Pin: <b>{'ON' if s['pin'] else 'OFF'}</b>\n"
            f"⏳ Auto-delete: <b>{str(s['dmin']) + ' min' if s['dmin'] else 'Off'}</b>")
    kb = KB([
        [B("All", callback_data="bcs:all"), B("Completed", callback_data="bcs:done"),
         B("Pending", callback_data="bcs:pend")],
        [B("📦 Product-wise", callback_data="bcp")],
        [B(f"📌 Pin: {'ON' if s['pin'] else 'OFF'}", callback_data="bcpin"),
         B("⏳ Auto-delete", callback_data="bcdel")],
        [B("🚀 Send", callback_data="bcgo"), B("❌ Cancel", callback_data="cancel")],
    ])
    return text, kb


@cb(r"^bc$")
async def bc(c, q):
    ST[q.from_user.id] = {"act": "bc_msg"}
    await show(q, "📢 Broadcast ka message bhejo (text / photo / video / kuch bhi):", CANCEL)
    await q.answer()


def _bc_state(q):
    s = ST.get(q.from_user.id)
    return s if s and s.get("act") == "bc_ready" else None


@cb(r"^bcs:(all|done|pend)$")
async def bcs(c, q):
    s = _bc_state(q)
    if not s:
        return await q.answer("Session expire.", show_alert=True)
    s["target"] = q.matches[0].group(1)
    t, k = await bc_panel(s)
    await show(q, t, k)
    await q.answer()


@cb(r"^bcp$")
async def bcp(c, q):
    s = _bc_state(q)
    if not s:
        return await q.answer("Session expire.", show_alert=True)
    items = await products.find({}, {"name": 1}).sort("_id", 1).limit(30).to_list(30)
    rows = [[B(f"#{p['_id']} {p['name'][:30]}", callback_data=f"bcpp:{p['_id']}")] for p in items]
    rows.append([B("⬅️ Back", callback_data="bcback")])
    await show(q, "📦 Product chuno:", KB(rows))
    await q.answer()


@cb(r"^(bcpp:(\d+)|bcback)$")
async def bcpp(c, q):
    s = _bc_state(q)
    if not s:
        return await q.answer("Session expire.", show_alert=True)
    if q.matches[0].group(2):
        s["target"] = int(q.matches[0].group(2))
    t, k = await bc_panel(s)
    await show(q, t, k)
    await q.answer()


@cb(r"^bcpin$")
async def bcpin(c, q):
    s = _bc_state(q)
    if not s:
        return await q.answer("Session expire.", show_alert=True)
    s["pin"] = not s["pin"]
    t, k = await bc_panel(s)
    await show(q, t, k)
    await q.answer()


@cb(r"^bcdel$")
async def bcdel(c, q):
    s = _bc_state(q)
    if not s:
        return await q.answer("Session expire.", show_alert=True)
    cycle = [0, 10, 30, 60]
    s["dmin"] = cycle[(cycle.index(s["dmin"]) + 1) % len(cycle)]
    t, k = await bc_panel(s)
    await show(q, t, k)
    await q.answer()


@cb(r"^bcx$")
async def bcx(c, q):
    r = RUN.get(q.from_user.id)
    if r:
        r["stop"] = True
    await q.answer("🛑 Stopping...")


@cb(r"^bcgo$")
async def bcgo(c, q):
    uid = q.from_user.id
    s = _bc_state(q)
    if not s:
        return await q.answer("Session expire.", show_alert=True)
    if uid in RUN:
        return await q.answer("Ek broadcast pehle se chal raha hai.", show_alert=True)
    ST.pop(uid, None)
    st = await q.message.edit_text("📢 Broadcast shuru...", reply_markup=KB([[B("🛑 Stop", callback_data="bcx")]]))
    task = asyncio.create_task(run_bc(uid, s["src"], bc_query(s["target"]), s["pin"], s["dmin"], st))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    await q.answer()


async def run_bc(admin_id, src, query, pin, dmin, st):
    RUN[admin_id] = {"stop": False}
    total = await users.count_documents(query)
    ok = blocked = fail = 0
    last = time.monotonic()
    stop_kb = KB([[B("🛑 Stop", callback_data="bcx")]])
    try:
        async for u in users.find(query, {"_id": 1}):
            if RUN[admin_id]["stop"]:
                break
            uid = u["_id"]
            while True:
                try:
                    sm = await app.copy_message(uid, src[0], src[1])
                    ok += 1
                    if pin:
                        try:
                            await app.pin_chat_message(uid, sm.id, disable_notification=True, both_sides=True)
                        except Exception:
                            pass
                    if dmin:
                        await jobs.insert_one({"chat_id": uid, "msg_ids": [sm.id], "notice_id": None,
                                               "delete_at": now() + timedelta(minutes=dmin)})
                    break
                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except (UserIsBlocked, InputUserDeactivated, PeerIdInvalid):
                    blocked += 1
                    await users.update_one({"_id": uid}, {"$set": {"active": False}})
                    break
                except Exception as e:
                    log.debug("bc fail %s: %s", uid, e)
                    fail += 1
                    break
            if time.monotonic() - last > 4:
                last = time.monotonic()
                try:
                    await st.edit_text(
                        f"📢 Broadcasting... {ok + blocked + fail}/{total}\n✅ {ok}  🚫 {blocked}  ❌ {fail}",
                        reply_markup=stop_kb)
                except Exception:
                    pass
            await asyncio.sleep(0.04)
        stopped = RUN[admin_id]["stop"]
        summary = (f"{'🛑 Stopped' if stopped else '✅ Broadcast complete'}\n\n"
                   f"Total: {total}\n✅ Sent: {ok}\n🚫 Blocked: {blocked}\n❌ Failed: {fail}")
        try:
            await st.edit_text(summary, reply_markup=HOME)
        except Exception:
            await app.send_message(admin_id, summary)
        await log_ch(f"📢 {summary}")
    finally:
        RUN.pop(admin_id, None)


# ================= admin text/media state handler =================
@app.on_message(filters.private & A & ~filters.command(["start", "admin"]), group=1)
async def state_h(c, m):
    uid = m.from_user.id
    s = ST.get(uid)
    if not s:
        return
    act = s["act"]
    txt = (m.text or "").strip()

    # ---- content collection (new / replace) ----
    if act in ("a_content", "e_content"):
        try:
            cp = await m.copy(config.STORAGE_CHANNEL)
        except Exception as e:
            return await m.reply_text(
                f"❌ Storage channel me copy nahi hua: <code>{esc(e)}</code>\nBot ko STORAGE_CHANNEL me admin banao.")
        s["items"].append((m.id, cp.id))
        try:
            await s["st"].edit_text(f"📥 <b>{len(s['items'])}</b> item mile. Aur bhejo ya ✅ Done dabao.",
                                    reply_markup=DONE_KB)
        except Exception:
            pass
        return

    if act == "a_name":
        if not txt:
            return await m.reply_text("❌ Text me naam bhejo.")
        s["d"]["name"] = txt[:60]
        s["act"] = "a_refers"
        return await m.reply_text("🎯 Kitne refer chahiye? (number)", reply_markup=CANCEL)

    if act == "a_refers":
        if not txt.isdigit():
            return await m.reply_text("❌ Number bhejo.")
        s["d"]["refers_required"] = int(txt)
        s["act"], s["items"] = "a_content", []
        s["st"] = await m.reply_text(
            "📦 Ab content bhejo — text, image, media, jo bhi (ek ek karke).\nPoora hone par ✅ Done dabao.",
            reply_markup=DONE_KB)
        return

    if act in ("e_name", "e_refers", "e_timer"):
        pid = s["pid"]
        if act == "e_name":
            if not txt:
                return await m.reply_text("❌ Text me naam bhejo.")
            upd = {"name": txt[:60]}
        elif act == "e_refers":
            if not txt.isdigit():
                return await m.reply_text("❌ Number bhejo.")
            upd = {"refers_required": int(txt)}
        else:
            if txt.lower() == "g":
                v = None
            elif txt.isdigit():
                v = int(txt)
            else:
                return await m.reply_text("❌ Number ya <code>g</code> bhejo.")
            upd = {"delete_min": v}
        await products.update_one({"_id": pid}, {"$set": upd})
        drop_cache(pid)
        ST.pop(uid, None)
        t, k = await card(pid)
        return await m.reply_text(t, reply_markup=k)

    if act == "fs_chat":
        ch = await resolve_chat(m)
        if not ch:
            return await m.reply_text("❌ Channel nahi mila. @username / ID bhejo ya channel ka message forward karo.")
        s["chat"] = ch
        return await m.reply_text(
            f"📢 <b>{esc(ch.title)}</b>\n\nFSub mode chuno:",
            reply_markup=KB([[B("✅ Normal", callback_data="fsm:normal"), B("📨 Request", callback_data="fsm:request")],
                             [B("❌ Cancel", callback_data="cancel")]]))

    if act == "set_timer":
        if not txt.isdigit():
            return await m.reply_text("❌ Number bhejo.")
        await set_setting("delete_min", int(txt))
        ST.pop(uid, None)
        t, k = await settings_view()
        return await m.reply_text(t, reply_markup=k)

    if act == "bc_msg":
        s.update(act="bc_ready", src=(m.chat.id, m.id), target="all", pin=False, dmin=0)
        t, k = await bc_panel(s)
        return await m.reply_text(t, reply_markup=k)
