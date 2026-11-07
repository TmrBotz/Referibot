import asyncio
import logging

from aiohttp import web
from pyrogram import idle

import admin  # noqa: F401  (registers handlers)
import config
import core
import db
import ui
import user  # noqa: F401  (registers handlers)
from client import app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


async def health(_):
    return web.Response(text="OK")


async def main():
    await db.init_indexes()
    await admin.load_admins()
    log.info("Button colors: %s", "ON" if ui.COLORS else "OFF (library me style support nahi)")

    # dummy web server (Render/Koyeb/Railway health check)
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_get("/health", health)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    log.info("Web server on port %s", config.PORT)

    await app.start()
    me = await app.get_me()
    core.BOT["username"] = me.username
    log.info("Bot started: @%s", me.username)

    loop_task = asyncio.create_task(core.delete_loop())
    await idle()

    loop_task.cancel()
    await app.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
