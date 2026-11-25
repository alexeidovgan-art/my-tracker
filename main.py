
import asyncio
import logging
import os

from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession
from telegram import Bot

import database
from scanner import run_scanner


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

log = logging.getLogger("main")


def required_env(name):
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Не задана переменная Render: {name}"
        )
    return value


async def health(request):
    return web.json_response({
        "status": "running",
        "service": "NFT Gift Tracker"
    })


async def start_health_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", "10000"))

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    log.info("Health server запущен: %s", port)

    return runner


async def main():
    api_id = int(required_env("API_ID"))
    api_hash = required_env("API_HASH")
    session_string = required_env("TG_SESSION")
    bot_token = required_env("BOT_TOKEN")

    database.init_db()

    user_client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash
    )

    bot = Bot(token=bot_token)

    await user_client.connect()

    if not await user_client.is_user_authorized():
        await user_client.disconnect()
        raise RuntimeError(
            "Telegram-сессия не авторизована."
        )

    me = await user_client.get_me()

    log.info(
        "Telegram подключён: %s",
        getattr(me, "username", None) or me.id
    )

    await bot.initialize()

    health_runner = await start_health_server()

    scanner_task = asyncio.create_task(
        run_scanner(user_client, bot),
        name="gift-scanner"
    )

    try:
        await scanner_task

    finally:
        scanner_task.cancel()

        try:
            await scanner_task
        except asyncio.CancelledError:
            pass

        await health_runner.cleanup()
        await bot.shutdown()
        await user_client.disconnect()

        log.info("Трекер остановлен")


if __name__ == "__main__":
    asyncio.run(main())
