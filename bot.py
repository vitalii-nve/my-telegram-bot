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
STRIKE_TTL = 30 * 86400      # strikes older than 30 days no longer count
CAPTCHA_TIMEOUT = 300        # seconds a new joiner has to press the button
MEDIA_LOCK_SECONDS = 86400   # text-only period after verification (24 h)
PROBATION_SECONDS = 86400    # after verification: no t.me links / @handles for 24 h
SPAM_SCORE_THRESHOLD = 2     # strong keyword = 2 points, weak = 1 point each
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

# Visually identical/near-identical letters used to evade filters
# (seen in the wild in this group: "оtkrut набор в команду")
LAT_TO_CYR = str.maketrans('aceiopxyk03', 'асеіорхукоз')
CYR_TO_LAT = str.maketrans('асеіорхук0', 'aceiopxyko')

def normalize_text(text):
    """Lowercase, strip evasion characters, unify ё->е, collapse spaces."""
    t = text.lower().translate(ZERO_WIDTH)
    t = t.replace('ё', 'е')
    t = re.sub(r"[.\_\-\*\@\#\$\%\^\&\(\)\+\=\~\'\"\!\?\,\:\;]", '', t)
    return ' '.join(t.split())

def text_variants(text):
    """Base + all-Cyrillic + all-Latin homoglyph variants of the text."""
    base = normalize_text(text)
    return {base, base.translate(LAT_TO_CYR), base.translate(CYR_TO_LAT)}

# Patterns checked on RAW (lowercased) text, before punctuation stripping
CONTACT_RX = re.compile(r'@[a-zA-Z][a-zA-Z0-9_]{3,}|t\.me/|telegram\.me/', re.I)
MONEY_RX = re.compile(
    r'[\$€£₴]\s*\d|\d\s*[\$€£₴]|'
    r'\d\s*(?:usd|usdt|eur|dkk|kr|kroner|крон|грн|гривень|долл|доллар)\b', re.I)

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
        conn.commit()
        # Migrations for databases created by V5
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

def add_strike(user_id):
    now = time.time()
    with db() as conn:
        c = conn.cursor()
        c.execute("SELECT strikes, last_at FROM user_strikes WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        # Old strikes expire: a warning from months ago shouldn't cause a ban today
        strikes = row[0] + 1 if row and now - (row[1] or 0) <= STRIKE_TTL else 1
        c.execute("INSERT INTO user_strikes (user_id, strikes, last_at) VALUES (?, ?, ?) "
                  "ON CONFLICT(user_id) DO UPDATE SET strikes = ?, last_at = ?",
                  (user_id, strikes, now, strikes, now))
        conn.commit()
    return strikes

def reset_strikes(user_id):
    with db() as conn:
        conn.execute("DELETE FROM user_strikes WHERE user_id = ?", (user_id,))
        conn.commit()

def add_spam_keyword(word, tier='strong', rebuild=True):
    word = normalize_text(word)
    if not word:
        return
    with db() as conn:
        conn.execute("INSERT INTO spam_keywords (word, tier) VALUES (?, ?) "
                     "ON CONFLICT(word) DO UPDATE SET tier = excluded.tier", (word, tier))
        conn.commit()
    if rebuild:
        rebuild_patterns()

def remove_spam_keyword(word, rebuild=True):
    word = normalize_text(word)
    with db() as conn:
        conn.execute("DELETE FROM spam_keywords WHERE word = ?", (word,))
        conn.commit()
    if rebuild:
        rebuild_patterns()

def get_spam_keywords():
    """Returns list of (word, tier)."""
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
# Keyword matching with tiers + scoring
# =========================================================
_patterns_lock = threading.Lock()
_patterns = []  # list of (keyword, tier, compiled_regex)

def rebuild_patterns():
    global _patterns
    pats = []
    for kw, tier in get_spam_keywords():
        pats.append((kw, tier, re.compile(r'(?<!\w)' + re.escape(kw) + r'(?!\w)')))
    with _patterns_lock:
        _patterns = pats

def score_message(raw_text):
    """Returns (score, reason) — strong keyword = 2 pts, weak = 1 pt,
    money-amount + telegram-contact pattern = 1 pt."""
    variants = text_variants(raw_text)
    with _patterns_lock:
        pats = list(_patterns)

    strong, weak = set(), set()
    for kw, tier, rx in pats:
        if any(rx.search(v) for v in variants):
            (strong if tier == 'strong' else weak).add(kw)

    # Don't double-count keywords contained in a longer matched phrase
    # (e.g. "в лс" inside "пишите в лс")
    matched = strong | weak
    strong = {k for k in strong if not any(k != o and k in o for o in matched)}
    weak = {k for k in weak if not any(k != o and k in o for o in matched)}

    raw = raw_text.lower()
    money_contact = bool(MONEY_RX.search(raw) and CONTACT_RX.search(raw))

    score = 2 * len(strong) + len(weak) + (1 if money_contact else 0)
    parts = []
    if strong:
        parts.append("strong: " + ", ".join(sorted(strong)))
    if weak:
        parts.append("weak: " + ", ".join(sorted(weak)))
    if money_contact:
        parts.append("pattern: money+contact")
    return score, " | ".join(parts)

# =========================================================
# Caches
# =========================================================
_admin_cache = {}   # chat_id -> (fetched_at, set(user_ids))
_cas_cache = {}     # user_id -> (checked_at, banned_bool)

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

def temp_reply(message, text, delay=5):
    try:
        sent = bot.reply_to(message, text, parse_mode="HTML")
        threading.Timer(delay, delete_silently, args=(message.chat.id, sent.message_id)).start()
        threading.Timer(delay, delete_silently, args=(message.chat.id, message.message_id)).start()
    except Exception:
        pass

def command_arg(message):
    parts = (message.text or '').split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ''

def forwarded_channel(message):
    """Returns the source Chat if the message is forwarded from a channel."""
    ch = getattr(message, 'forward_from_chat', None)
    if ch is not None and getattr(ch, 'type', '') == 'channel':
        return ch
    origin = getattr(message, 'forward_origin', None)
    if origin is not None and getattr(origin, 'type', '') == 'channel':
        return getattr(origin, 'chat', None)
    return None

# =========================================================
# Default keyword library (EN / UK / RU)
# strong  -> one hit is enough to delete
# weak    -> needs 2 combined hits (or 1 hit + money/contact pattern)
# =========================================================
DEFAULT_KEYWORDS = {
    'strong': [
        # English
        "casino", "jackpot", "free spins", "1win",
        "onlyfans", "escort", "sugar daddy", "sugar mommy",
        "adult content", "hot photos", "private photos",
        "airdrop", "trading signals", "crypto signals", "pump and dump",
        "binary options", "guaranteed profit", "guaranteed income",
        "double your money", "cloud mining", "mining pool",
        "usdt", "ustd", "trc20", "erc20",
        "free money", "easy money", "quick cash", "fast cash",
        "get rich", "financial freedom", "passive income",
        "telegram premium free", "free premium",
        # Ukrainian
        "казино", "ставки на спорт", "букмекер", "бонус за реєстрацію",
        "ескорт", "інтим", "вебкам",
        "схема заробітку", "робоча схема", "безкоштовні гроші",
        "легкі гроші", "швидкі гроші", "заробіток в інтернеті",
        "пасивний дохід", "надішлю інформацію",
        "сигарети", "цигарки", "набір у команду",
        # Russian
        "бонус за регистрацию", "эскорт", "интим",
        "схема заработка", "рабочая схема", "бесплатные деньги", "халява",
        "легкие деньги", "быстрые деньги", "заработок в интернете",
        "пассивный доход", "пришлю информацию", "пришлю вам интересующую",
        "сигареты", "набор в команду",
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
        "трейдинг", "сигнали", "заробіток", "заробити", "дохід",
        "зарплата від", "гарна зарплата", "без вкладень", "вкладення від",
        "щоденні виплати", "виплати щодня", "потрібні люди",
        "набираємо людей", "шукаємо людей", "потрібен персонал",
        "віддалена робота", "робота вдома",
        "пиши в пп", "в пп", "пиши в особисті", "пишіть в особисті",
        "в приватні повідомлення", "деталі в особистих",
        "переходь за посиланням", "тисни на посилання",
        "телеграм канал", "приєднуйся", "бонус", "промокод",
        "розіграш", "ставки",
        # Russian
        "биткоин", "инвестиции", "инвестировать", "трейдинг", "трейдер",
        "сигналы", "заработок", "заработать", "доход",
        "зарплата от", "хорошая зарплата", "без вложений", "вложения от",
        "ежедневные выплаты", "выплаты каждый день", "первые выплаты",
        "нужны люди", "набираем людей", "ищем людей",
        "требуются сотрудники", "нужен персонал",
        "удаленная работа", "работа на дому",
        "пиши в лс", "пишите в лс", "в лс", "пиши в личку", "пишите в личку",
        "в личные сообщения", "подробности в лс",
        "переходи по ссылке", "жми на ссылку", "приватный канал",
        "розыгрыш",
    ],
}

# Channels whose forwarded posts are allowed (community announcements)
DEFAULT_ALLOWED_CHANNELS = ["ua_diaspora_dk"]

# =========================================================
# Admin commands (confirmations self-destruct after 5 s)
# =========================================================
@bot.message_handler(commands=['addspam'])
def handle_add_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keyword = command_arg(message)
    if keyword:
        add_spam_keyword(keyword, tier='strong')
        temp_reply(message, f"✅ Added <b>{html.escape(keyword)}</b> as a STRONG keyword (1 hit = delete).")
        log_event(f"➕ Strong keyword added by {user_label(message.from_user)}: <code>{html.escape(keyword)}</code>")
    else:
        temp_reply(message, "Usage: <code>/addspam word or phrase</code>")

@bot.message_handler(commands=['addweak'])
def handle_add_weak(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keyword = command_arg(message)
    if keyword:
        add_spam_keyword(keyword, tier='weak')
        temp_reply(message, f"✅ Added <b>{html.escape(keyword)}</b> as a WEAK keyword (needs 2 combined hits).")
        log_event(f"➕ Weak keyword added by {user_label(message.from_user)}: <code>{html.escape(keyword)}</code>")
    else:
        temp_reply(message, "Usage: <code>/addweak word or phrase</code>")

@bot.message_handler(commands=['delspam'])
def handle_del_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keyword = command_arg(message)
    if keyword:
        remove_spam_keyword(keyword)
        temp_reply(message, f"❌ Removed <b>{html.escape(keyword)}</b> from the blacklist.")
        log_event(f"➖ Keyword removed by {user_label(message.from_user)}: <code>{html.escape(keyword)}</code>")
    else:
        temp_reply(message, "Usage: <code>/delspam word or phrase</code>")

@bot.message_handler(commands=['listspam'])
def handle_list_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keywords = get_spam_keywords()
    temp_reply(message, f"📋 {len(keywords)} keywords — full list sent to the log channel.")
    lines = []
    current_tier = None
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
        temp_reply(message, f"✅ Forwards from <b>@{html.escape(name.lstrip('@'))}</b> are now allowed.")
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
        temp_reply(message, f"❌ Forwards from <b>@{html.escape(name.lstrip('@'))}</b> are no longer allowed.")
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

@bot.channel_post_handler(commands=['getid'])
def handle_get_id(message):
    bot.reply_to(message, f"The ID of this channel is: <code>{message.chat.id}</code>", parse_mode="HTML")

# =========================================================
# New-joiner procedure
# =========================================================
_pending_captcha = {}   # (chat_id, user_id) -> {"msg_id": int, "timer": Timer}
_pending_lock = threading.Lock()
_recent_joins = {}      # (chat_id, user_id) -> timestamp, dedupes double join events

def captcha_timeout(chat_id, user_id, first_name):
    with _pending_lock:
        entry = _pending_captcha.pop((chat_id, user_id), None)
    if not entry:
        return
    delete_silently(chat_id, entry["msg_id"])
    soft_kick(chat_id, user_id)
    log_event(f"⏱ Captcha timeout — removed {html.escape(first_name or '?')} (id <code>{user_id}</code>). They can rejoin.")

def process_new_member(chat_id, user):
    if user.is_bot:
        return
    now = time.time()
    key = (chat_id, user.id)
    if now - _recent_joins.get(key, 0) < 60:
        return
    _recent_joins[key] = now

    if is_admin(chat_id, user.id):
        log_event(f"👤 Admin {user_label(user)} joined — captcha skipped (admins can't be restricted).")
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
    """Catches joins that produce NO service message."""
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
# Moderation — fully silent in the group.
# Order: CAS -> channel forwards -> newcomer probation -> keyword scoring
# =========================================================
def punish(message, reason):
    chat_id = message.chat.id
    user = message.from_user
    text = message.text or message.caption or ""
    delete_silently(chat_id, message.message_id)
    strikes = add_strike(user.id)
    log_event(
        f"🗑 <b>DELETED</b> — strike {strikes}/{STRIKE_LIMIT}\n"
        f"User: {user_label(user)}\n"
        f"Reason: {reason}\n"
        f"Message: {html.escape(text[:800])}"
    )
    if strikes >= STRIKE_LIMIT:
        hard_ban(chat_id, user.id)
        reset_strikes(user.id)
        log_event(f"🔨 <b>USER BANNED</b>: {user_label(user)} ({STRIKE_LIMIT} strikes)")

def moderate(message):
    chat_id = message.chat.id
    user = message.from_user
    if user is None or is_admin(chat_id, user.id):
        return

    text = message.text or message.caption or ""

    # 1. CAS trap for existing members
    if check_cas_banned(user.id):
        delete_silently(chat_id, message.message_id)
        hard_ban(chat_id, user.id)
        log_event(f"🚨 Banned existing member {user_label(user)} (CAS blacklist).")
        return

    # 2. Messages forwarded from channels = ads, unless whitelisted
    ch = forwarded_channel(message)
    if ch is not None:
        uname = (getattr(ch, 'username', '') or '').lower()
        if uname not in get_allowed_channels() and str(ch.id) not in get_allowed_channels():
            punish(message, f"forward from channel @{html.escape(uname) if uname else ch.id}")
            return

    if not text:
        return

    # 3. Probation: fresh members can't drop telegram links / @handles
    if CONTACT_RX.search(text.lower()) and in_probation(user.id):
        delete_silently(chat_id, message.message_id)
        log_event(
            f"🧪 <b>PROBATION</b> — deleted telegram link/@handle from newcomer (no strike)\n"
            f"User: {user_label(user)}\n"
            f"Message: {html.escape(text[:400])}"
        )
        return

    # 4. Tiered keyword scoring with homoglyph variants
    score, reason = score_message(text)
    if score >= SPAM_SCORE_THRESHOLD:
        punish(message, f"score {score} ({reason})")

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
    print(f"V6 anti-spam bot starting — {len(get_spam_keywords())} keywords loaded, "
          f"tiered scoring ON, silent mode ON.")
    bot.infinity_polling(allowed_updates=[
        'message', 'edited_message', 'channel_post', 'callback_query', 'chat_member'
    ])
