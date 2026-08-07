import telebot
import requests
import time
import sqlite3
import threading
import os
import re
import html
from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions

# =========================================================
# Configuration — set these in Render -> Environment
# =========================================================
API_TOKEN = os.environ["API_TOKEN"]                          # REQUIRED
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))  # private log channel id
DB_NAME = os.environ.get("DB_PATH", "spam_bot.db")           # point at a persistent disk on Render!

STRIKE_LIMIT = 3
STRIKE_TTL = 30 * 86400        # strikes older than 30 days no longer count
SPAM_SCORE_THRESHOLD = 2       # delete + strike at this score
BAN_SCORE_THRESHOLD = 5        # delete + INSTANT PERMANENT BAN at this score
CAPTCHA_TIMEOUT = 120
MEDIA_LOCK_SECONDS = 86400
PROBATION_SECONDS = 86400
TRUSTED_MIN_MESSAGES = 30      # members with this many clean messages skip flood/probation
TOXIC_LIMIT = 3                # profanity warnings before a temporary mute
TOXIC_MUTE_SECONDS = 7 * 86400 # "ban for a week" = 7-day mute
TOXIC_TTL = 30 * 86400         # profanity counter decays after 30 days
ADMIN_CACHE_TTL = 300
CAS_CACHE_TTL = 3600

bot = telebot.TeleBot(API_TOKEN)

# =========================================================
# Web server (keeps the bot alive on Render)
# =========================================================
app = Flask(__name__)

@app.route('/')
def keep_alive():
    return "Bot is alive and actively guarding the group!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# =========================================================
# Text normalization + homoglyph handling
# =========================================================
ZERO_WIDTH = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff'), None)
LAT_TO_CYR = str.maketrans('aceiopxyk03', 'асеіорхукоз')
CYR_TO_LAT = str.maketrans('асеіорхук0', 'aceiopxyko')

def normalize_text(text):
    """Lowercase, strip evasion characters, unify ё->е, collapse spaces.
    Dashes become spaces so 'онлайн-співпраця' stays two words."""
    t = text.lower().translate(ZERO_WIDTH)
    t = t.replace('ё', 'е')
    t = re.sub(r'[\-\—\–]', ' ', t)
    t = re.sub(r"[.\_\*\@\#\$\%\^\&\(\)\+\=\~\'\"\!\?\,\:\;]", '', t)
    return ' '.join(t.split())

def normalize_keyword(word):
    """Like normalize_text, but preserves a trailing * (suffix wildcard)."""
    star = word.strip().endswith('*')
    n = normalize_text(word)
    return n + '*' if star and n else n

def text_variants(text):
    base = normalize_text(text)
    return {base, base.translate(LAT_TO_CYR), base.translate(CYR_TO_LAT)}

# ---- Patterns checked on RAW (lowercased) text ----
CONTACT_RX = re.compile(r'@[a-zA-Z][a-zA-Z0-9_]{3,}|t\.me/|telegram\.me/', re.I)
CURRENCY = r'(?:usdt?|euro?|dkk|kroner|kr|евро|євро|крон\w*|грн|грив\w*|долл\w*|долар\w*)'
MONEY_RX = re.compile(
    r'[\$€£₴]\s*\d|\d\s*[\$€£₴]|'
    r'\d\s*' + CURRENCY + r'\b', re.I)
# "700 евро в неделю" / "260 долларов США в день" — EUR/USD pay-rate promises
PAYRATE_RX = re.compile(
    r'\d[\d\s.,]*\s*(?:[\$€]|usdt?|euro?|евро|євро|долл\w*|долар\w*)\s*(?:сша|usa)?\s*'
    r'(?:в|на|per|/)\s*(?:день|сутки|неделю|месяц|тиждень|місяць|day|week|month)', re.I)
# Selling tobacco = instant ban ("продам сигарети/сігарети/цигарки/вейпи...")
SELL_RX = re.compile(r'(?<!\w)(?:продам|продаю|продаж\w*|є в наявності|в наличии)(?!\w)', re.I)
TOBACCO_RX = re.compile(
    r'сигарет\w*|сігарет\w*|сиграет\w*|сігрaет\w*|цигарк\w*|цыгарк\w*|'
    r'тютюн\w*|табак\w*|айкос\w*|iqos|стіки|стики|вейп\w*|одноразк\w*|снюс\w*', re.I)
# P2P crypto deals ("куплю юсдт", "продам usdt за нал") — +1 scoring point
CRYPTO_TRADE_RX = re.compile(
    r'(?<!\w)(?:куплю|купить|купити|продам|продаю|обмен\w*|обміня\w*|обмін|'
    r'вывод\w*|виведу|обнал\w*)(?!\w).{0,80}?'
    r'(?:usdt|юсдт|усдт|тезер|tether|крипт\w*|біткоїн\w*|биткоин\w*|btc)', re.I | re.S)

# =========================================================
# Database
# =========================================================
def db():
    return sqlite3.connect(DB_NAME)

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS user_strikes '
                  '(user_id INTEGER PRIMARY KEY, strikes INTEGER DEFAULT 0, last_at REAL DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS spam_keywords '
                  "(word TEXT PRIMARY KEY, tier TEXT DEFAULT 'strong')")
        c.execute('CREATE TABLE IF NOT EXISTS verified_members '
                  '(user_id INTEGER PRIMARY KEY, verified_at REAL)')
        c.execute('CREATE TABLE IF NOT EXISTS allowed_channels (name TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE IF NOT EXISTS user_stats '
                  '(user_id INTEGER PRIMARY KEY, msg_count INTEGER DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS toxic_strikes '
                  '(user_id INTEGER PRIMARY KEY, count INTEGER DEFAULT 0, last_at REAL DEFAULT 0)')
        conn.commit()
        for stmt in ("ALTER TABLE user_strikes ADD COLUMN last_at REAL DEFAULT 0",
                     "ALTER TABLE spam_keywords ADD COLUMN tier TEXT DEFAULT 'strong'"):
            try:
                c.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass
    for tier, words in DEFAULT_KEYWORDS.items():
        for w in words:
            add_spam_keyword(w, tier=tier, rebuild=False)
    for ch in DEFAULT_ALLOWED_CHANNELS:
        allow_channel(ch)
    rebuild_patterns()

def _counter_bump(table, user_id, ttl):
    """Shared logic for expiring counters (spam strikes, toxic warnings)."""
    col = 'strikes' if table == 'user_strikes' else 'count'
    now = time.time()
    with db() as conn:
        c = conn.cursor()
        c.execute(f"SELECT {col}, last_at FROM {table} WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        value = row[0] + 1 if row and now - (row[1] or 0) <= ttl else 1
        c.execute(f"INSERT INTO {table} (user_id, {col}, last_at) VALUES (?, ?, ?) "
                  f"ON CONFLICT(user_id) DO UPDATE SET {col} = ?, last_at = ?",
                  (user_id, value, now, value, now))
        conn.commit()
    return value

def add_strike(user_id):
    return _counter_bump('user_strikes', user_id, STRIKE_TTL)

def add_toxic(user_id):
    return _counter_bump('toxic_strikes', user_id, TOXIC_TTL)

def reset_strikes(user_id):
    with db() as conn:
        conn.execute("DELETE FROM user_strikes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM toxic_strikes WHERE user_id = ?", (user_id,))
        conn.commit()

def bump_msg_count(user_id):
    with db() as conn:
        conn.execute("INSERT INTO user_stats (user_id, msg_count) VALUES (?, 1) "
                     "ON CONFLICT(user_id) DO UPDATE SET msg_count = msg_count + 1", (user_id,))
        conn.commit()

def set_msg_count(user_id, value):
    with db() as conn:
        conn.execute("INSERT INTO user_stats (user_id, msg_count) VALUES (?, ?) "
                     "ON CONFLICT(user_id) DO UPDATE SET msg_count = ?", (user_id, value, value))
        conn.commit()

def is_trusted(user_id):
    with db() as conn:
        row = conn.execute("SELECT msg_count FROM user_stats WHERE user_id = ?",
                           (user_id,)).fetchone()
    return bool(row) and row[0] >= TRUSTED_MIN_MESSAGES

def add_spam_keyword(word, tier='strong', rebuild=True):
    word = normalize_keyword(word)
    if not word:
        return
    with db() as conn:
        conn.execute("INSERT INTO spam_keywords (word, tier) VALUES (?, ?) "
                     "ON CONFLICT(word) DO UPDATE SET tier = excluded.tier", (word, tier))
        conn.commit()
    if rebuild:
        rebuild_patterns()

def remove_spam_keyword(word, rebuild=True):
    word = normalize_keyword(word)
    with db() as conn:
        conn.execute("DELETE FROM spam_keywords WHERE word = ?", (word,))
        conn.commit()
    if rebuild:
        rebuild_patterns()

def get_spam_keywords():
    with db() as conn:
        return list(conn.execute("SELECT word, tier FROM spam_keywords ORDER BY tier, word"))

def mark_verified(user_id):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO verified_members (user_id, verified_at) VALUES (?, ?)",
                     (user_id, time.time()))
        conn.commit()

def in_probation(user_id):
    with db() as conn:
        row = conn.execute("SELECT verified_at FROM verified_members WHERE user_id = ?",
                           (user_id,)).fetchone()
    return bool(row) and time.time() - row[0] < PROBATION_SECONDS

def allow_channel(name):
    name = str(name).lstrip('@').lower().strip()
    if not name:
        return
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO allowed_channels (name) VALUES (?)", (name,))
        conn.commit()

def disallow_channel(name):
    name = str(name).lstrip('@').lower().strip()
    with db() as conn:
        conn.execute("DELETE FROM allowed_channels WHERE name = ?", (name,))
        conn.commit()

def get_allowed_channels():
    with db() as conn:
        return {row[0] for row in conn.execute("SELECT name FROM allowed_channels")}

# =========================================================
# Keyword matching: tiers ban / strong / weak / toxic,
# optional trailing * = suffix wildcard ("тезер*" matches "тезера")
# =========================================================
_patterns_lock = threading.Lock()
_patterns = []  # (keyword, tier, compiled_regex)

def rebuild_patterns():
    global _patterns
    pats = []
    for kw, tier in get_spam_keywords():
        if kw.endswith('*'):
            rx = re.compile(r'(?<!\w)' + re.escape(kw[:-1]) + r'\w*')
        else:
            rx = re.compile(r'(?<!\w)' + re.escape(kw) + r'(?!\w)')
        pats.append((kw, tier, rx))
    with _patterns_lock:
        _patterns = pats

def match_keywords(raw_text):
    """Returns {tier: set(keywords)} matched across homoglyph variants."""
    variants = text_variants(raw_text)
    with _patterns_lock:
        pats = list(_patterns)
    hits = {'ban': set(), 'strong': set(), 'weak': set(), 'toxic': set()}
    for kw, tier, rx in pats:
        if tier in hits and any(rx.search(v) for v in variants):
            hits[tier].add(kw)
    # Don't double-count keywords contained in a longer matched phrase
    all_matched = set().union(*hits.values())
    for tier in hits:
        hits[tier] = {k for k in hits[tier]
                      if not any(k != o and k.rstrip('*') in o for o in all_matched)}
    return hits

def score_message(raw_text, hits):
    raw = raw_text.lower()
    money_contact = bool(MONEY_RX.search(raw) and CONTACT_RX.search(raw))
    payrate = bool(PAYRATE_RX.search(raw))
    crypto_trade = bool(CRYPTO_TRADE_RX.search(raw))

    score = (2 * len(hits['strong']) + len(hits['weak'])
             + (1 if money_contact else 0) + (1 if payrate else 0)
             + (1 if crypto_trade else 0))
    parts = []
    if hits['strong']:
        parts.append("strong: " + ", ".join(sorted(hits['strong'])))
    if hits['weak']:
        parts.append("weak: " + ", ".join(sorted(hits['weak'])))
    if money_contact:
        parts.append("pattern: money+contact")
    if payrate:
        parts.append("pattern: pay-rate promise")
    if crypto_trade:
        parts.append("pattern: crypto trade")
    return score, " | ".join(parts)

# =========================================================
# Caches
# =========================================================
_admin_cache = {}
_cas_cache = {}

def is_admin(chat_id, user_id):
    now = time.time()
    cached = _admin_cache.get(chat_id)
    if cached and now - cached[0] < ADMIN_CACHE_TTL:
        return user_id in cached[1]
    try:
        admins = {a.user.id for a in bot.get_chat_administrators(chat_id)}
        _admin_cache[chat_id] = (now, admins)
        return user_id in admins
    except Exception:
        return user_id in cached[1] if cached else False

def check_cas_banned(user_id):
    now = time.time()
    cached = _cas_cache.get(user_id)
    if cached and now - cached[0] < CAS_CACHE_TTL:
        return cached[1]
    banned = False
    try:
        r = requests.get(f"https://api.cas.chat/check?user_id={user_id}", timeout=5).json()
        banned = bool(r.get("ok", False))
    except Exception:
        pass
    _cas_cache[user_id] = (now, banned)
    return banned

# =========================================================
# Helpers
# =========================================================
def log_event(text_html):
    if not LOG_CHANNEL_ID:
        print(text_html)
        return
    try:
        bot.send_message(LOG_CHANNEL_ID, text_html, parse_mode="HTML")
    except Exception as e:
        print(f"Log error: {e}")

def user_label(user):
    name = html.escape(user.first_name or "?")
    uname = f" @{html.escape(user.username)}" if user.username else ""
    return f"{name}{uname} (id <code>{user.id}</code>)"

def delete_silently(chat_id, message_id):
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass

def soft_kick(chat_id, user_id):
    try:
        bot.ban_chat_member(chat_id, user_id)
        bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
    except Exception as e:
        print(f"Soft-kick error: {e}")

def hard_ban(chat_id, user_id):
    try:
        bot.ban_chat_member(chat_id, user_id)
    except Exception as e:
        print(f"Ban error: {e}")

def mute_for(chat_id, user_id, seconds):
    try:
        bot.restrict_chat_member(chat_id, user_id,
                                 until_date=int(time.time()) + seconds,
                                 permissions=ChatPermissions(can_send_messages=False))
    except Exception as e:
        print(f"Mute error: {e}")

def temp_reply(message, text, delay=5):
    try:
        sent = bot.reply_to(message, text, parse_mode="HTML")
        threading.Timer(delay, delete_silently, args=(message.chat.id, sent.message_id)).start()
        threading.Timer(delay, delete_silently, args=(message.chat.id, message.message_id)).start()
    except Exception:
        pass

def temp_notice(chat_id, text, delay=10):
    """Visible self-destructing notice (used only for toxicity warnings)."""
    try:
        sent = bot.send_message(chat_id, text, parse_mode="HTML")
        threading.Timer(delay, delete_silently, args=(chat_id, sent.message_id)).start()
    except Exception:
        pass

def command_arg(message):
    parts = (message.text or '').split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ''

def forwarded_channel(message):
    ch = getattr(message, 'forward_from_chat', None)
    if ch is not None and getattr(ch, 'type', '') == 'channel':
        return ch
    origin = getattr(message, 'forward_origin', None)
    if origin is not None and getattr(origin, 'type', '') == 'channel':
        return getattr(origin, 'chat', None)
    return None

# =========================================================
# Default keyword library
#   ban    -> one hit = delete + PERMANENT BAN (admin-managed, starts empty)
#   strong -> 2 points  |  weak -> 1 point  (threshold 2 = delete+strike,
#   5+ = delete+ban)    |  toxic -> profanity counter (3 -> 7-day mute)
# =========================================================
DEFAULT_KEYWORDS = {
    'ban': [],
    'strong': [
        # English
        "casino", "jackpot", "free spins", "1win",
        "onlyfans", "escort", "sugar daddy", "sugar mommy",
        "adult content", "hot photos", "private photos",
        "airdrop", "trading signals", "crypto signals", "pump and dump",
        "binary options", "guaranteed profit", "guaranteed income",
        "double your money", "cloud mining", "mining pool",
        "usdt", "ustd", "trc20", "erc20", "tether",
        "free money", "easy money", "quick cash", "fast cash",
        "get rich", "financial freedom", "passive income",
        "telegram premium free", "free premium",
        # Ukrainian
        "казино", "ставки на спорт", "букмекер", "бонус за реєстрацію",
        "ескорт", "інтим", "вебкам",
        "схема заробітку", "робоча схема", "безкоштовні гроші",
        "легкі гроші", "швидкі гроші", "заробіток в інтернеті",
        "пасивний дохід", "надішлю інформацію",
        "набір у команду",
        "розкрити свій потенціал",
        "онлайн співпрац*", "робота з телефону", "навчання з нуля",
        "заняття безкоштовно", "урок безкоштовно",
        "пробне заняття", "пробний урок",
        "чекаю на ваші повідомлення",
        "дивіться в профілі", "дивись в профілі", "інфа в профілі",
        "набираю дітей", "набір дітей", "шукаю дівчаток",
        # Russian
        "бонус за регистрацию", "эскорт", "интим",
        "схема заработка", "рабочая схема", "бесплатные деньги", "халява",
        "легкие деньги", "быстрые деньги", "заработок в интернете",
        "пассивный доход", "пришлю информацию", "пришлю вам интересующую",
        "набор в команду",
        "раскрыть свой потенциал", "справляться с любыми задачами",
        "онлайн сотрудничеств*", "работа с телефона", "обучение с нуля",
        "занятие бесплатно", "урок бесплатно",
        "пробное занятие", "пробный урок",
        "жду ваши сообщения", "жду ваших сообщений", "ожидаю вашего сообщения",
        "смотрите в профиле", "смотри в профиле", "инфо в профиле", "инфа в профиле",
        "юсдт", "усдт", "тезер*",
        "ищем русскоязычных", "набираю ребят", "набираю детей", "набор детей",
        "ищу девушек", "ищу девочек",
    ],
    'weak': [
        # English
        "crypto", "bitcoin", "invest", "forex", "binance", "giveaway",
        "dm me", "join channel", "work from home", "no experience needed",
        "limited spots", "click the link", "link in bio", "check my bio",
        "referral link", "earn cash", "earn daily", "daily profit",
        "weekly payout", "instant payout", "promo code",
        "sign up bonus", "welcome bonus", "presale",
        # Ukrainian
        "крипта", "криптовалюта", "біткоїн", "інвестиції", "інвестувати",
        "трейдинг", "сигнали", "заробіток", "заробити", "дохід", "доходу", "доходів",
        "зарплата від", "гарна зарплата", "без вкладень", "вкладення від",
        "щоденні виплати", "виплати щодня", "потрібні люди",
        "набираємо людей", "шукаємо людей", "потрібен персонал",
        "віддалена робота", "робота вдома", "вільний графік",
        "пиши в пп", "в пп", "пиши в особисті", "пишіть в особисті",
        "в приватні повідомлення", "деталі в особистих",
        "звертайтесь в особисті", "звертайтеся в особисті", "у приват", "в приват",
        "переходь за посиланням", "тисни на посилання",
        "телеграм канал", "приєднуйся", "бонус", "промокод",
        "розіграш", "ставки",
        "виїзд", "виїзд за кордон", "білий квиток", "відстрочка", "сзч",
        "оплата на картку", "продам євро", "куплю євро", "обмін валют",
        "кількість місць обмежена", "дізнатися деталі",
        # Russian
        "биткоин", "инвестиции", "инвестировать", "трейдинг", "трейдер",
        "сигналы", "заработок", "заработать", "доход", "дохода",
        "потенциал дохода",
        "зарплата от", "хорошая зарплата", "без вложений", "вложения от",
        "ежедневные выплаты", "выплаты каждый день", "первые выплаты",
        "нужны люди", "набираем людей", "ищем людей",
        "требуются сотрудники", "требуются люди", "нужен персонал",
        "удаленная работа", "работа на дому", "удаленный формат",
        "дистанционн*", "русскоязычн*", "свободный график", "гибкий график",
        "совмещение с основной",
        "пиши в лс", "пишите в лс", "в лс", "пиши в личку", "пишите в личку",
        "в личные сообщения", "подробности в лс",
        "переходи по ссылке", "жми на ссылку", "приватный канал",
        "розыгрыш",
        "выезд", "выезд за границу", "белый билет", "отсрочка",
        "оплата на карту", "продам евро", "куплю евро", "обмен валют", "за нал",
        "количество мест ограничено", "узнать детали",
        "сигареты", "сигарети", "цигарки",
    ],
    'toxic': [
        "хуй*", "хуе*", "хуи*", "нахуй", "похуй", "пизд*", "бляд*", "блят*",
        "еба*", "ебан*", "ебл*", "заеб*", "уеб*", "долбоеб*",
        "мудак*", "мудач*", "гандон*", "гондон*",
        "пидор*", "пидар*", "підар*", "підор*",
        "дебил*", "дебіл*", "сука", "суч*", "курв*", "шлюх*", "чмо",
        "тупой", "тупая", "тупые", "дурак", "дурач*", "попуск*",
        "идиот*", "ідіот*", "кретин*", "придурок", "придурк*", "долбан*",
    ],
}

DEFAULT_ALLOWED_CHANNELS = ["ua_diaspora_dk"]

# =========================================================
# Admin commands (confirmations self-destruct after 5 s)
# =========================================================
def _add_kw_command(message, tier, label):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keyword = command_arg(message)
    if keyword:
        add_spam_keyword(keyword, tier=tier)
        temp_reply(message, f"✅ Added <b>{html.escape(keyword)}</b> as {label}.")
        log_event(f"➕ {label} keyword added by {user_label(message.from_user)}: "
                  f"<code>{html.escape(keyword)}</code>")
    else:
        temp_reply(message, f"Usage: <code>/{message.text.split()[0].lstrip('/')} word or phrase</code>")

@bot.message_handler(commands=['addspam'])
def handle_add_spam(message):
    _add_kw_command(message, 'strong', 'STRONG (2 pts)')

@bot.message_handler(commands=['addweak'])
def handle_add_weak(message):
    _add_kw_command(message, 'weak', 'WEAK (1 pt)')

@bot.message_handler(commands=['addban'])
def handle_add_ban(message):
    _add_kw_command(message, 'ban', 'INSTANT-BAN')

@bot.message_handler(commands=['addtoxic'])
def handle_add_toxic(message):
    _add_kw_command(message, 'toxic', 'TOXIC')

@bot.message_handler(commands=['delspam', 'deltoxic'])
def handle_del_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keyword = command_arg(message)
    if keyword:
        remove_spam_keyword(keyword)
        temp_reply(message, f"❌ Removed <b>{html.escape(keyword)}</b>.")
        log_event(f"➖ Keyword removed by {user_label(message.from_user)}: "
                  f"<code>{html.escape(keyword)}</code>")
    else:
        temp_reply(message, "Usage: <code>/delspam word or phrase</code>")

@bot.message_handler(commands=['listspam'])
def handle_list_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keywords = get_spam_keywords()
    temp_reply(message, f"📋 {len(keywords)} keywords — full list sent to the log channel.")
    lines, current_tier = [], None
    for w, tier in keywords:
        if tier != current_tier:
            lines.append(f"\n<b>{tier.upper()}:</b>")
            current_tier = tier
        lines.append(f"• <code>{html.escape(w)}</code>")
    chunks, chunk = [], []
    for ln in lines:
        chunk.append(ln)
        if len(chunk) == 80:
            chunks.append("\n".join(chunk)); chunk = []
    if chunk:
        chunks.append("\n".join(chunk))
    for i, part in enumerate(chunks, 1):
        log_event(f"📋 <b>Spam keywords ({len(keywords)}), part {i}/{len(chunks)}:</b>\n{part}")

@bot.message_handler(commands=['allowfwd'])
def handle_allow_fwd(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    name = command_arg(message)
    if name:
        allow_channel(name)
        temp_reply(message, f"✅ Forwards from <b>@{html.escape(name.lstrip('@'))}</b> allowed.")
        log_event(f"📢 Forward whitelist + <code>{html.escape(name)}</code> by {user_label(message.from_user)}")
    else:
        temp_reply(message, "Usage: <code>/allowfwd @channel_username</code>")

@bot.message_handler(commands=['delfwd'])
def handle_del_fwd(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    name = command_arg(message)
    if name:
        disallow_channel(name)
        temp_reply(message, f"❌ Forwards from <b>@{html.escape(name.lstrip('@'))}</b> removed.")
        log_event(f"📢 Forward whitelist - <code>{html.escape(name)}</code> by {user_label(message.from_user)}")
    else:
        temp_reply(message, "Usage: <code>/delfwd @channel_username</code>")

@bot.message_handler(commands=['listfwd'])
def handle_list_fwd(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    channels = sorted(get_allowed_channels())
    listing = "\n".join(f"• @{html.escape(c)}" for c in channels) or "— empty —"
    temp_reply(message, "📢 Whitelist sent to the log channel.")
    log_event(f"📢 <b>Allowed forward sources:</b>\n{listing}")

@bot.message_handler(commands=['trust'])
def handle_trust(message):
    """Reply to someone's message with /trust to exempt them from
    flood detection and probation."""
    if not is_admin(message.chat.id, message.from_user.id):
        return
    target = getattr(message.reply_to_message, 'from_user', None) if message.reply_to_message else None
    if target:
        set_msg_count(target.id, 100000)
        temp_reply(message, f"🤝 {html.escape(target.first_name or '')} is now trusted.")
        log_event(f"🤝 Trusted: {user_label(target)} (by {user_label(message.from_user)})")
    else:
        temp_reply(message, "Reply to the user's message with <code>/trust</code>.")

@bot.message_handler(commands=['untrust'])
def handle_untrust(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    target = getattr(message.reply_to_message, 'from_user', None) if message.reply_to_message else None
    if target:
        set_msg_count(target.id, 0)
        temp_reply(message, f"↩️ {html.escape(target.first_name or '')} is no longer trusted.")
        log_event(f"↩️ Untrusted: {user_label(target)} (by {user_label(message.from_user)})")
    else:
        temp_reply(message, "Reply to the user's message with <code>/untrust</code>.")

@bot.message_handler(commands=['pardon'])
def handle_pardon(message):
    """Reply with /pardon to clear someone's spam strikes and toxic warnings."""
    if not is_admin(message.chat.id, message.from_user.id):
        return
    target = getattr(message.reply_to_message, 'from_user', None) if message.reply_to_message else None
    if target:
        reset_strikes(target.id)
        temp_reply(message, f"🕊 Strikes cleared for {html.escape(target.first_name or '')}.")
        log_event(f"🕊 Pardoned: {user_label(target)} (by {user_label(message.from_user)})")
    else:
        temp_reply(message, "Reply to the user's message with <code>/pardon</code>.")

@bot.channel_post_handler(commands=['getid'])
def handle_get_id(message):
    bot.reply_to(message, f"The ID of this channel is: <code>{message.chat.id}</code>", parse_mode="HTML")

# =========================================================
# New-joiner procedure
# =========================================================
_pending_captcha = {}
_pending_lock = threading.Lock()
_recent_joins = {}

def captcha_timeout(chat_id, user_id, first_name):
    with _pending_lock:
        entry = _pending_captcha.pop((chat_id, user_id), None)
    if not entry:
        return
    delete_silently(chat_id, entry["msg_id"])
    soft_kick(chat_id, user_id)
    log_event(f"⏱ Captcha timeout — removed {html.escape(first_name or '?')} "
              f"(id <code>{user_id}</code>). They can rejoin.")

def process_new_member(chat_id, user):
    if user.is_bot:
        return
    now = time.time()
    key = (chat_id, user.id)
    if now - _recent_joins.get(key, 0) < 60:
        return
    _recent_joins[key] = now

    if is_admin(chat_id, user.id):
        log_event(f"👤 Admin {user_label(user)} joined — captcha skipped.")
        return

    if check_cas_banned(user.id):
        hard_ban(chat_id, user.id)
        log_event(f"🚨 Pre-emptively banned {user_label(user)} on join (CAS blacklist).")
        return

    try:
        bot.restrict_chat_member(chat_id, user.id,
                                 permissions=ChatPermissions(can_send_messages=False))
    except Exception as e:
        log_event(f"⚠️ Could not restrict {user_label(user)}: {html.escape(str(e))} — captcha skipped.")
        return

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("I am human 🤖🚫", callback_data=f"captcha_{user.id}"))
    sent = bot.send_message(
        chat_id,
        f"Welcome {user.first_name}! Press the button within "
        f"{CAPTCHA_TIMEOUT // 60} minutes to unlock the chat.",
        reply_markup=markup
    )
    timer = threading.Timer(CAPTCHA_TIMEOUT, captcha_timeout,
                            args=(chat_id, user.id, user.first_name))
    timer.daemon = True
    timer.start()
    with _pending_lock:
        _pending_captcha[(chat_id, user.id)] = {"msg_id": sent.message_id, "timer": timer}
    log_event(f"👤 New joiner {user_label(user)} — captcha sent.")

@bot.message_handler(content_types=['new_chat_members'])
def handle_new_member(message):
    for new_user in message.new_chat_members:
        process_new_member(message.chat.id, new_user)

@bot.chat_member_handler()
def handle_chat_member_update(update):
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    if new == 'member' and old in ('left', 'kicked'):
        process_new_member(update.chat.id, update.new_chat_member.user)

@bot.message_handler(content_types=['left_chat_member'])
def handle_left_member(message):
    user = message.left_chat_member
    if not user:
        return
    with _pending_lock:
        entry = _pending_captcha.pop((message.chat.id, user.id), None)
    if entry:
        entry["timer"].cancel()
        delete_silently(message.chat.id, entry["msg_id"])

@bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_'))
def handle_captcha(call):
    target_user_id = int(call.data.split('_')[1])
    if call.from_user.id != target_user_id:
        bot.answer_callback_query(call.id, "This button is not for you!", show_alert=True)
        return
    chat_id = call.message.chat.id
    with _pending_lock:
        entry = _pending_captcha.pop((chat_id, target_user_id), None)
    if entry:
        entry["timer"].cancel()
    try:
        bot.restrict_chat_member(
            chat_id, target_user_id,
            until_date=int(time.time()) + MEDIA_LOCK_SECONDS,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            )
        )
    except Exception as e:
        print(f"Unmute error: {e}")
    mark_verified(target_user_id)
    bot.answer_callback_query(call.id, "Verified! You can text now. Media unlocks in 24 h.")
    delete_silently(chat_id, call.message.message_id)
    log_event(f"✅ Verified: {user_label(call.from_user)}")

# =========================================================
# Moderation pipeline (silent except toxicity warnings):
# CAS -> instant-ban checks -> forwards -> probation -> flood
# -> toxicity -> keyword scoring -> trust counter
# =========================================================
DUP_WINDOW = 900
DUP_MIN_LEN = 80
_dup_lock = threading.Lock()
_recent_texts = {}

def duplicate_count(norm_text):
    """How many times this long text has been seen within the window."""
    if len(norm_text) < DUP_MIN_LEN:
        return 0
    h = hash(norm_text)
    now = time.time()
    with _dup_lock:
        for k in [k for k, (ts, _) in _recent_texts.items() if now - ts > DUP_WINDOW]:
            del _recent_texts[k]
        entry = _recent_texts.get(h)
        if entry:
            entry[1] += 1
            return entry[1]
        _recent_texts[h] = [now, 1]
    return 1

def punish(message, reason, ban=False):
    chat_id = message.chat.id
    user = message.from_user
    text = message.text or message.caption or ""
    delete_silently(chat_id, message.message_id)
    if ban:
        hard_ban(chat_id, user.id)
        reset_strikes(user.id)
        log_event(
            f"🔨 <b>INSTANT BAN</b>\nUser: {user_label(user)}\nReason: {reason}\n"
            f"Message: {html.escape(text[:800])}"
        )
        return
    strikes = add_strike(user.id)
    log_event(
        f"🗑 <b>DELETED</b> — strike {strikes}/{STRIKE_LIMIT}\n"
        f"User: {user_label(user)}\nReason: {reason}\n"
        f"Message: {html.escape(text[:800])}"
    )
    if strikes >= STRIKE_LIMIT:
        hard_ban(chat_id, user.id)
        reset_strikes(user.id)
        log_event(f"🔨 <b>USER BANNED</b>: {user_label(user)} ({STRIKE_LIMIT} strikes)")

def handle_toxicity(message, toxic_words):
    chat_id = message.chat.id
    user = message.from_user
    delete_silently(chat_id, message.message_id)
    count = add_toxic(user.id)
    name = html.escape(user.first_name or "")
    if count >= TOXIC_LIMIT:
        mute_for(chat_id, user.id, TOXIC_MUTE_SECONDS)
        temp_notice(chat_id, f"🔇 {name} отримує мут на 7 днів за лайку та образи ({count}/{TOXIC_LIMIT}).")
        log_event(f"🔇 <b>7-DAY MUTE</b>: {user_label(user)} — toxic {count}/{TOXIC_LIMIT} "
                  f"(matched: {html.escape(', '.join(sorted(toxic_words)))})")
        with db() as conn:
            conn.execute("DELETE FROM toxic_strikes WHERE user_id = ?", (user.id,))
            conn.commit()
    else:
        temp_notice(chat_id, f"⚠️ {name}, будь ласка, без лайки та образ. "
                             f"Попередження {count}/{TOXIC_LIMIT} — далі мут на тиждень.")
        log_event(f"🤬 Toxic warning {count}/{TOXIC_LIMIT}: {user_label(user)} "
                  f"(matched: {html.escape(', '.join(sorted(toxic_words)))})\n"
                  f"Message: {html.escape((message.text or message.caption or '')[:400])}")

def moderate(message):
    chat_id = message.chat.id
    user = message.from_user
    if user is None or is_admin(chat_id, user.id):
        return

    text = message.text or message.caption or ""
    raw = text.lower()

    # 1. CAS trap for existing members
    if check_cas_banned(user.id):
        delete_silently(chat_id, message.message_id)
        hard_ban(chat_id, user.id)
        log_event(f"🚨 Banned existing member {user_label(user)} (CAS blacklist).")
        return

    # 2. Selling tobacco = instant ban
    if text and SELL_RX.search(raw) and TOBACCO_RX.search(raw):
        punish(message, "tobacco sale", ban=True)
        return

    hits = match_keywords(text) if text else {'ban': set(), 'strong': set(),
                                              'weak': set(), 'toxic': set()}

    # 3. Instant-ban tier keywords
    if hits['ban']:
        punish(message, "ban keyword: " + ", ".join(sorted(hits['ban'])), ban=True)
        return

    # 4. Forwards from non-whitelisted channels
    ch = forwarded_channel(message)
    if ch is not None:
        uname = (getattr(ch, 'username', '') or '').lower()
        if uname not in get_allowed_channels() and str(ch.id) not in get_allowed_channels():
            punish(message, f"forward from channel @{html.escape(uname) if uname else ch.id}")
            return

    if not text:
        return

    trusted = is_trusted(user.id)

    # 5. Probation: fresh members can't drop telegram links / @handles
    if not trusted and CONTACT_RX.search(raw) and in_probation(user.id):
        delete_silently(chat_id, message.message_id)
        log_event(f"🧪 <b>PROBATION</b> — deleted telegram link/@handle from newcomer (no strike)\n"
                  f"User: {user_label(user)}\nMessage: {html.escape(text[:400])}")
        return

    # 6. Duplicate flood — trusted members exempt; strike only from the 3rd copy
    if not trusted:
        dup = duplicate_count(normalize_text(text))
        if dup == 2:
            delete_silently(chat_id, message.message_id)
            log_event(f"♻️ Duplicate deleted (no strike) from {user_label(user)}\n"
                      f"Message: {html.escape(text[:400])}")
            return
        if dup >= 3:
            punish(message, f"duplicate flood (copy #{dup})")
            return

    # 7. Toxicity: visible warning, 3rd -> 7-day mute
    if hits['toxic']:
        handle_toxicity(message, hits['toxic'])
        return

    # 8. Tiered keyword scoring; 5+ = instant ban, 2+ = delete + strike
    score, reason = score_message(text, hits)
    if score >= BAN_SCORE_THRESHOLD:
        punish(message, f"score {score} ({reason})", ban=True)
        return
    if score >= SPAM_SCORE_THRESHOLD:
        punish(message, f"score {score} ({reason})")
        return

    # 9. Clean message — grow the user's trust counter
    bump_msg_count(user.id)

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'animation'])
def filter_spam(message):
    try:
        moderate(message)
    except Exception as e:
        print(f"Moderation error: {e}")

@bot.edited_message_handler(content_types=['text', 'photo', 'video', 'document', 'animation'])
def filter_edited_spam(message):
    try:
        moderate(message)
    except Exception as e:
        print(f"Moderation error (edit): {e}")

# =========================================================
# Startup
# =========================================================
if __name__ == '__main__':
    init_db()
    threading.Thread(target=run_web, daemon=True).start()
    print(f"V7 anti-spam bot starting — {len(get_spam_keywords())} keywords loaded, "
          f"tiers ban/strong/weak/toxic, trust system ON.")
    bot.infinity_polling(allowed_updates=[
        'message', 'edited_message', 'channel_post', 'callback_query', 'chat_member'
    ])
