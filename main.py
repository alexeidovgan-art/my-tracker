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

    log.info("Health server запущен на порту %s", port)

    return runner


async def main():
    required = [
        "API_ID",
        "API_HASH",
        "TG_SESSION",
        "BOT_TOKEN",
    ]

    missing = [
        name for name in required
        if not os.getenv(name)
    ]

    if missing:
        raise RuntimeError(
            "Не заданы переменные Render: " + ", ".join(missing)
        )

    api_id = int(os.environ["API_ID"])
    api_hash = os.environ["API_HASH"]
    session_string = os.environ["TG_SESSION"]
    bot_token = os.environ["BOT_TOKEN"]

    database.init_db()

    user_client = TelegramClient(
        StringSession(session_string),
        api_id,
        api_hash
    )

    bot = Bot(token=bot_token)

    health_runner = None
    scanner_task = None

    try:
        log.info("Подключение Telethon...")

        await user_client.start()

        if not await user_client.is_user_authorized():
            raise RuntimeError(
                "Telethon-сессия не авторизована"
            )

        me = await user_client.get_me()

        log.info(
            "Telethon подключен: @%s | ID: %s",
            me.username,
            me.id
        )

        await bot.initialize()

        bot_info = await bot.get_me()

        log.info(
            "Бот подключен: @%s | ID: %s",
            bot_info.username,
            bot_info.id
        )

        health_runner = await start_health_server()

        scanner_task = asyncio.create_task(
            run_scanner(user_client, bot)
        )

        log.info("NFT Gift Tracker запущен")

        await scanner_task

    finally:
        if scanner_task:
            scanner_task.cancel()
            try:
                await scanner_task
            except asyncio.CancelledError:
                pass

        if health_runner:
            await health_runner.cleanup()

        try:
            await bot.shutdown()
        except Exception:
            pass

        await user_client.disconnect()

        log.info("NFT Gift Tracker остановлен")


if __name__ == "__main__":
    asyncio.run(main())
