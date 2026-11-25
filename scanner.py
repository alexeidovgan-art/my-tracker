import asyncio
import html
import logging
import re
import unicodedata

from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from telethon import functions, types
from telethon.errors import FloodWaitError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import database


log = logging.getLogger("scanner")

GROUP_ID = -1005907973030

TOPIC_NAMES = {
    "cheap": "ширп",
    "medium": "средняк",
    "expensive": "на богатом",
}

PAGE_SIZE = 100
SCAN_INTERVAL = 60

PRICE_CHEAP = 10
PRICE_MEDIUM = 50

PRICER_USERNAME = "PriceNFTbot"
PRICER_DELAY = 2.5
PRICER_TIMEOUT = 35


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


def get_ton_price(gift):
    amounts = get_value(gift, "resell_amount")

    if amounts is None:
        return None

    if not isinstance(amounts, (list, tuple)):
        amounts = [amounts]

    for amount_obj in amounts:
        if amount_obj is None:
            continue

        class_name = amount_obj.__class__.__name__.lower()

        if "ton" not in class_name:
            continue

        raw_amount = get_value(amount_obj, "amount")

        if raw_amount is None:
            continue

        try:
            nanograms = Decimal(str(raw_amount))
            price = nanograms / Decimal("1000000000")

            if price > 0:
                return float(price)

        except (InvalidOperation, TypeError, ValueError):
            log.warning("Ошибка преобразования TON-цены: %r", raw_amount)

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


def extract_profile_url(message):
    """
    Ссылка владельца находится в сущности-ссылке первого слова.
    Не составляем URL из отображаемого имени.
    """

    text = message.raw_text or ""

    for entity in getattr(message, "entities", []) or []:
        if getattr(entity, "offset", -1) != 0:
            continue

        url = getattr(entity, "url", None)

        if url:
            return url

        if entity.__class__.__name__ == "MessageEntityUrl":
            length = getattr(entity, "length", 0)
            candidate = text[:length].strip()

            if candidate.startswith("https://t.me/"):
                return candidate

    # Дополнительная проверка: иногда ссылка присутствует прямо в тексте.
    first_line = text.splitlines()[0] if text else ""
    match = re.search(
        r"https?://t\.me/[A-Za-z0-9_]{5,32}",
        first_line
    )

    if match:
        return match.group(0)

    return None


def get_username_from_url(url):
    try:
        parsed = urlparse(url)
        if parsed.netloc.lower() not in ("t.me", "www.t.me"):
            return None

        path = parsed.path.strip("/")
        if not path or "/" in path:
            return None

        if path.startswith("+") or path.startswith("joinchat"):
            return None

        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", path):
            return None

        return path

    except Exception:
        return None


async def verify_owner(client, profile_url):
    """
    Проверяем, что ссылка ведёт на существующего обычного пользователя,
    а не на удалённый аккаунт, канал или бота.
    """

    username = get_username_from_url(profile_url)

    if not username:
        return None

    try:
        entity = await client.get_entity(username)

        if not isinstance(entity, types.User):
            return None

        if getattr(entity, "deleted", False):
            return None

        if getattr(entity, "bot", False):
            return None

        actual_username = get_value(entity, "username")

        if not actual_username:
            return None

        return {
            "id": entity.id,
            "username": actual_username,
            "name": (
                clean(get_value(entity, "first_name"))
                or clean(get_value(entity, "last_name"))
                or actual_username
            ),
            "url": f"https://t.me/{actual_username}",
        }

    except Exception as error:
        log.warning(
            "Не удалось проверить профиль %s: %s",
            profile_url,
            error
        )
        return None


async def ask_pricer(client, gift_url):
    """
    1. Отправляем ссылку калькулятору.
    2. Читаем его первое сообщение.
    3. Ищем кнопку Get profile.
    4. Нажимаем только её.
    5. Ищем новое сообщение с профилем владельца.
    """

    if not gift_url:
        return None

    try:
        bot_entity = await client.get_entity(PRICER_USERNAME)

        async with client.conversation(
            bot_entity,
            timeout=PRICER_TIMEOUT
        ) as conv:

            await conv.send_message(gift_url)
            first_message = await conv.get_response()

            floor = extract_floor(first_message.raw_text or "")

            target_button = None

            for i, row in enumerate(first_message.buttons or []):
                for j, button in enumerate(row):
                    button_text = clean(getattr(button, "text", ""))

                    if (
                        "get profile" in button_text.lower()
                        or "получить профиль" in button_text.lower()
                    ):
                        target_button = (i, j)
                        break

                if target_button:
                    break

            if target_button is None:
                log.warning(
                    "Кнопка Get profile не найдена: %s",
                    gift_url
                )
                return None

            i, j = target_button

            # Нажимаем только кнопку получения профиля.
            await first_message.click(i, j)

            profile_message = None
            original_edit_date = first_message.edit_date

            deadline = asyncio.get_running_loop().time() + PRICER_TIMEOUT

            while asyncio.get_running_loop().time() < deadline:
                messages = await client.get_messages(
                    bot_entity,
                    limit=10
                )

                for message in messages:
                    if message.id > first_message.id:
                        candidate_url = extract_profile_url(message)

                        if candidate_url:
                            profile_message = message
                            break

                if profile_message:
                    break

                # Некоторые боты редактируют исходное сообщение вместо нового.
                updated = await client.get_messages(
                    bot_entity,
                    ids=first_message.id
                )

                if updated:
                    updated_message = updated[0] if isinstance(
                        updated, list
                    ) else updated

                    if (
                        updated_message.edit_date
                        and updated_message.edit_date != original_edit_date
                    ):
                        candidate_url = extract_profile_url(updated_message)

                        if candidate_url:
                            profile_message = updated_message
                            break

                await asyncio.sleep(1)

            if not profile_message:
                log.warning(
                    "Калькулятор не прислал ссылку владельца: %s",
                    gift_url
                )
                return None

            profile_url = extract_profile_url(profile_message)

            if not floor:
                floor = extract_floor(profile_message.raw_text or "")

            if not profile_url:
                return None

            return {
                "floor": floor,
                "profile_url": profile_url,
            }

    except asyncio.TimeoutError:
        log.warning("Тайм-аут PriceNFTbot: %s", gift_url)

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
        functions.messages.GetForumTopicsRequest(
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

        log.info("Найдена тема: %s | ID: %s", title, topic.id)

        for key, expected_name in TOPIC_NAMES.items():
            if title == normalize_topic(expected_name):
                topics[key] = topic.id

    missing = [
        name
        for key, name in TOPIC_NAMES.items()
        if key not in topics
    ]

    if missing:
        raise RuntimeError(
            "Не найдены темы группы: " + ", ".join(missing)
        )

    return topics


def make_listing(data):
    def esc(value):
        return html.escape(clean(value) or "Не указано")

    return (
        f"🎁 <b>{esc(data['title'])}</b>\n\n"
        f"💰 Цена: <b>{data['price']:.3f} TON</b>\n"
        f"📊 Floor: <b>{data['floor']:.3f} TON</b>\n\n"
        f"🎨 Модель: {esc(data['model'])}\n"
        f"🖼 Фон: {esc(data['backdrop'])}\n"
        f"🔣 Узор: {esc(data['pattern'])}\n\n"
        f"👤 Владелец: {esc(data['owner'])}\n\n"
        f'<a href="{html.escape(data["gift_url"], quote=True)}">'
        f'Открыть подарок</a>'
    )


async def publish_gift(client, bot, gift, topics):
    if is_blockchain_gift(gift):
        return False

    gift_key = get_gift_key(gift)
    gift_url = get_gift_url(gift)

    if not gift_key or not gift_url:
        return False

    if database.gift_exists(gift_key):
        return False

    price = get_ton_price(gift)

    if price is None:
        log.info("Нет TON-цены: %s", gift_key)
        return False

    attrs = get_attributes(gift)

    title = (
        attrs["model"]
        or get_value(gift, "title")
        or "Telegram Gift"
    )

    pricer_result = await ask_pricer(client, gift_url)
    await asyncio.sleep(PRICER_DELAY)

    if not pricer_result:
        log.warning("Не получен профиль владельца: %s", gift_key)
        return False

    floor = pricer_result.get("floor")
    profile_url = pricer_result.get("profile_url")

    if floor is None:
        log.warning("Не удалось получить floor: %s", gift_key)
        return False

    owner = await verify_owner(client, profile_url)

    if not owner:
        log.info(
            "Подарок пропущен: профиль не прошёл проверку: %s",
            gift_key
        )
        return False

    category = get_category(floor)

    if category not in topics:
        return False

    data = {
        "gift_key": gift_key,
        "gift_url": gift_url,
        "title": title,
        "owner": owner["name"],
        "owner_url": owner["url"],
        "owner_id": owner["id"],
        "price": price,
        "floor": floor,
        "model": attrs["model"],
        "backdrop": attrs["backdrop"],
        "pattern": attrs["pattern"],
        "category": category,
    }

    # Сначала сохраняем запись, чтобы callback уже знал её ID.
    database.mark_seen(gift_key)
    row_id = database.save_gift(data)

    if not row_id:
        return False

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Занять лог",
                callback_data=f"claim:{row_id}"
            )
        ]
    ])

    try:
        message = await bot.send_message(
            chat_id=GROUP_ID,
            message_thread_id=topics[category],
            text=make_listing(data),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard
        )

        database.set_group_message(
            row_id,
            GROUP_ID,
            message.message_id
        )

        log.info(
            "Опубликован %s | цена %.3f TON | floor %.3f TON",
            gift_key,
            price,
            floor
        )

        return True

    except Exception:
        database.delete_unclaimed_gift(row_id)
        log.exception("Ошибка публикации подарка %s", gift_key)
        return False


async def scan_collection(client, bot, base_gift, topics, baseline):
    base_gift_id = get_value(base_gift, "id")

    if not base_gift_id:
        return

    offset = ""
    processed_offsets = set()
    page_number = 0

    while True:
        try:
            # Без сортировки по цене Telegram отдаёт сначала
            # подарки с наиболее недавно изменённой ценой.
            result = await client(
                functions.payments.GetResaleStarGiftsRequest(
                    gift_id=base_gift_id,
                    offset=offset,
                    limit=PAGE_SIZE
                )
            )

        except FloodWaitError as error:
            log.warning("FloodWait: %s секунд", error.seconds)
            await asyncio.sleep(error.seconds + 1)
            continue

        except Exception:
            log.exception(
                "Ошибка получения коллекции gift_id=%s",
                base_gift_id
            )
            return

        page_number += 1
        gifts = getattr(result, "gifts", []) or []

        log.info(
            "Коллекция %s | страница %s | подарков: %s",
            base_gift_id,
            page_number,
            len(gifts)
        )

        for gift in gifts:
            gift_key = get_gift_key(gift)

            if not gift_key:
                continue

            if baseline:
                database.mark_seen(gift_key)
                continue

            if database.is_seen(gift_key):
                continue

            # Отмечаем до обработки, чтобы ошибка калькулятора
            # не заставляла бесконечно повторять один и тот же запрос.
            database.mark_seen(gift_key)

            await publish_gift(
                client,
                bot,
                gift,
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

    baseline = not database.baseline_done()

    if baseline:
        log.info(
            "Первый запуск: запоминаем текущие объявления без публикации"
        )

    for base_gift in base_gifts:
        try:
            await scan_collection(
                client,
                bot,
                base_gift,
                topics,
                baseline
            )

        except FloodWaitError as error:
            log.warning("FloodWait: %s секунд", error.seconds)
            await asyncio.sleep(error.seconds + 1)

        except Exception:
            log.exception("Ошибка обработки типа подарка")

    if baseline:
        database.set_baseline_done()
        log.info("Первичная база объявлений заполнена")

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
