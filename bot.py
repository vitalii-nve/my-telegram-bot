import telebot
import requests
import time
import sqlite3
import threading
import os
import re
from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Configuration ---
API_TOKEN = '8615464194:AAGv1HnYIwoMJfsb7jPDGsEdj1zsK7A_udQ'  # <--- PASTE YOUR REAL TOKEN HERE
LOG_CHANNEL_ID = -1004359034302  # <--- PASTE YOUR NEW CHANNEL ID HERE
bot = telebot.TeleBot(API_TOKEN)
STRIKE_LIMIT = 3
DB_NAME = "spam_bot.db"

# --- Web Server (Keeps the bot alive on Render) ---
app = Flask(__name__)

@app.route('/')
def keep_alive():
    return "Bot is alive and actively guarding the group!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- Database Management ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        # Strike tracking table
        cursor.execute('''CREATE TABLE IF NOT EXISTS user_strikes (user_id INTEGER PRIMARY KEY, strikes INTEGER DEFAULT 0)''')
        # Dynamic keywords table
        cursor.execute('''CREATE TABLE IF NOT EXISTS spam_keywords (word TEXT PRIMARY KEY)''')
        conn.commit()
        
    # Seed default words (English & Ukrainian)
    default_words = [
        # English Spam Words
        "crypto", "free money", "invest", "bitcoin", "earn cash", "join channel",
        "giveaway", "airdrop", "forex", "binance", "casino", "jackpot", "bonus", "dm me", "usdt", "ustd",
        
        # Ukrainian Spam Words
        "крипта", "безкоштовні гроші", "інвестиції", "біткоїн", "заробіток", 
        "приєднуйся", "розіграш", "казино", "бонус", "безкоштовно", "ставки", 
        "заробити", "виплата", "телеграм канал", "криптовалюта", "пиши в пп", "ужен персонал", "Хорошая зарплата",
    ]
    for word in default_words:
        add_spam_keyword(word)

def add_strike(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT strikes FROM user_strikes WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        new_strikes = (row[0] + 1) if row else 1
        if row:
            cursor.execute("UPDATE user_strikes SET strikes = ? WHERE user_id = ?", (new_strikes, user_id))
        else:
            cursor.execute("INSERT INTO user_strikes (user_id, strikes) VALUES (?, ?)", (user_id, new_strikes))
        conn.commit()
    return new_strikes

def reset_strikes(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM user_strikes WHERE user_id = ?", (user_id,))
        conn.commit()

def add_spam_keyword(word):
    word = word.lower().strip()
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO spam_keywords (word) VALUES (?)", (word,))
        conn.commit()

def remove_spam_keyword(word):
    word = word.lower().strip()
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM spam_keywords WHERE word = ?", (word,))
        conn.commit()

def get_spam_keywords():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT word FROM spam_keywords")
        return [row[0] for row in cursor.fetchall()]

# --- Helper Functions ---
def is_admin(chat_id, user_id):
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ['creator', 'administrator']
    except Exception:
        return False

def check_cas_banned(user_id):
    try:
        response = requests.get(f"https://api.cas.chat/check?user_id={user_id}", timeout=5).json()
        return response.get("ok", False) 
    except Exception:
        return False

def normalize_text(text):
    """Removes special characters and extra spaces to prevent word-evasion."""
    cleaned = re.sub(r'[\.\_\-\*\@\#\$\%\^\&\(\)]', '', text.lower())
    cleaned = ' '.join(cleaned.split())
    return cleaned

# --- Admin Commands to Manage Keywords ---
@bot.message_handler(commands=['addspam'])
def handle_add_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
    
    keyword = message.text.replace('/addspam', '').strip()
    if keyword:
        add_spam_keyword(keyword)
        bot.reply_to(message, f"✅ Added **'{keyword}'** to the spam blacklist.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "Usage: `/addspam <word or phrase>`", parse_mode="Markdown")

@bot.message_handler(commands=['delspam'])
def handle_del_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
        
    keyword = message.text.replace('/delspam', '').strip()
    if keyword:
        remove_spam_keyword(keyword)
        bot.reply_to(message, f"❌ Removed **'{keyword}'** from the spam blacklist.", parse_mode="Markdown")
    else:
        bot.reply_to(message, "Usage: `/delspam <word or phrase>`", parse_mode="Markdown")

@bot.message_handler(commands=['listspam'])
def handle_list_spam(message):
    if not is_admin(message.chat.id, message.from_user.id):
        return
        
    keywords = get_spam_keywords()
    if keywords:
        word_list = "\n".join([f"• `{w}`" for w in keywords])
        bot.reply_to(message, f"📋 **Current Spam Keywords ({len(keywords)}):**\n\n{word_list}", parse_mode="Markdown")
    else:
        bot.reply_to(message, "No spam keywords set.")

@bot.channel_post_handler(commands=['getid'])
def handle_get_id(message):
    bot.reply_to(message, f"The ID of this channel is: `{message.chat.id}`", parse_mode="Markdown")
    
# --- Core Bot Features ---
@bot.message_handler(content_types=['new_chat_members'])
def handle_new_member(message):
    for new_user in message.new_chat_members:
        if check_cas_banned(new_user.id):
            bot.kick_chat_member(message.chat.id, new_user.id)
            bot.send_message(message.chat.id, f"🚨 Pre-emptively banned {new_user.first_name} (Global CAS Blacklist).")
            continue
            
        bot.restrict_chat_member(message.chat.id, new_user.id, can_send_messages=False)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("I am human 🤖🚫", callback_data=f"captcha_{new_user.id}"))
        bot.send_message(message.chat.id, f"Welcome {new_user.first_name}! Please prove you are human to unlock chat.", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('captcha_'))
def handle_captcha(call):
    target_user_id = int(call.data.split('_')[1])
    if call.from_user.id == target_user_id:
        restrict_until = int(time.time()) + 86400 
        bot.restrict_chat_member(
            call.message.chat.id, target_user_id, until_date=restrict_until,
            can_send_messages=True, can_send_media_messages=False,
            can_send_other_messages=False, can_add_web_page_previews=False
        )
        bot.answer_callback_query(call.id, "Verified! You can text now. Media is unlocked in 24h.")
        bot.delete_message(call.message.chat.id, call.message.message_id)
    else:
        bot.answer_callback_query(call.id, "This button is not for you!", show_alert=True)

@bot.message_handler(func=lambda message: True)
def filter_spam_and_strike(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text if message.text else ""

    if is_admin(chat_id, user_id):
        return

    # --- NEW TRAP: Check CAS blacklist every time someone speaks ---
    if check_cas_banned(user_id):
        try:
            bot.delete_message(chat_id, message.message_id)
            bot.kick_chat_member(chat_id, user_id)
            bot.send_message(chat_id, f"🚨 Banned an existing member ({message.from_user.first_name}) who is on the global CAS blacklist.")
            return # Stop processing and exit
        except Exception:
            pass
            
    # Normalize incoming text to catch evasive spelling
    clean_text = normalize_text(text)
    
    # Check against the dynamic database keywords
    current_keywords = get_spam_keywords()
    is_spam = any(keyword in clean_text for keyword in current_keywords)

    if is_spam:
        try:
            bot.delete_message(chat_id, message.message_id)
            
            # --- NEW: Send a report to your private log channel ---
            bot.send_message(
                LOG_CHANNEL_ID, 
                f"🗑 **SPAM DELETED**\n"
                f"User: {message.from_user.first_name} (ID: `{user_id}`)\n"
                f"Message: {text}",
                parse_mode="Markdown"
            )
            
            current_strikes = add_strike(user_id)
            
            if current_strikes >= STRIKE_LIMIT:
                bot.kick_chat_member(chat_id, user_id)
                bot.send_message(chat_id, f"🔨 User banned for reaching {STRIKE_LIMIT} spam strikes.")
                
                # --- NEW: Log the ban ---
                bot.send_message(LOG_CHANNEL_ID, f"🔨 **USER BANNED**: {message.from_user.first_name} (3 Strikes)")
                
                reset_strikes(user_id)
            else:
                warning = bot.send_message(chat_id, f"⚠️ Warning {current_strikes}/{STRIKE_LIMIT}: No links or spam allowed.")
                time.sleep(5)
                bot.delete_message(chat_id, warning.message_id)
        except Exception as e:
            print(f"Error handling spam: {e}")

# --- Startup Sequence ---
if __name__ == '__main__':
    init_db() # Create tables and populate default keywords
    threading.Thread(target=run_web).start() # Keep Render alive
    print("V4 Anti-spam bot with dynamic keywords is starting up...")
    bot.infinity_polling()
