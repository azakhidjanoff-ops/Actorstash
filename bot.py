import html
import json
import logging
import os
import random
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- SOZLAMALAR
TOKEN = os.environ["BOT_TOKEN"]  # BotFather bergan token

# Ixtiyoriy: hisobot yuboriladigan chat (o'zingizning ID'ingiz yoki log guruhi)
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "0") or 0) or None
# Ixtiyoriy: /stats buyrug'ini ishlata oladigan adminlar ID'si (vergul bilan)
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}

# --- Kirish tekshiruvi (captcha)
TIMEOUT = 600             # to'liq tasdiqlash uchun vaqt (soniya) -> 10 daqiqa
MAX_ATTEMPTS = 3          # nechta xato urinishga ruxsat
MIN_ANSWER_SECONDS = 1.0  # bundan tez bosilgan javob = bot deb hisoblanadi

# --- Guruh moderatsiyasi
MOD_ENABLED = True
# Har bir qoida uchun harakat:
#   "delete" - faqat xabarni o'chiradi
#   "mute"   - o'chiradi va odamni yozishdan mahrum qiladi (muddati MUTE_DURATIONS da)
#   "ban"    - o'chiradi va guruhdan chiqaradi (hozir hech qaysi qoidada ishlatilmaydi)
ACTIONS = {
    "narkotik": "mute",
    "usdt": "mute",
    "kazino": "mute",
    "intim": "mute",
    "soxta hujjat": "mute",
    "reklama": "mute",          # boshqa guruh/kanal havolasi, @nomi yoki forward
    "kanal nomidan": "delete",  # kanal nomidan yozilgan xabar (kanalni jim qilib bo'lmaydi;
                                # "ban" qilsangiz o'sha kanal guruhda bloklanadi)
    "flood": "mute",
    "yangi a'zo havolasi": "delete",
}
# Yozishdan mahrum qilish muddati (soniya). 0 = admin qo'lda ochmaguncha abadiy.
MUTE_DURATIONS = {
    "default": 86400,   # 24 soat
    "flood": 600,       # 10 daqiqa
}
# Reklama hisoblanmaydigan Telegram nomlari (o'z guruhingiz/kanalingiz), kichik harfda,
# @ belgisiz. Masalan: {"actorstashkent", "mening_kanalim"}. Guruhning o'z nomi avtomatik ruxsat.
ALLOWED_TELEGRAM = set()
# Qaysi manbadan forward qilingan xabarlar o'chiriladi: "channel" (kanal), "chat" (guruh),
# "user" (odam), "hidden_user" (yashirin odam)
FORWARD_BLOCK = {"channel", "chat"}
FLOOD_MAX = 5                 # FLOOD_SECONDS ichida shuncha xabar = flood
FLOOD_SECONDS = 8
REPEAT_MAX = 3                # REPEAT_SECONDS ichida bir xil matn shuncha marta = flood
REPEAT_SECONDS = 60
NEWBIE_HOURS = 24             # yangi a'zo: qabul qilingandan keyingi shuncha soat
DELETE_SERVICE_MESSAGES = True  # «... guruhga qo'shildi/chiqdi» xabarlarini o'chirish
# ------------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)  # tokenni logda yashiradi
log = logging.getLogger("captcha-bot")

BASE = Path(__file__).parent
PENDING_FILE = BASE / "pending.json"
STATS_FILE = BASE / "stats.json"
JOINED_FILE = BASE / "joined.json"

pending: dict = {}  # "user_id:chat_id" -> holat (qayta ishga tushganda saqlanadi)
joined: dict = {}   # "user_id:chat_id" -> qabul qilingan vaqt (yangi a'zolarni bilish uchun)
stats = {"approved": 0, "declined": 0, "timeout": 0, "bots": 0, "spam": 0, "flood": 0}

# Matnlar (xabarlarni shu yerdan o'zgartirishingiz mumkin)
T = {
    "uz": {
        "ask": (
            "{note}Salom, {name}! 👋\n\n"
            "«{chat}» guruhiga qo'shilish uchun bot emasligingizni tasdiqlang.\n\n"
            "{prompt}\n\n"
            "⏳ {minutes} daqiqa ichida javob bering."
        ),
        "math": "Misolni yeching: <b>{expr} = ?</b>",
        "emoji": "Toping: <b>{animal}</b>",
        "wrong": "❌ Noto'g'ri ({n}/{m}). Yana urinib ko'ring.\n\n",
        "welcome": "✅ Tasdiqlandi! «{chat}» guruhiga qabul qilindingiz. Xush kelibsiz!",
        "handled": "ℹ️ Zapros allaqachon ko'rib chiqilgan.",
        "over": "❌ Urinishlar tugadi, zapros rad etildi.",
        "expired": "⌛ Vaqt tugadi, zapros rad etildi. Qaytadan zapros yuborishingiz mumkin.",
        "stale": "⌛ Muddat tugagan yoki allaqachon ishlangan.",
    },
    "ru": {
        "ask": (
            "{note}Здравствуйте, {name}! 👋\n\n"
            "Чтобы вступить в «{chat}», подтвердите, что вы не бот.\n\n"
            "{prompt}\n\n"
            "⏳ Ответьте в течение {minutes} минут."
        ),
        "math": "Решите пример: <b>{expr} = ?</b>",
        "emoji": "Найдите: <b>{animal}</b>",
        "wrong": "❌ Неверно ({n}/{m}). Попробуйте ещё раз.\n\n",
        "welcome": "✅ Подтверждено! Вы приняты в «{chat}». Добро пожаловать!",
        "handled": "ℹ️ Заявка уже обработана.",
        "over": "❌ Попытки закончились, заявка отклонена.",
        "expired": "⌛ Время вышло, заявка отклонена. Можно отправить заявку заново.",
        "stale": "⌛ Время истекло или уже обработано.",
    },
}

ANIMALS = {
    "🐱": {"uz": "mushuk", "ru": "кошка"},
    "🐶": {"uz": "it", "ru": "собака"},
    "🐰": {"uz": "quyon", "ru": "кролик"},
    "🐸": {"uz": "qurbaqa", "ru": "лягушка"},
    "🐼": {"uz": "panda", "ru": "панда"},
    "🦁": {"uz": "sher", "ru": "лев"},
    "🐵": {"uz": "maymun", "ru": "обезьяна"},
    "🐘": {"uz": "fil", "ru": "слон"},
}


def t(st, key, **kw):
    """Matn tanlangan tilda; til hali tanlanmagan bo'lsa, ikkala tilda."""
    lang = st.get("lang")
    if lang:
        return T[lang][key].format(**kw)
    return T["uz"][key].format(**kw) + "\n" + T["ru"][key].format(**kw)


# ------------------------------------------------------------- saqlash/yuklash
def load_state():
    for path, target in ((PENDING_FILE, pending), (STATS_FILE, stats), (JOINED_FILE, joined)):
        try:
            target.update(json.loads(path.read_text(encoding="utf-8")))
        except (FileNotFoundError, ValueError):
            pass
    prune_joined()


def save_pending():
    PENDING_FILE.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")


def save_stats():
    STATS_FILE.write_text(json.dumps(stats), encoding="utf-8")


def save_joined():
    JOINED_FILE.write_text(json.dumps(joined), encoding="utf-8")


def prune_joined():
    limit = time.time() - NEWBIE_HOURS * 3600
    for k in [k for k, ts in joined.items() if ts < limit]:
        joined.pop(k, None)


def key_of(user_id, chat_id):
    return f"{user_id}:{chat_id}"


# ------------------------------------------------------------------ savollar
def make_challenge():
    """Tasodifiy savol: misol yoki hayvon rasmini tanlash."""
    if random.random() < 0.5:
        r = random.random()
        if r < 0.4:
            a, b = random.randint(1, 20), random.randint(1, 20)
            expr, answer = f"{a} + {b}", a + b
        elif r < 0.8:
            a, b = random.randint(1, 20), random.randint(1, 20)
            a, b = max(a, b), min(a, b)
            expr, answer = f"{a} − {b}", a - b
        else:
            a, b = random.randint(2, 9), random.randint(2, 9)
            expr, answer = f"{a} × {b}", a * b
        options = {answer}
        while len(options) < 4:
            options.add(max(0, answer + random.randint(-10, 10)))
        options = [str(o) for o in options]
        random.shuffle(options)
        return {"kind": "math", "expr": expr, "answer": str(answer), "options": options}

    emoji = random.choice(list(ANIMALS))
    options = random.sample([e for e in ANIMALS if e != emoji], 3) + [emoji]
    random.shuffle(options)
    return {"kind": "emoji", "emoji": emoji, "answer": emoji, "options": options}


def challenge_text(st, ch, note=""):
    lang = st["lang"]
    if ch["kind"] == "math":
        prompt = T[lang]["math"].format(expr=ch["expr"])
    else:
        prompt = T[lang]["emoji"].format(animal=ANIMALS[ch["emoji"]][lang])
    return T[lang]["ask"].format(
        note=note,
        name=html.escape(st["name"]),
        chat=html.escape(st["chat_title"]),
        prompt=prompt,
        minutes=TIMEOUT // 60,
    )


def answer_keyboard(chat_id, options):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(o, callback_data=f"cap:{chat_id}:{o}") for o in options]]
    )


def lang_keyboard(chat_id):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data=f"lang:{chat_id}:uz"),
            InlineKeyboardButton("🇷🇺 Русский", callback_data=f"lang:{chat_id}:ru"),
        ]]
    )


def who(st):
    username = f" @{st['username']}" if st.get("username") else ""
    return f"{html.escape(st['name'])}{username} (<code>{st['user_id']}</code>)"


# ------------------------------------------------------------ yordamchilar
async def report(context, text):
    if not LOG_CHAT_ID:
        return
    try:
        await context.bot.send_message(
            LOG_CHAT_ID, text, parse_mode=ParseMode.HTML, disable_notification=True
        )
    except TelegramError as e:
        log.warning("Hisobot yuborilmadi: %s", e)


def cancel_job(job_queue, key):
    for job in job_queue.get_jobs_by_name(key):
        job.schedule_removal()


def schedule_expire(job_queue, key, delay):
    cancel_job(job_queue, key)
    job_queue.run_once(expire, max(delay, 0), data=key, name=key)


async def decline(context, st, reason, counter="declined"):
    key = key_of(st["user_id"], st["chat_id"])
    pending.pop(key, None)
    cancel_job(context.job_queue, key)
    save_pending()
    try:
        await context.bot.decline_chat_join_request(st["chat_id"], st["user_id"])
    except TelegramError as e:
        log.info("Rad etib bo'lmadi (ehtimol admin hal qilgan): %s", e)
    stats[counter] += 1
    save_stats()
    await report(
        context,
        f"❌ Rad etildi ({reason}): {who(st)}\nGuruh: {html.escape(st['chat_title'])}",
    )


# ------------------------------------------------------- kirish tekshiruvi
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    user, chat = req.from_user, req.chat

    st = {
        "user_id": user.id,
        "chat_id": chat.id,
        "user_chat_id": req.user_chat_id,
        "name": user.full_name,
        "username": user.username or "",
        "chat_title": chat.title or "guruh",
        "lang": None,
        "answer": None,
        "attempts": 0,
        "sent_at": time.time(),
        "deadline": time.time() + TIMEOUT,
    }

    if user.is_bot:
        await decline(context, st, "bot", counter="bots")
        return

    name, title = html.escape(st["name"]), html.escape(st["chat_title"])
    try:
        await context.bot.send_message(
            req.user_chat_id,
            f"Salom, {name}! 👋 / Здравствуйте!\n\n"
            f"«{title}»\n\n"
            "🌐 Tilni tanlang / Выберите язык:",
            parse_mode=ParseMode.HTML,
            reply_markup=lang_keyboard(chat.id),
        )
    except TelegramError as e:
        # Xabar yuborib bo'lmadi: zapros qo'lda ko'rib chiqish uchun qoladi.
        log.warning("Xabar yuborib bo'lmadi: %s", e)
        await report(
            context,
            f"⚠️ {who(st)} ga xabar yuborib bo'lmadi, zapros qo'lda ko'rib chiqilsin.\n"
            f"Guruh: {title}",
        )
        return

    key = key_of(user.id, chat.id)
    pending[key] = st
    save_pending()
    schedule_expire(context.job_queue, key, TIMEOUT)


async def on_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, chat_id, lang = q.data.split(":", 2)
    st = pending.get(key_of(q.from_user.id, chat_id))

    if not st:
        await q.answer(
            T["uz"]["stale"] + "\n" + T["ru"]["stale"], show_alert=True
        )
        return
    if st.get("lang") or lang not in T:
        await q.answer()
        return

    st["lang"] = lang
    ch = make_challenge()
    st.update(answer=ch["answer"], sent_at=time.time())
    save_pending()
    await q.edit_message_text(
        challenge_text(st, ch),
        parse_mode=ParseMode.HTML,
        reply_markup=answer_keyboard(st["chat_id"], ch["options"]),
    )
    await q.answer()


async def on_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, chat_id, chosen = q.data.split(":", 2)
    key = key_of(q.from_user.id, chat_id)
    st = pending.get(key)

    if not st or not st.get("lang"):
        await q.answer(
            T["uz"]["stale"] + "\n" + T["ru"]["stale"], show_alert=True
        )
        return

    too_fast = time.time() - st["sent_at"] < MIN_ANSWER_SECONDS
    if chosen == st["answer"] and not too_fast:
        pending.pop(key, None)
        cancel_job(context.job_queue, key)
        save_pending()
        try:
            await context.bot.approve_chat_join_request(st["chat_id"], st["user_id"])
        except TelegramError as e:
            log.info("Qabul qilib bo'lmadi: %s", e)
            await q.edit_message_text(t(st, "handled"))
            await q.answer()
            return
        stats["approved"] += 1
        save_stats()
        joined[key] = time.time()
        save_joined()
        await q.edit_message_text(t(st, "welcome", chat=st["chat_title"]))
        await q.answer()
        await report(
            context,
            f"✅ Qabul qilindi: {who(st)}\nGuruh: {html.escape(st['chat_title'])}",
        )
        return

    # Xato javob
    st["attempts"] += 1
    if st["attempts"] >= MAX_ATTEMPTS:
        await decline(context, st, "xato javoblar")
        await q.edit_message_text(t(st, "over"))
        await q.answer()
        return

    ch = make_challenge()
    st.update(answer=ch["answer"], sent_at=time.time())
    save_pending()
    note = t(st, "wrong", n=st["attempts"], m=MAX_ATTEMPTS)
    await q.edit_message_text(
        challenge_text(st, ch, note),
        parse_mode=ParseMode.HTML,
        reply_markup=answer_keyboard(st["chat_id"], ch["options"]),
    )
    await q.answer()


async def expire(context: ContextTypes.DEFAULT_TYPE):
    st = pending.get(context.job.data)
    if not st:
        return
    await decline(context, st, "vaqt tugadi", counter="timeout")
    try:
        await context.bot.send_message(st["user_chat_id"], t(st, "expired"))
    except TelegramError:
        pass


# ------------------------------------------------------------ MODERATSIYA
# Taqiqlangan so'zlar/iboralar (regex). Ro'yxatni o'zingiz to'ldirishingiz mumkin.
# Tekshiruv lotin/kirill o'xshash harflar (a/а, o/о, p/р ...) bilan aldashga ham chidamli.
RULES = {
    "narkotik": [
        r"наркот", r"мефедрон", r"амфетамин", r"гашиш", r"кокаин", r"героин",
        r"марихуан", r"\bспайс", r"экстази", r"\bлсд\b",
        r"mefedron", r"amfetamin", r"gashish", r"kokain", r"geroin",
        r"marixuana", r"marihuana", r"narkotik", r"\bnasha\b",
    ],
    "usdt": [
        r"usdt", r"юсдт", r"тезер", r"tether", r"trc[- ]?20", r"erc[- ]?20",
        r"\bp2p\b", r"криптовалют", r"kriptovalyuta",
    ],
    "kazino": [
        r"казино", r"casino", r"kazino", r"1xbet", r"1хбет", r"mostbet",
        r"melbet", r"pin[- ]?up", r"букмекер", r"ставки на спорт",
    ],
    "intim": [
        r"порно", r"porno", r"эскорт", r"escort", r"интим[- ]?услуг",
        r"проститутк", r"intim\s*xizmat",
    ],
    "soxta hujjat": [
        r"(куп|прода|сдела|нарису)\w*\s+(диплом|права\b|паспорт)",
        r"diplom\s+(sotaman|sotiladi|sotib)",
        r"guvohnoma\s+(sotaman|sotiladi)",
    ],
}
# Harflar orasiga bo'sh joy/belgi qo'yib yashirilgan so'zlar uchun (masalan "u s d t")
STRONG = {
    "usdt": ["usdt"],
    "narkotik": ["наркотик", "narkotik", "мефедрон", "mefedron", "кокаин", "kokain", "гашиш"],
    "kazino": ["казино", "1xbet", "mostbet"],
}

RULES_RX = {c: [re.compile(p, re.I) for p in pats] for c, pats in RULES.items()}
ZERO_WIDTH_RX = re.compile("[​‌‍⁠﻿­]")
LAT2CYR = str.maketrans("aeopcyxkmthb", "аеорсухкмтнв")
CYR2LAT = str.maketrans("аеорсухкмтнв", "aeopcyxkmthb")
LINK_RX = re.compile(
    r"(https?://|www\.|t\.me/|telegram\.me/|\b[a-z0-9-]+\.(com|ru|uz|org|net|me|io|ly)\b)",
    re.I,
)


def check_text(text):
    """Taqiqlangan toifa nomini qaytaradi, toza bo'lsa None."""
    base = unicodedata.normalize("NFKC", text).lower().replace("ё", "е")
    base = ZERO_WIDTH_RX.sub("", base)
    variants = [base, base.translate(LAT2CYR), base.translate(CYR2LAT)]
    for category, regexes in RULES_RX.items():
        for rx in regexes:
            if any(rx.search(v) for v in variants):
                return category
    squashed = [re.sub(r"[\W_]+", "", v) for v in variants]
    for category, words in STRONG.items():
        if any(w in s for w in words for s in squashed):
            return category
    return None


recent = defaultdict(deque)  # (chat_id, user_id) -> deque[(vaqt, message_id, matn)]


def flood_check(chat_id, user_id, msg_id, text):
    """Flood bo'lsa, o'chirilishi kerak bo'lgan message_id'lar ro'yxatini qaytaradi."""
    now = time.time()
    norm = text.strip().lower()[:200]
    dq = recent[(chat_id, user_id)]
    dq.append((now, msg_id, norm))
    while dq and now - dq[0][0] > max(FLOOD_SECONDS, REPEAT_SECONDS):
        dq.popleft()

    burst = [x for x in dq if now - x[0] <= FLOOD_SECONDS]
    if len(burst) >= FLOOD_MAX:
        dq.clear()
        return [x[1] for x in burst]
    if len(norm) >= 5:
        same = [x for x in dq if x[2] == norm and now - x[0] <= REPEAT_SECONDS]
        if len(same) >= REPEAT_MAX:
            dq.clear()
            return [x[1] for x in same]
    return None


def has_link(msg):
    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    if any(e.type in ("url", "text_link") for e in entities):
        return True
    return bool(LINK_RX.search(msg.text or msg.caption or ""))


TG_LINK_RX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)/([^\s/?#]+)"
    r"|tg://(?:join|resolve)\S*",
    re.I,
)
MENTION_RX = re.compile(r"(?<![\w@/])@([A-Za-z][A-Za-z0-9_]{4,31})")
_username_cache: dict = {}  # nom -> (amal qilish muddati, kanal/guruhmi)
_linked_cache: dict = {}    # chat_id -> (amal qilish muddati, bog'langan kanal id)


async def is_group_or_channel(context, username):
    """@nom kanal yoki guruhga tegishlimi (odam/bot emas)? Natija keshlanadi."""
    expires, value = _username_cache.get(username, (0, False))
    if time.time() < expires:
        return value
    try:
        found = await context.bot.get_chat("@" + username)
        value = found.type in ("channel", "supergroup", "group")
    except TelegramError:
        value = False
    _username_cache[username] = (time.time() + (86400 if value else 3600), value)
    return value


async def get_linked_chat_id(context, chat_id):
    expires, value = _linked_cache.get(chat_id, (0, None))
    if time.time() < expires:
        return value
    try:
        value = (await context.bot.get_chat(chat_id)).linked_chat_id
    except TelegramError:
        value = None
    _linked_cache[chat_id] = (time.time() + 3600, value)
    return value


async def find_ad(context, msg, chat, text):
    """Boshqa guruh/kanal reklamasi bo'lsa True: forward, Telegram havola yoki @nom."""
    allowed = {x.lower().lstrip("@") for x in ALLOWED_TELEGRAM}
    if chat.username:
        allowed.add(chat.username.lower())

    origin = msg.forward_origin
    if origin is not None and not msg.is_automatic_forward and origin.type in FORWARD_BLOCK:
        return True

    entities = list(msg.entities or ()) + list(msg.caption_entities or ())
    scan = text + " " + " ".join(e.url for e in entities if e.type == "text_link" and e.url)
    for m in TG_LINK_RX.finditer(scan):
        name = m.group(1)
        if name is None or name.lower() not in allowed:  # tg:// yoki ruxsat etilmagan nom
            return True

    names = []
    for m in MENTION_RX.finditer(text):
        n = m.group(1).lower()
        if n not in allowed and n not in names:
            names.append(n)
    for n in names[:3]:
        if await is_group_or_channel(context, n):
            return True
    return False


_admin_cache: dict = {}  # chat_id -> (amal qilish muddati, {admin id'lar})


async def get_admin_ids(context, chat_id):
    expires, ids = _admin_cache.get(chat_id, (0, set()))
    if time.time() < expires:
        return ids
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        ids = {a.user.id for a in admins}
    except TelegramError as e:
        log.warning("Adminlar ro'yxatini olib bo'lmadi: %s", e)
    _admin_cache[chat_id] = (time.time() + 300, ids)
    return ids


def user_label(user):
    username = f" @{user.username}" if user.username else ""
    return f"{html.escape(user.full_name)}{username} (<code>{user.id}</code>)"


def describe_duration(seconds):
    if not seconds:
        return "abadiy (admin ochmaguncha)"
    if seconds % 86400 == 0:
        return f"{seconds // 86400} kun"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} soat"
    return f"{max(seconds // 60, 1)} daqiqa"


async def apply_action(context, chat, user, msg_ids, category, text):
    action = ACTIONS.get(category, "delete")
    problems = []
    mute_note = ""

    try:
        if len(msg_ids) > 1:
            await context.bot.delete_messages(chat.id, msg_ids)
        else:
            await context.bot.delete_message(chat.id, msg_ids[0])
    except TelegramError as e:
        problems.append(f"o'chirib bo'lmadi: {e}")

    try:
        if action == "ban":
            await context.bot.ban_chat_member(chat.id, user.id, revoke_messages=True)
            joined.pop(key_of(user.id, chat.id), None)
        elif action == "mute":
            seconds = MUTE_DURATIONS.get(category, MUTE_DURATIONS["default"])
            until = (
                datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds else None
            )  # None = abadiy
            await context.bot.restrict_chat_member(
                chat.id, user.id, ChatPermissions.no_permissions(), until_date=until
            )
            mute_note = describe_duration(seconds)
    except TelegramError as e:
        problems.append(f"{action} qilib bo'lmadi: {e}")

    stats["flood" if category == "flood" else "spam"] += 1
    save_stats()

    icon = {"ban": "⛔", "mute": "🔇", "delete": "🗑"}.get(action, "🗑")
    lines = [
        f"{icon} {action.upper()} ({html.escape(category)}): {user_label(user)}",
        f"Guruh: {html.escape(chat.title or '')}",
    ]
    if mute_note:
        lines.append(f"Yozish huquqi olindi: {mute_note}")
    if text:
        lines.append(f"Matn: <i>{html.escape(text[:200])}</i>")
    if problems:
        lines.append("⚠️ " + "; ".join(html.escape(p) for p in problems))
        lines.append("Botga «Xabarlarni o'chirish» va «Foydalanuvchilarni bloklash» huquqini bering.")
    await report(context, "\n".join(lines))
    if problems:
        log.warning("Moderatsiya muammosi: %s", problems)


async def handle_sender_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boshqa kanal nomidan yozilgan xabar (anonim admin va bog'langan kanal bundan mustasno)."""
    msg, chat = update.effective_message, update.effective_chat
    sc = msg.sender_chat
    if sc.id == chat.id or msg.is_automatic_forward:
        return
    if sc.id == await get_linked_chat_id(context, chat.id):
        return

    action = ACTIONS.get("kanal nomidan", "delete")
    problems = []
    try:
        await msg.delete()
    except TelegramError as e:
        problems.append(f"o'chirib bo'lmadi: {e}")
    if action == "ban":
        try:
            await context.bot.ban_chat_sender_chat(chat.id, sc.id)
        except TelegramError as e:
            problems.append(f"kanalni bloklab bo'lmadi: {e}")
    stats["spam"] += 1
    save_stats()
    lines = [
        f"⛔ {action.upper()} (kanal nomidan): {html.escape(sc.title or '')}"
        f" {'@' + sc.username if sc.username else ''} (<code>{sc.id}</code>)",
        f"Guruh: {html.escape(chat.title or '')}",
    ]
    text = msg.text or msg.caption or ""
    if text:
        lines.append(f"Matn: <i>{html.escape(text[:200])}</i>")
    if problems:
        lines.append("⚠️ " + "; ".join(html.escape(p) for p in problems))
    await report(context, "\n".join(lines))


async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not MOD_ENABLED or not msg or not user:
        return
    if msg.sender_chat:
        await handle_sender_chat(update, context)
        return
    if user.is_bot or user.id in (777000, 1087968824):  # bot / Telegram xizmat akkaunti
        return
    if user.id in await get_admin_ids(context, chat.id):
        return

    text = msg.text or msg.caption or ""

    category = check_text(text) if text else None
    if category is None and await find_ad(context, msg, chat, text):
        category = "reklama"
    if category:
        await apply_action(context, chat, user, [msg.message_id], category, text)
        return

    if not update.edited_message:
        ids = flood_check(chat.id, user.id, msg.message_id, text)
        if ids:
            await apply_action(context, chat, user, ids, "flood", text)
            return

    ts = joined.get(key_of(user.id, chat.id))
    if ts and time.time() - ts < NEWBIE_HOURS * 3600:
        if has_link(msg) or msg.forward_origin is not None:
            await apply_action(context, chat, user, [msg.message_id], "yangi a'zo havolasi", text)


async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not DELETE_SERVICE_MESSAGES:
        return
    try:
        await update.effective_message.delete()
    except TelegramError:
        pass


async def cleanup(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    for k in [k for k, dq in recent.items() if not dq or now - dq[-1][0] > REPEAT_SECONDS]:
        recent.pop(k, None)
    before = len(joined)
    prune_joined()
    if len(joined) != before:
        save_joined()


# ---------------------------------------------------------------- buyruqlar
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Men guruhga zapros yuborgan odamlarni tekshiraman. Guruhga zapros tashlang, "
        "men sizga savol yuboraman.\n\n"
        "Я проверяю заявки на вступление в группу. Отправьте заявку, и я пришлю вопрос."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID: {update.effective_user.id}")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text(
        "📊 Statistika\n"
        f"✅ Qabul qilingan: {stats['approved']}\n"
        f"❌ Rad etilgan (xato javob): {stats['declined']}\n"
        f"⌛ Vaqt tugagan: {stats['timeout']}\n"
        f"🤖 Bot deb rad etilgan: {stats['bots']}\n"
        f"⏳ Hozir kutayotganlar: {len(pending)}\n\n"
        f"🚫 Spam/taqiqlangan xabarlar: {stats['spam']}\n"
        f"🌊 Flood: {stats['flood']}"
    )


async def post_init(app: Application):
    now = time.time()
    for key, st in list(pending.items()):
        schedule_expire(app.job_queue, key, st["deadline"] - now)
    if pending:
        log.info("Qayta tiklandi: %d ta kutayotgan zapros", len(pending))
    app.job_queue.run_repeating(cleanup, interval=1800, first=1800)


def main():
    load_state()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(CallbackQueryHandler(on_lang, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(on_answer, pattern=r"^cap:"))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("stats", cmd_stats))
    # Moderatsiya (guruhlarda)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER),
            on_service_message,
        ),
        group=1,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, moderate),
        group=1,
    )
    log.info("Bot ishga tushdi")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
