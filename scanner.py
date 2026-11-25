
import asyncio
import logging
import os
import re
from telethon import TelegramClient, functions, types
from telethon.errors import FloodWaitError

import database


log = logging.getLogger("scanner")

GROUP_ID = -1005741245874
PRICER_USERNAME = "PriceNFTbot"

TOPIC_NAMES = {
    "cheap": ("дешёвые", 0, 10),
    "medium": ("средние", 10, 50),
    "expensive": ("дорогие", 50, 150),
    "premium": ("премиум", 150, float("inf")),
}

SCAN_INTERVAL = 300
PAGE_SIZE = 100


def get_topic_for_floor(floor):
    if floor is None:
        return "cheap"

    if floor < 10:
        return "cheap"
    if floor < 50:
        return "medium"
    if floor < 150:
        return "expensive"

    return "premium"


async def discover_topics(client):
    entity = await client.get_entity(GROUP_ID)

    result = await client(
        functions.channels.GetForumTopicsRequest(
            channel=entity,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
        )
    )

    found = {}

    for topic in result.topics:
        name = topic.title.strip().casefold()

        for key, (expected, _, _) in TOPIC_NAMES.items():
            if name == expected:
                found[key] = topic.id

    missing = set(TOPIC_NAMES) - set(found)

    if missing:
        raise RuntimeError(
            "Не найдены темы группы: " + ", ".join(sorted(missing))
        )

    log.info("Темы найдены автоматически: %s", found)
    return found


def extract_attributes(gift):
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


def get_gift_price(gift):
    for amount in getattr(gift, "resell_amount", []) or []:
        name = type(amount).__name__

        if name == "StarsTonAmount":
            value = getattr(amount, "amount", None)

            if value is not None:
                return f"{value / 1_000_000_000:.4f} TON"

        elif name == "StarsAmount":
            value = getattr(amount, "amount", None)

            if value is not None:
                return f"{value} ⭐"

    return "Цена не определена"


def get_owner(gift, users):
    owner = getattr(gift, "owner_id", None)

    if not isinstance(owner, types.PeerUser):
        return None, None, None

    user = users.get(owner.user_id)

    if not user:
        return str(owner.user_id), None, f"tg://user?id={owner.user_id}"

    username = getattr(user, "username", None)

    if username:
        return str(owner.user_id), username, f"https://t.me/{username}"

    return str(owner.user_id), None, f"tg://user?id={owner.user_id}"


def is_blockchain_gift(gift):
    return bool(
        getattr(gift, "gift_address", None)
        or getattr(gift, "owner_address", None)
    )


def get_gift_url(gift):
    slug = getattr(gift, "slug", None)

    if slug:
        return f"https://t.me/nft/{slug}"

    return None


async def ask_pricer(client, gift_url):
    if not gift_url:
        return None

    try:
        bot = await client.get_entity(PRICER_USERNAME)

        async with client.conversation(
            bot,
            timeout=35,
            exclusive=False
        ) as conversation:
            await conversation.send_message(gift_url)

            response = await conversation.get_response()

            return response.raw_text or ""

    except asyncio.TimeoutError:
        log.warning("PriceNFTbot не ответил вовремя")
    except FloodWaitError as error:
        log.warning("Telegram ограничил запросы на %s секунд", error.seconds)
        await asyncio.sleep(error.seconds)
    except Exception:
        log.exception("Ошибка обращения к PriceNFTbot")

    return None


def extract_pricer_data(text):
    if not text:
        return {}

    data = {}

    username = re.search(r"@([A-Za-z0-9_]{5,32})", text)

    if username:
        data["owner_username"] = username.group(1)
        data["owner_url"] = "https://t.me/" + username.group(1)

    ton_values = re.findall(
        r"(\d+(?:[.,]\d+)?)\s*TON",
        text,
        flags=re.IGNORECASE
    )

    if ton_values:
        data["pricer_ton_values"] = [
            float(value.replace(",", ".")) for value in ton_values
        ]

    return data


def format_listing(gift, owner, username, owner_url, attributes, price, floor):
    title = getattr(gift, "title", None) or "Telegram Gift"

    owner_text = (
        f"@{username}" if username
        else f"ID: {owner}" if owner
        else "Не определён"
    )

    floor_text = f"{floor:.4f} TON" if floor is not None else "Не определён"

    return (
        f"🎁 <b>{title}</b>\n\n"
        f"💰 Цена: {price}\n"
        f"📊 Флор: {floor_text}\n\n"
        f"🎨 Модель: {attributes['model'] or 'Не определена'}\n"
        f"🖼 Фон: {attributes['backdrop'] or 'Не определён'}\n"
        f"🔹 Узоры: {attributes['pattern'] or 'Не определены'}\n\n"
        f"👤 Владелец: {owner_text}\n"
        f"🔗 Подарок: {get_gift_url(gift) or 'Ссылка отсутствует'}"
    )


async def publish_gift(client, gift, topics, users):
    if is_blockchain_gift(gift):
        log.info("Пропущен подарок из блокчейна: %s", gift.id)
        return

    gift_id = str(gift.id)

    if database.gift_exists(gift_id):
        return

    attributes = extract_attributes(gift)
    owner_id, username, owner_url = get_owner(gift, users)

    gift_url = get_gift_url(gift)
    price = get_gift_price(gift)

    pricer_response = await ask_pricer(client, gift_url)
    pricer_data = extract_pricer_data(pricer_response)

    if not username and pricer_data.get("owner_username"):
        username = pricer_data["owner_username"]
        owner_url = pricer_data["owner_url"]

    floor = None

    values = pricer_data.get("pricer_ton_values", [])

    if values:
        floor = min(values)

    category = get_topic_for_floor(floor)
    topic_id = topics[category]

    title = getattr(gift, "title", None) or "Telegram Gift"

    database.save_gift({
        "gift_id": gift_id,
        "title": title,
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
    })

    text = format_listing(
        gift,
        owner_id,
        username,
        owner_url,
        attributes,
        price,
        floor
    )

    try:
        entity = await client.get_entity(GROUP_ID)

        message = await client.send_message(
            entity,
            text,
            reply_to=topic_id,
            parse_mode="html",
            link_preview=False
        )

        database.mark_published(gift_id, topic_id, message.id)

        log.info("Опубликован подарок %s", gift_id)

    except Exception:
        log.exception("Не удалось опубликовать подарок %s", gift_id)


async def scan_collection(client, topics, base_gift):
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
            log.warning("FloodWait: %s секунд", error.seconds)
            await asyncio.sleep(error.seconds)
            continue

        except Exception:
            log.exception("Ошибка получения страницы коллекции")
            break

        gifts = getattr(result, "gifts", []) or []
        users = {
            user.id: user
            for user in getattr(result, "users", []) or []
        }

        if not gifts:
            break

        for gift in gifts:
            try:
                await publish_gift(client, gift, topics, users)
                await asyncio.sleep(1)
            except Exception:
                log.exception("Ошибка обработки подарка")

        next_offset = getattr(result, "next_offset", None)

        if not next_offset or next_offset == offset:
            break

        offset = next_offset


async def scan_once(client):
    topics = await discover_topics(client)

    catalog = await client(
        functions.payments.GetStarGiftsRequest(hash=0)
    )

    collections = getattr(catalog, "gifts", []) or []

    log.info("Коллекций в каталоге: %s", len(collections))

    for base_gift in collections:
        try:
            log.info(
                "Сканируем коллекцию: %s",
                getattr(base_gift, "title", base_gift.id)
            )

            await scan_collection(client, topics, base_gift)

        except Exception:
            log.exception("Ошибка коллекции %s", base_gift.id)


async def run_scanner(client):
    while True:
        try:
            log.info("Начинаем новый цикл сканирования")
            await scan_once(client)
            log.info("Сканирование завершено")

        except Exception:
            log.exception("Ошибка главного цикла сканера")

        await asyncio.sleep(SCAN_INTERVAL)
