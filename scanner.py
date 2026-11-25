
import asyncio
import logging
import re
import html

from telethon import functions, types
from telethon.errors import FloodWaitError
from telegram import Bot

import database


log = logging.getLogger("scanner")

GROUP_ID = -1005741245874
PRICER_USERNAME = "PriceNFTbot"

SCAN_INTERVAL = 300
PAGE_SIZE = 100

TOPIC_NAMES = {
    "cheap": "дешёвые",
    "medium": "средние",
    "expensive": "дорогие",
    "premium": "премиум",
}


def get_topic_for_floor(floor):
    if floor is None or floor < 10:
        return "cheap"
    if floor < 50:
        return "medium"
    if floor < 150:
        return "expensive"
    return "premium"


async def discover_topics(client):
    group = await client.get_entity(GROUP_ID)

    result = await client(
        functions.channels.GetForumTopicsRequest(
            channel=group,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
        )
    )

    found = {}

    for topic in result.topics:
        name = topic.title.strip().casefold()

        for key, expected in TOPIC_NAMES.items():
            if name == expected:
                found[key] = topic.id

    missing = set(TOPIC_NAMES) - set(found)

    if missing:
        raise RuntimeError(
            "Не найдены темы: " + ", ".join(sorted(missing))
        )

    log.info("Темы группы найдены: %s", found)
    return found


def is_blockchain_gift(gift):
    return bool(
        getattr(gift, "gift_address", None)
        or getattr(gift, "owner_address", None)
    )


def get_attributes(gift):
    result = {
        "model": None,
        "backdrop": None,
        "pattern": None,
    }

    for attribute in getattr(gift, "attributes", []) or []:
        name = type(attribute).__name__

        if name == "StarGiftAttributeModel":
            result["model"] = getattr(
                getattr(attribute, "name", None), "text", None
            )

        elif name == "StarGiftAttributeBackdrop":
            result["backdrop"] = getattr(
                getattr(attribute, "name", None), "text", None
            )

        elif name == "StarGiftAttributePattern":
            result["pattern"] = getattr(
                getattr(attribute, "name", None), "text", None
            )

    return result


def get_owner(gift, users):
    owner = getattr(gift, "owner_id", None)

    if not isinstance(owner, types.PeerUser):
        return None, None, None

    user_id = owner.user_id
    user = users.get(user_id)

    if user:
        username = getattr(user, "username", None)

        if username:
            return (
                str(user_id),
                username,
                f"https://t.me/{username}"
            )

    return (
        str(user_id),
        None,
        f"tg://user?id={user_id}"
    )


def get_gift_url(gift):
    slug = getattr(gift, "slug", None)

    if slug:
        return f"https://t.me/nft/{slug}"

    return None


def get_price(gift):
    for amount in getattr(gift, "resell_amount", []) or []:
        name = type(amount).__name__

        if name == "StarsTonAmount":
            value = getattr(amount, "amount", None)

            if value is not None:
                return f"{value / 1_000_000_000:.4f} TON"

        if name == "StarsAmount":
            value = getattr(amount, "amount", None)

            if value is not None:
                return f"{value} ⭐"

    return "Не определена"


async def ask_pricer(client, gift_url):
    if not gift_url:
        return None

    try:
        pricer = await client.get_entity(PRICER_USERNAME)

        async with client.conversation(
            pricer,
            timeout=25,
            exclusive=False
        ) as conversation:
            await conversation.send_message(gift_url)
            response = await conversation.get_response()

            return response.raw_text or ""

    except asyncio.TimeoutError:
        log.warning("PriceNFTbot не ответил")
    except FloodWaitError as error:
        log.warning(
            "PriceNFTbot: ожидание %s секунд",
            error.seconds
        )
        await asyncio.sleep(error.seconds)
    except Exception:
        log.exception("Ошибка обращения к PriceNFTbot")

    return None


def extract_floor(text):
    if not text:
        return None

    patterns = [
        r"(?:floor|флор)[^\d]{0,30}(\d+(?:[.,]\d+)?)\s*TON",
        r"(?:минимальная цена|минимум)[^\d]{0,30}"
        r"(\d+(?:[.,]\d+)?)\s*TON",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                pass

    return None


def extract_pricer_owner(text):
    if not text:
        return None, None

    match = re.search(
        r"@([A-Za-z0-9_]{5,32})",
        text
    )

    if not match:
        return None, None

    username = match.group(1)

    return username, f"https://t.me/{username}"


def make_listing(
    gift,
    owner_id,
    username,
    gift_url,
    attributes,
    price,
    floor
):
    title = html.escape(
        getattr(gift, "title", None) or "Telegram Gift"
    )

    if username:
        owner_text = "@" + html.escape(username)
    elif owner_id:
        owner_text = "ID: " + html.escape(str(owner_id))
    else:
        owner_text = "Не определён"

    floor_text = (
        f"{floor:.4f} TON"
        if floor is not None
        else "Не определён"
    )

    gift_link = (
        f'<a href="{html.escape(gift_url, quote=True)}">'
        f"Открыть подарок</a>"
        if gift_url
        else "Ссылка отсутствует"
    )

    return (
        f"🎁 <b>{title}</b>\n\n"
        f"💰 Цена: {html.escape(price)}\n"
        f"📊 Флор: {floor_text}\n\n"
        f"🎨 Модель: "
        f"{html.escape(attributes['model'] or 'Не определена')}\n"
        f"🖼 Фон: "
        f"{html.escape(attributes['backdrop'] or 'Не определён')}\n"
        f"🔹 Узоры: "
        f"{html.escape(attributes['pattern'] or 'Не определены')}\n\n"
        f"👤 Владелец: {owner_text}\n"
        f"🔗 {gift_link}"
    )


async def publish_gift(client, bot, gift, topics, users):
    if is_blockchain_gift(gift):
        log.info("Пропуск блокчейн-подарка: %s", gift.id)
        return

    gift_id = str(gift.id)

    if database.gift_exists(gift_id):
        return

    attributes = get_attributes(gift)
    owner_id, username, owner_url = get_owner(gift, users)

    gift_url = get_gift_url(gift)
    price = get_price(gift)

    pricer_text = await ask_pricer(client, gift_url)

    floor = extract_floor(pricer_text)

    if not username:
        pricer_username, pricer_url = extract_pricer_owner(
            pricer_text
        )

        if pricer_username:
            username = pricer_username
            owner_url = pricer_url

    category = get_topic_for_floor(floor)
    topic_id = topics[category]

    text = make_listing(
        gift,
        owner_id,
        username,
        gift_url,
        attributes,
        price,
        floor
    )

    try:
        message = await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=topic_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        database.save_gift({
            "gift_id": gift_id,
            "title": getattr(gift, "title", None)
                     or "Telegram Gift",
            "gift_url": gift_url,
            "owner_id": owner_id,
            "owner_username": username,
            "owner_url": owner_url,
            "price": price,
            "floor": floor,
            "model": attributes["model"],
            "backdrop": attributes["backdrop"],
            "pattern": attributes["pattern"],
            "topic_id": topic_id,
            "message_id": message.message_id,
            "status": "published",
        })

        database.mark_published(
            gift_id,
            topic_id,
            message.message_id
        )

        log.info(
            "Опубликован подарок %s в тему %s",
            gift_id,
            category
        )

    except Exception:
        log.exception(
            "Ошибка публикации подарка %s",
            gift_id
        )


async def scan_collection(client, bot, topics, base_gift):
    offset = ""

    while True:
        try:
            result = await client(
                functions.payments.GetResaleStarGiftsRequest(
                    gift_id=base_gift.id,
                    offset=offset,
                    limit=PAGE_SIZE,
                    sort_by_price=True,
                )
            )

        except FloodWaitError as error:
            log.warning(
                "Telegram FloodWait: %s секунд",
                error.seconds
            )
            await asyncio.sleep(error.seconds)
            continue

        except Exception:
            log.exception(
                "Ошибка получения коллекции %s",
                base_gift.id
            )
            return

        gifts = getattr(result, "gifts", []) or []

        users = {
            user.id: user
            for user in getattr(result, "users", []) or []
        }

        if not gifts:
            break

        log.info(
            "Коллекция %s: получено %s подарков",
            getattr(base_gift, "title", base_gift.id),
            len(gifts)
        )

        for gift in gifts:
            try:
                await publish_gift(
                    client,
                    bot,
                    gift,
                    topics,
                    users
                )

                await asyncio.sleep(1)

            except Exception:
                log.exception(
                    "Ошибка обработки подарка %s",
                    getattr(gift, "id", "?")
                )

        next_offset = getattr(result, "next_offset", None)

        if not next_offset or next_offset == offset:
            break

        offset = next_offset


async def scan_once(client, bot):
    topics = await discover_topics(client)

    catalog = await client(
        functions.payments.GetStarGiftsRequest(hash=0)
    )

    collections = getattr(catalog, "gifts", []) or []

    log.info(
        "Найдено коллекций в каталоге: %s",
        len(collections)
    )

    for base_gift in collections:
        try:
            log.info(
                "Сканируем коллекцию: %s",
                getattr(base_gift, "title", base_gift.id)
            )

            await scan_collection(
                client,
                bot,
                topics,
                base_gift
            )

        except Exception:
            log.exception(
                "Ошибка коллекции %s",
                base_gift.id
            )


async def run_scanner(client, bot):
    while True:
        try:
            log.info("Начало сканирования")
            await scan_once(client, bot)
            log.info("Цикл сканирования завершён")

        except Exception:
            log.exception("Ошибка главного цикла")

        await asyncio.sleep(SCAN_INTERVAL)
