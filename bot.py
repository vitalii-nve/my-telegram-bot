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
API_TOKEN = os.environ["API_TOKEN"]                          # REQUIRED. Never hardcode it again.
LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", "0"))  # your private log channel id
DB_NAME = os.environ.get("DB_PATH", "spam_bot.db")           # point at a persistent disk on Render!

STRIKE_LIMIT = 3
CAPTCHA_TIMEOUT = 60        # seconds a new joiner has to press the button (5 min)
MEDIA_LOCK_SECONDS = 86400   # text-only period after verification (24 h)
ADMIN_CACHE_TTL = 300        # re-fetch admin list every 5 min
CAS_CACHE_TTL = 3600         # re-check CAS per user at most once per hour

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
# Text normalization (shared by keywords and messages)
# =========================================================
ZERO_WIDTH = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff'), None)

def normalize_text(text):
    """Lowercase, strip evasion characters, unify ё->е, collapse spaces."""
    t = text.lower().translate(ZERO_WIDTH)
    t = t.replace('ё', 'е')
    t = re.sub(r"[.\_\-\*\@\#\$\%\^\&\(\)\+\=\~\'\"\!\?\,\:\;]", '', t)
    return ' '.join(t.split())

# =========================================================
# Database
# =========================================================
def db():
    return sqlite3.connect(DB_NAME)

def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute('CREATE TABLE IF NOT EXISTS user_strikes (user_id INTEGER PRIMARY KEY, strikes INTEGER DEFAULT 0)')
        c.execute('CREATE TABLE IF NOT EXISTS spam_keywords (word TEXT PRIMARY KEY)')
        conn.commit()
    for word in DEFAULT_KEYWORDS:
        add_spam_keyword(word, rebuild=False)
    rebuild_patterns()

def add_strike(user_id):
    with db() as conn:
        c = conn.cursor()
        c.execute("INSERT INTO user_strikes (user_id, strikes) VALUES (?, 1) "
                  "ON CONFLICT(user_id) DO UPDATE SET strikes = strikes + 1", (user_id,))
        conn.commit()
        c.execute("SELECT strikes FROM user_strikes WHERE user_id = ?", (user_id,))
        return c.fetchone()[0]

def reset_strikes(user_id):
    with db() as conn:
        conn.execute("DELETE FROM user_strikes WHERE user_id = ?", (user_id,))
        conn.commit()

def add_spam_keyword(word, rebuild=True):
    word = normalize_text(word)
    if not word:
        return
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO spam_keywords (word) VALUES (?)", (word,))
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
    with db() as conn:
        return [row[0] for row in conn.execute("SELECT word FROM spam_keywords ORDER BY word")]

# =========================================================
# Keyword matching (compiled once, word-boundary safe)
# =========================================================
_patterns_lock = threading.Lock()
_patterns = []  # list of (keyword, compiled_regex)

def rebuild_patterns():
    global _patterns
    pats = []
    for kw in get_spam_keywords():
        # (?<!\w) / (?!\w) = word boundaries that work for Cyrillic too.
        # "invest" no longer matches "investigation".
        pats.append((kw, re.compile(r'(?<!\w)' + re.escape(kw) + r'(?!\w)')))
    with _patterns_lock:
        _patterns = pats

def find_spam_keyword(clean_text):
    with _patterns_lock:
        pats = list(_patterns)
    for kw, rx in pats:
        if rx.search(clean_text):
            return kw
    return None

# =========================================================
# Caches (avoid hammering Telegram / CAS on every message)
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
    """All reporting goes to the private log channel — never to the group."""
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
    """Remove from the group but allow rejoining (used for captcha timeouts)."""
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
    """Reply to an admin command, then wipe both the reply and the command."""
    try:
        sent = bot.reply_to(message, text, parse_mode="HTML")
        threading.Timer(delay, delete_silently, args=(message.chat.id, sent.message_id)).start()
        threading.Timer(delay, delete_silently, args=(message.chat.id, message.message_id)).start()
    except Exception:
        pass

# =========================================================
# Default keyword library (EN / UK / RU)
# =========================================================
DEFAULT_KEYWORDS = [
    # ---------- English ----------
    "crypto", "criptocurrency", "cryptocurrency", "bitcoin", "usdt", "ustd", "trc20", "erc20",
    "free money", "easy money", "quick cash", "fast cash", "earn cash", "earn daily",
    "passive income", "financial freedom", "get rich", "double your money",
    "guaranteed profit", "guaranteed income", "daily profit", "weekly payout", "instant payout",
    "invest", "investment opportunity", "forex", "binary options",
    "trading signals", "crypto signals", "pump and dump", "presale",
    "cloud mining", "mining pool", "airdrop", "giveaway",
    "binance", "casino", "jackpot", "free spins", "promo code", "sign up bonus", "welcome bonus",
    "work from home", "no experience needed", "limited spots",
    "dm me", "join channel", "click the link", "link in bio", "check my bio", "referral link",
    "free premium", "telegram premium free",
    "onlyfans", "escort", "sugar daddy", "sugar mommy", "adult content", "hot photos", "private photos",

    # ---------- Ukrainian ----------
    "крипта", "криптовалюта", "біткоїн", "інвестиції", "інвестувати", "трейдинг",
    "заробіток", "заробити", "заробіток в інтернеті", "схема заробітку", "робоча схема",
    "легкі гроші", "швидкі гроші", "безкоштовні гроші", "безкоштовно",
    "пасивний дохід", "стабільний дохід", "додатковий дохід",
    "щоденні виплати", "виплати щодня", "виплата", "без вкладень", "вкладення від",
    "віддалена робота", "робота вдома", "потрібні люди", "набираємо людей",
    "шукаємо людей", "потрібен персонал", "зарплата від", "гарна зарплата",
    "пиши в пп", "пиши в особисті", "пишіть в особисті", "в приватні повідомлення",
    "деталі в особистих", "переходь за посиланням", "тисни на посилання",
    "казино", "ставки", "букмекер", "промокод", "бонус", "бонус за реєстрацію",
    "розіграш", "приєднуйся", "телеграм канал",
    "ескорт", "інтим", "вебкам",

    # ---------- Russian ----------
    "криптовалюта", "биткоин", "инвестиции", "инвестировать", "трейдинг", "трейдер", "сигналы",
    "заработок", "заработать", "схема заработка", "рабочая схема",
    "легкие деньги", "быстрые деньги", "бесплатные деньги", "халява",
    "пассивный доход", "стабильный доход", "дополнительный доход",
    "ежедневные выплаты", "выплаты каждый день", "первые выплаты",
    "без вложений", "вложения от",
    "удаленная работа", "работа на дому", "нужны люди", "набираем людей",
    "ищем людей", "требуются сотрудники", "нужен персонал",
    "зарплата от", "хорошая зарплата",
    "пиши в лс", "пишите в лс", "пиши в личку", "пишите в личку",
    "в личные сообщения", "подробности в лс",
    "переходи по ссылке", "жми на ссылку", "приватный канал",
    "ставки на спорт", "букмекер", "промокод", "бонус за регистрацию", "розыгрыш",
    "эскорт", "интим", "вебкам",
]

# =========================================================
# Admin commands (confirmations self-destruct after 5 s)
# =========================================================
@bot.message_handler(commands=['addspam'])
def handle_add_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keyword = message.text.replace('/addspam', '', 1).strip()
    if keyword:
        add_spam_keyword(keyword)
        temp_reply(message, f"✅ Added <b>{html.escape(keyword)}</b> to the blacklist.")
        log_event(f"➕ Keyword added by {user_label(message.from_user)}: <code>{html.escape(keyword)}</code>")
    else:
        temp_reply(message, "Usage: <code>/addspam word or phrase</code>")

@bot.message_handler(commands=['delspam'])
def handle_del_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    keyword = message.text.replace('/delspam', '', 1).strip()
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
    # Send in chunks so long lists don't hit the 4096-char message limit
    chunk, chunks = [], []
    for w in keywords:
        chunk.append(f"• <code>{html.escape(w)}</code>")
        if len(chunk) == 80:
            chunks.append("\n".join(chunk)); chunk = []
    if chunk:
        chunks.append("\n".join(chunk))
    for i, part in enumerate(chunks, 1):
        log_event(f"📋 <b>Spam keywords ({len(keywords)}), part {i}/{len(chunks)}:</b>\n{part}")

@bot.channel_post_handler(commands=['getid'])
def handle_get_id(message):
    bot.reply_to(message, f"The ID of this channel is: <code>{message.chat.id}</code>", parse_mode="HTML")

# =========================================================
# New-joiner procedure
#   1. CAS blacklist check -> instant silent ban
#   2. Full mute + captcha button
#   3. No button press within CAPTCHA_TIMEOUT -> soft-kick (can rejoin)
#   4. Verified -> text only for 24 h, then full group permissions
# =========================================================
_pending_captcha = {}   # (chat_id, user_id) -> {"msg_id": int, "timer": Timer}
_pending_lock = threading.Lock()

def captcha_timeout(chat_id, user_id, first_name):
    with _pending_lock:
        entry = _pending_captcha.pop((chat_id, user_id), None)
    if not entry:
        return  # already verified or left
    delete_silently(chat_id, entry["msg_id"])
    soft_kick(chat_id, user_id)
    log_event(f"⏱ Captcha timeout — removed {html.escape(first_name or '?')} (id <code>{user_id}</code>). They can rejoin.")

@bot.message_handler(content_types=['new_chat_members'])
def handle_new_member(message):
    chat_id = message.chat.id
    for new_user in message.new_chat_members:
        if new_user.is_bot:
            continue

        # Step 1: global CAS blacklist
        if check_cas_banned(new_user.id):
            hard_ban(chat_id, new_user.id)
            log_event(f"🚨 Pre-emptively banned {user_label(new_user)} on join (CAS blacklist).")
            continue

        # Step 2: mute until verified
        try:
            bot.restrict_chat_member(chat_id, new_user.id,
                                     permissions=ChatPermissions(can_send_messages=False))
        except Exception as e:
            print(f"Restrict error: {e}")
            continue

        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("I am human 🤖🚫", callback_data=f"captcha_{new_user.id}"))
        sent = bot.send_message(
            chat_id,
            f"Welcome {new_user.first_name}! Press the button within "
            f"{CAPTCHA_TIMEOUT // 60} minutes to unlock the chat.",
            reply_markup=markup
        )

        # Step 3: arm the timeout
        timer = threading.Timer(CAPTCHA_TIMEOUT, captcha_timeout,
                                args=(chat_id, new_user.id, new_user.first_name))
        timer.daemon = True
        timer.start()
        with _pending_lock:
            _pending_captcha[(chat_id, new_user.id)] = {"msg_id": sent.message_id, "timer": timer}

        log_event(f"👤 New joiner {user_label(new_user)} — captcha sent.")

@bot.message_handler(content_types=['left_chat_member'])
def handle_left_member(message):
    """If a pending joiner leaves on their own, clean up the captcha."""
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

    # Step 4: text only for 24 h, then group defaults apply
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

    bot.answer_callback_query(call.id, "Verified! You can text now. Media unlocks in 24 h.")
    delete_silently(chat_id, call.message.message_id)
    log_event(f"✅ Verified: {user_label(call.from_user)}")

# =========================================================
# Spam filter — fully silent in the group.
# Checks text AND media captions, including edited messages.
# =========================================================
def moderate(message):
    chat_id = message.chat.id
    user = message.from_user
    if user is None or is_admin(chat_id, user.id):
        return

    text = message.text or message.caption or ""

    # CAS trap: existing members who land on the blacklist later
    if check_cas_banned(user.id):
        delete_silently(chat_id, message.message_id)
        hard_ban(chat_id, user.id)
        log_event(f"🚨 Banned existing member {user_label(user)} (CAS blacklist).")
        return

    if not text:
        return

    matched = find_spam_keyword(normalize_text(text))
    if not matched:
        return

    delete_silently(chat_id, message.message_id)
    strikes = add_strike(user.id)

    log_event(
        f"🗑 <b>SPAM DELETED</b> — strike {strikes}/{STRIKE_LIMIT}\n"
        f"User: {user_label(user)}\n"
        f"Matched: <code>{html.escape(matched)}</code>\n"
        f"Message: {html.escape(text[:800])}"
    )

    if strikes >= STRIKE_LIMIT:
        hard_ban(chat_id, user.id)
        reset_strikes(user.id)
        log_event(f"🔨 <b>USER BANNED</b>: {user_label(user)} ({STRIKE_LIMIT} strikes)")

@bot.message_handler(content_types=['text', 'photo', 'video', 'document', 'animation'])
def filter_spam(message):
    try:
        moderate(message)
    except Exception as e:
        print(f"Moderation error: {e}")

@bot.edited_message_handler(content_types=['text', 'photo', 'video', 'document', 'animation'])
def filter_edited_spam(message):
    """Spammers often post something innocent, then edit spam in."""
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
    print(f"V5 anti-spam bot starting — {len(get_spam_keywords())} keywords loaded, silent mode ON.")
    bot.infinity_polling()
