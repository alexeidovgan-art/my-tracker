import asyncio
import logging
import os

from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
)

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


async def claim_callback(update, context):
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    match = __import__("re").fullmatch(
        r"claim:(\d+)",
        query.data or ""
    )

    if not match:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    gift_id = int(match.group(1))

    # Атомарно закрепляем лог за одним пользователем.
    gift = database.claim_gift(gift_id, user.id)

    if not gift:
        await query.answer(
            "Этот лог уже занят.",
            show_alert=True
        )
        return

    owner_url = gift.get("owner_url")

    if not owner_url:
        database.release_claim(gift_id, user.id)

        await query.answer(
            "Ссылка владельца отсутствует.",
            show_alert=True
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Посмотреть владельца",
                url=owner_url
            )
        ]
    ])

    text = (
        "✅ <b>Лог успешно занят.</b>\n\n"
        f"🎁 {gift.get('title') or 'Подарок'}\n"
        f"💰 Цена: <b>{gift.get('price') or 0:.3f} TON</b>\n"
        f"📊 Floor: <b>{gift.get('floor') or 0:.3f} TON</b>\n\n"
        "Нажми кнопку ниже, чтобы открыть профиль владельца."
    )

    try:
        # Пишем только тому, кто нажал «Занять лог».
        await context.bot.send_message(
            chat_id=user.id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

    except TelegramError:
        # Если пользователь ещё не запускал бота,
        # освобождаем лог, чтобы его мог занять другой.
        database.release_claim(gift_id, user.id)

        await query.answer(
            "Не могу написать тебе в личные сообщения. "
            "Открой бота, нажми /start и попробуй снова.",
            show_alert=True
        )
        return

    # Убираем кнопку из группового сообщения.
    try:
        await query.edit_message_reply_markup(
            reply_markup=None
        )
    except TelegramError:
        log.warning("Не удалось убрать кнопку занятого лога")

    await query.answer("Лог занят. Проверь личные сообщения.")


async def main():
    required = [
        "API_ID",
        "API_HASH",
        "TG_SESSION",
        "BOT_TOKEN",
    ]

    missing = [
        name
        for name in required
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

    application = (
        Application.builder()
        .token(bot_token)
        .build()
    )

    application.add_handler(
        CallbackQueryHandler(
            claim_callback,
            pattern=r"^claim:\d+$"
        )
    )

    health_runner = None
    scanner_task = None
    application_started = False
    polling_started = False

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

        await application.initialize()
        await application.start()
        application_started = True

        await application.updater.start_polling(
            drop_pending_updates=True
        )
        polling_started = True

        bot_info = await application.bot.get_me()

        log.info(
            "Бот подключен: @%s | ID: %s",
            bot_info.username,
            bot_info.id
        )

        health_runner = await start_health_server()

        scanner_task = asyncio.create_task(
            run_scanner(
                user_client,
                application.bot
            )
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

        if polling_started:
            await application.updater.stop()

        if application_started:
            await application.stop()
            await application.shutdown()

        if health_runner:
            await health_runner.cleanup()

        await user_client.disconnect()

        log.info("NFT Gift Tracker остановлен")


if __name__ == "__main__":
    asyncio.run(main())
