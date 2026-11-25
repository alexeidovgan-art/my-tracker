
import asyncio
import html
import logging
import re

from telethon import functions, types
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetForumTopicsRequest

import database


log = logging.getLogger("scanner")

# ID вашей форум-группы
GROUP_ID = -1005741245874

# Названия тем в группе
TOPIC_NAMES = {
    "cheap": "дешёвые",
    "medium": "средние",
    "expensive": "дорогие",
    "premium": "премиум",
}

# Ценовые категории во FLOR, TON
PRICE_CHEAP = 10
PRICE_MEDIUM = 50
PRICE_EXPENSIVE = 150

PAGE_SIZE = 100
SCAN_INTERVAL = 60
PRICER_DELAY = 1.5


def safe_text(value):
    if value is None:
        return "Не указано"
    return html.escape(str(value))


def get_gift_id(gift):
    value = getattr(gift, "id", None)
    return str(value) if value is not None else None


def get_gift_title(gift):
    return (
        getattr(gift, "title", None)
        or getattr(gift, "name", None)
        or "Telegram Gift"
    )


def get_gift_url(gift):
    slug = getattr(gift, "slug", None)

    if slug:
        return f"https://t.me/nft/{slug}"

    gift_id = get_gift_id(gift)
    if gift_id:
        return f"https://t.me/nft/{gift_id}"

    return "https://t.me/nft"


def is_blockchain_gift(gift):
    return bool(
        getattr(gift, "gift_address", None)
        or getattr(gift, "owner_address", None)
    )


def get_attributes(gift):
    result = {
        "model": "Не указана",
        "backdrop": "Не указан",
        "pattern": "Не указан",
    }

    attributes = getattr(gift, "attributes", None) or []

    for attribute in attributes:
        name = attribute.__class__.__name__
        value = getattr(attribute, "name", None)

        if hasattr(value, "text"):
            value = value.text

        if not value:
            continue

        if "Model" in name:
            result["model"] = str(value)
        elif "Backdrop" in name:
            result["backdrop"] = str(value)
        elif "Pattern" in name:
            result["pattern"] = str(value)

    return result


def get_owner(gift, users):
    owner_peer = getattr(gift, "owner_id", None)

    if not isinstance(owner_peer, types.PeerUser):
        return "Неизвестен", None

    owner = next(
        (
            user for user in users
            if getattr(user, "id", None) == owner_peer.user_id
        ),
        None,
    )

    if owner is None:
        return (
            f"ID {owner_peer.user_id}",
            f"tg://user?id={owner_peer.user_id}",
        )

    username = getattr(owner, "username", None)

    if username:
        return f"@{username}", f"https://t.me/{username}"

    return (
        f"ID {owner_peer.user_id}",
        f"tg://user?id={owner_peer.user_id}",
    )


def get_price(gift):
    amounts = getattr(gift, "resell_amount", None) or []

    for amount in amounts:
        class_name = amount.__class__.__name__
        value = getattr(amount, "amount", None)

        if value is None:
            continue

        if "Ton" in class_name:
            return float(value) / 1_000_000_000, "TON"

        if "Stars" in class_name:
            return float(value), "звёзд"

    return None, None


def extract_floor(text):
    if not text:
        return None

    patterns = [
        r"(?:floor|флор)\D{0,20}([\d.,]+)\s*(?:ton|тон)",
        r"([\d.,]+)\s*(?:ton|тон)\D{0,20}(?:floor|флор)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            try:
                return float(match.group(1).replace(",", "."))
            except ValueError:
                pass

    return None


async def ask_pricer(client, gift_url):
    """
    Запрашивает информацию у @PriceNFTbot.
    Формат ответа зависит от самого бота.
    """

    try:
        async with client.conversation(
            "@PriceNFTbot",
            timeout=25,
            exclusive=True,
        ) as conversation:

            await conversation.send_message(gift_url)
            response = await conversation.get_response()

            return response.raw_text or ""

    except asyncio.TimeoutError:
        log.warning("PriceNFTbot не ответил: %s", gift_url)
        return ""

    except Exception:
        log.exception("Ошибка запроса к PriceNFTbot")
        return ""


def get_topic_for_floor(floor):
    if floor is None:
        return "cheap"

    if floor < PRICE_CHEAP:
        return "cheap"

    if floor < PRICE_MEDIUM:
        return "medium"

    if floor < PRICE_EXPENSIVE:
        return "expensive"

    return "premium"


def make_listing(
    gift,
    gift_url,
    owner_name,
    owner_url,
    price,
    price_currency,
    floor,
    attributes,
):
    title = safe_text(get_gift_title(gift))
    model = safe_text(attributes["model"])
    backdrop = safe_text(attributes["backdrop"])
    pattern = safe_text(attributes["pattern"])

    if price is None:
        price_text = "Не указана"
    else:
        price_text = f"{price:g} {price_currency}"

    if floor is None:
        floor_text = "Не удалось получить"
    else:
        floor_text = f"{floor:g} TON"

    owner = safe_text(owner_name)

    if owner_url:
        owner_text = f'<a href="{html.escape(owner_url, quote=True)}">{owner}</a>'
    else:
        owner_text = owner

    return (
        f"🎁 <b>{title}</b>\n\n"
        f"💰 <b>Цена:</b> {price_text}\n"
        f"📊 <b>Флор коллекции:</b> {floor_text}\n\n"
        f"👤 <b>Владелец:</b> {owner_text}\n\n"
        f"🎨 <b>Модель:</b> {model}\n"
        f"🖼 <b>Фон:</b> {backdrop}\n"
        f"🔹 <b>Узор:</b> {pattern}\n\n"
        f'🔗 <a href="{html.escape(gift_url, quote=True)}">Открыть подарок</a>'
    )


async def discover_topics(client):
    entity = await client.get_entity(GROUP_ID)

    result = await client(
        GetForumTopicsRequest(
            peer=entity,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
        )
    )

    found = {}

    for topic in result.topics:
        name = topic.title.strip().casefold()

        for key, expected_name in TOPIC_NAMES.items():
            if name == expected_name:
                found[key] = topic.id

    missing = set(TOPIC_NAMES) - set(found)

    if missing:
        raise RuntimeError(
            "Не найдены темы группы: "
            + ", ".join(sorted(missing))
        )

    log.info("Темы группы найдены: %s", found)
    return found


async def publish_gift(client, bot, gift, topics, users):
    gift_id = get_gift_id(gift)

    if not gift_id:
        return

    # Не публикуем подарки, связанные с блокчейном
    if is_blockchain_gift(gift):
        log.info("Пропуск блокчейн-подарка: %s", gift_id)
        return

    # Не публикуем уже обработанные подарки
    if database.gift_exists(gift_id):
        return

    gift_url = get_gift_url(gift)

    attributes = get_attributes(gift)
    owner_name, owner_url = get_owner(gift, users)
    price, price_currency = get_price(gift)

    # Запрашиваем флор у PriceNFTbot
    pricer_text = await ask_pricer(client, gift_url)
    floor = extract_floor(pricer_text)

    await asyncio.sleep(PRICER_DELAY)

    topic_key = get_topic_for_floor(floor)
    topic_id = topics[topic_key]

    message_text = make_listing(
        gift=gift,
        gift_url=gift_url,
        owner_name=owner_name,
        owner_url=owner_url,
        price=price,
        price_currency=price_currency,
        floor=floor,
        attributes=attributes,
    )

    try:
        message = await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=topic_id,
            text=message_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

        database.save_gift({
            "gift_id": gift_id,
            "title": get_gift_title(gift),
            "gift_url": gift_url,
            "owner_id": (
                getattr(getattr(gift, "owner_id", None), "user_id", None)
            ),
            "owner_username": (
                owner_name if owner_name.startswith("@") else None
            ),
            "owner_url": owner_url,
            "price": price,
            "floor": floor,
            "model": attributes["model"],
            "backdrop": attributes["backdrop"],
            "pattern": attributes["pattern"],
        })

        database.mark_published(
            gift_id,
            topic_id,
            message.message_id,
        )

        log.info(
            "Опубликован подарок %s в тему %s",
            gift_id,
            topic_key,
        )

    except Exception:
        log.exception("Не удалось опубликовать подарок %s", gift_id)


async def scan_collection(client, bot, base_gift, topics):
    base_gift_id = getattr(base_gift, "id", None)

    if base_gift_id is None:
        return

    offset = ""
    page_number = 0
    total_found = 0

    while True:
        try:
            result = await client(
                functions.payments.GetResaleStarGiftsRequest(
                    gift_id=base_gift_id,
                    offset=offset,
                    limit=PAGE_SIZE,
                    sort_by_price=True,
                )
            )

        except FloodWaitError as error:
            log.warning(
                "Telegram FloodWait: пауза %s секунд",
                error.seconds,
            )
            await asyncio.sleep(error.seconds + 1)
            continue

        except Exception:
            log.exception(
                "Ошибка получения коллекции %s",
                base_gift_id,
            )
            break

        gifts = getattr(result, "gifts", []) or []
        users = getattr(result, "users", []) or []

        page_number += 1
        total_found += len(gifts)

        log.info(
            "Коллекция %s: страница %s, подарков %s",
            base_gift_id,
            page_number,
            len(gifts),
        )

        for gift in gifts:
            try:
                await publish_gift(
                    client,
                    bot,
                    gift,
                    topics,
                    users,
                )

            except FloodWaitError as error:
                log.warning(
                    "FloodWait при обработке подарка: %s секунд",
                    error.seconds,
                )
                await asyncio.sleep(error.seconds + 1)

            except Exception:
                log.exception("Ошибка обработки подарка")

        next_offset = getattr(result, "next_offset", None)

        if not next_offset:
            break

        offset = next_offset

    log.info(
        "Коллекция %s обработана. Всего найдено: %s",
        base_gift_id,
        total_found,
    )


async def scan_once(client, bot):
    log.info("Начало сканирования")

    topics = await discover_topics(client)

    catalog = await client(
        functions.payments.GetStarGiftsRequest(hash=0)
    )

    base_gifts = getattr(catalog, "gifts", []) or []

    log.info(
        "Найдено коллекций в каталоге: %s",
        len(base_gifts),
    )

    for base_gift in base_gifts:
        try:
            await scan_collection(
                client,
                bot,
                base_gift,
                topics,
            )

        except FloodWaitError as error:
            log.warning(
                "FloodWait при сканировании каталога: %s секунд",
                error.seconds,
            )
            await asyncio.sleep(error.seconds + 1)

        except Exception:
            log.exception(
                "Ошибка сканирования коллекции"
            )

    log.info("Сканирование каталога завершено")


async def run_scanner(client, bot):
    log.info("Сканер запущен")

    while True:
        try:
            await scan_once(client, bot)

        except FloodWaitError as error:
            log.warning(
                "FloodWait главного цикла: %s секунд",
                error.seconds,
            )
            await asyncio.sleep(error.seconds + 1)

        except Exception:
            log.exception("Ошибка главного цикла")

        log.info(
            "Следующее сканирование через %s секунд",
            SCAN_INTERVAL,
        )

        await asyncio.sleep(SCAN_INTERVAL)
