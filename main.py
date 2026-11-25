
import asyncio
import logging
import os
from aiohttp import web
from telethon import TelegramClient

import database
from scanner import run_scanner


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

log = logging.getLogger("main")


def get_required_env(name):
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"Не задана переменная Render: {name}")

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
        host="0.0.0.0",
        port=port
    )

    await site.start()

    log.info("Health server запущен на порту %s", port)

    return runner


async def main():
    api_id = int(get_required_env("API_ID"))
    api_hash = get_required_env("API_HASH")
    session_string = get_required_env("TG_SESSION")

    database.init_db()

    client = TelegramClient(
        "tracker_session",
        api_id,
        api_hash
    )

    await client.connect()

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "TG_SESSION не авторизована. Проверь строку сессии."
        )

    me = await client.get_me()

    log.info(
        "Telegram подключён: %s",
        getattr(me, "username", None) or me.id
    )

    health_runner = await start_health_server()

    scanner_task = asyncio.create_task(
        run_scanner(client),
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
        await client.disconnect()

        log.info("Трекер остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Получена команда остановки")
