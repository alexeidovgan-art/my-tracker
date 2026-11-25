import asyncio
import html
import logging
import re
import unicodedata

from telethon import functions, types
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.errors import FloodWaitError

import database


log = logging.getLogger("scanner")

GROUP_ID = -1005907973030

TOPIC_NAMES = {
    "cheap": "дешевые",
    "medium": "средние",
    "expensive": "дорогие",
}

PAGE_SIZE = 100
SCAN_INTERVAL = 60
PRICER_DELAY = 2.5

PRICE_CHEAP = 10
PRICE_MEDIUM = 50

PRICER_USERNAME = "PriceNFTbot"


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def normalize_topic(value):
    value = clean(value).lower().replace("ё", "е")
    value = unicodedata.normalize("NFKC", value)
    return re.sub(r"\s+", " ", value)


def get_value(obj, *names):
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def get_gift_key(gift):
    value = get_value(gift, "slug", "id")
    return str(value) if value is not None else None


def get_gift_url(gift):
    slug = get_value(gift, "slug")
    if slug:
        return f"https://t.me/nft/{slug}"

    gift_id = get_value(gift, "id")
    if gift_id:
        return f"https://t.me/nft/{gift_id}"

    return ""


def get_attributes(gift):
    result = {
        "model": "",
        "backdrop": "",
        "pattern": "",
    }

    for attr in get_value(gift, "attributes") or []:
        class_name = attr.__class__.__name__.lower()
        name_obj = get_value(attr, "name")
        name = get_value(name_obj, "text") or clean(name_obj)

        if "model" in class_name:
            result["model"] = name
        elif "backdrop" in class_name:
            result["backdrop"] = name
        elif "pattern" in class_name:
            result["pattern"] = name

    return result


def is_blockchain_gift(gift):
    return bool(
        get_value(gift, "gift_address")
        or get_value(gift, "owner_address")
    )


def get_owner(gift, users):
    owner_id = get_value(gift, "owner_id")

    if isinstance(owner_id, types.PeerUser):
        owner_id = owner_id.user_id

    if not owner_id:
        return "Неизвестен", ""

    for user in users or []:
        if getattr(user, "id", None) == owner_id:
            username = get_value(user, "username")

            if username:
                return f"@{username}", f"https://t.me/{username}"

            return (
                f"ID {owner_id}",
                f"tg://user?id={owner_id}"
            )

    return f"ID {owner_id}", f"tg://user?id={owner_id}"


def get_ton_price(gift):
    amount = get_value(gift, "resell_amount")

    if amount is None:
        return None

    class_name = amount.__class__.__name__.lower()

    if "ton" not in class_name:
        return None

    whole = get_value(amount, "amount") or 0
    nanos = get_value(amount, "nanos") or 0

    try:
        return float(whole) + float(nanos) / 1_000_000_000
    except (TypeError, ValueError):
        return None


def extract_floor(text):
    if not text:
        return None

    patterns = [
        r"floor(?:\s+price)?\s*[:：—-]?\s*([\d.,]+)\s*TON",
        r"флор(?:\s+цена)?\s*[:：—-]?\s*([\d.,]+)\s*TON",
        r"([\d.,]+)\s*TON\s*(?:floor|флор)",
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
    if not gift_url:
        return None

    try:
        async with client.conversation(
            PRICER_USERNAME,
            timeout=30
        ) as conv:
            await conv.send_message(gift_url)
            response = await conv.get_response()

            text = response.raw_text or ""
            return extract_floor(text)

    except asyncio.TimeoutError:
        log.warning("PriceNFTbot не ответил на %s", gift_url)
    except Exception:
        log.exception("Ошибка обращения к PriceNFTbot")

    return None


def get_category(floor):
    if floor is None:
        return None

    if floor < PRICE_CHEAP:
        return "cheap"

    if floor < PRICE_MEDIUM:
        return "medium"

    return "expensive"


async def discover_topics(client):
    entity = await client.get_entity(GROUP_ID)

    log.info(
        "Подключена группа: %s",
        get_value(entity, "title") or GROUP_ID
    )

    result = await client(
        GetForumTopicsRequest(
            peer=entity,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100
        )
    )

    topics = {}

    for topic in getattr(result, "topics", []):
        title = normalize_topic(get_value(topic, "title"))

        log.info(
            "Найдена тема: %s | ID: %s",
            title,
            topic.id
        )

        for key, expected_name in TOPIC_NAMES.items():
            if title == normalize_topic(expected_name):
                topics[key] = topic.id

    missing = [
        name for key, name in TOPIC_NAMES.items()
        if key not in topics
    ]

    if missing:
        raise RuntimeError(
            "В группе не найдены темы: " + ", ".join(missing)
        )

    return topics


def make_listing(data):
    def esc(value):
        return html.escape(clean(value) or "Не указано")

    owner = esc(data["owner"])
    owner_url = data.get("owner_url")

    if owner_url:
        owner_line = f'<a href="{html.escape(owner_url, quote=True)}">{owner}</a>'
    else:
        owner_line = owner

    return (
        f"🎁 <b>{esc(data['title'])}</b>\n\n"
        f"💰 Цена: <b>{data['price']:.3f} TON</b>\n"
        f"📊 Floor: <b>{data['floor']:.3f} TON</b>\n\n"
        f"🎨 Модель: {esc(data['model'])}\n"
        f"🖼 Фон: {esc(data['backdrop'])}\n"
        f"🔣 Узор: {esc(data['pattern'])}\n\n"
        f"👤 Владелец: {owner_line}\n\n"
        f'🔗 <a href="{html.escape(data["gift_url"], quote=True)}">'
        f"Открыть подарок</a>"
    )


async def publish_gift(client, bot, gift, users, topics):
    if is_blockchain_gift(gift):
        return

    gift_key = get_gift_key(gift)
    gift_url = get_gift_url(gift)

    if not gift_key or not gift_url:
        return

    if database.gift_exists(gift_key):
        return

    price = get_ton_price(gift)

    if price is None:
        log.info("Пропущен подарок без цены в TON: %s", gift_key)
        return

    attrs = get_attributes(gift)
    owner, owner_url = get_owner(gift, users)

    title = (
        attrs["model"]
        or get_value(gift, "title")
        or "Telegram Gift"
    )

    floor = await ask_pricer(client, gift_url)
    await asyncio.sleep(PRICER_DELAY)

    if floor is None:
        log.warning(
            "Не удалось получить floor от PriceNFTbot: %s",
            gift_url
        )
        return

    category = get_category(floor)

    if category not in topics:
        return

    data = {
        "gift_key": gift_key,
        "gift_url": gift_url,
        "title": title,
        "owner": owner,
        "owner_url": owner_url,
        "price": price,
        "floor": floor,
        "model": attrs["model"],
        "backdrop": attrs["backdrop"],
        "pattern": attrs["pattern"],
        "category": category,
    }

    message = make_listing(data)

    try:
        await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=topics[category],
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )

        database.save_gift(data)

        log.info(
            "Опубликован %s | цена %.3f TON | floor %.3f TON",
            gift_key,
            price,
            floor
        )

    except Exception:
        log.exception("Ошибка публикации подарка %s", gift_key)


async def scan_collection(client, bot, base_gift, topics):
    base_gift_id = get_value(base_gift, "id")

    if not base_gift_id:
        return

    offset = ""
    processed_offsets = set()

    while True:
        try:
            result = await client(
                functions.payments.GetResaleStarGiftsRequest(
                    gift_id=base_gift_id,
                    offset=offset,
                    limit=PAGE_SIZE,
                    sort_by_price=True
                )
            )

        except FloodWaitError as error:
            log.warning("FloodWait: ожидание %s секунд", error.seconds)
            await asyncio.sleep(error.seconds + 1)
            continue

        except Exception:
            log.exception(
                "Ошибка получения перепродаж для gift_id=%s",
                base_gift_id
            )
            return

        gifts = getattr(result, "gifts", []) or []
        users = getattr(result, "users", []) or []

        for gift in gifts:
            await publish_gift(
                client,
                bot,
                gift,
                users,
                topics
            )

        next_offset = getattr(result, "next_offset", None)

        if not next_offset or next_offset in processed_offsets:
            break

        processed_offsets.add(next_offset)
        offset = next_offset


async def scan_once(client, bot):
    log.info("Начало сканирования")

    topics = await discover_topics(client)

    result = await client(
        functions.payments.GetStarGiftsRequest(hash=0)
    )

    base_gifts = getattr(result, "gifts", []) or []

    log.info("Найдено типов подарков: %s", len(base_gifts))

    for base_gift in base_gifts:
        try:
            await scan_collection(
                client,
                bot,
                base_gift,
                topics
            )
        except FloodWaitError as error:
            log.warning("FloodWait: %s секунд", error.seconds)
            await asyncio.sleep(error.seconds + 1)
        except Exception:
            log.exception("Ошибка обработки типа подарка")

    log.info("Сканирование завершено")


async def run_scanner(client, bot):
    database.init_db()

    while True:
        try:
            await scan_once(client, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка основного цикла сканера")

        await asyncio.sleep(SCAN_INTERVAL)
