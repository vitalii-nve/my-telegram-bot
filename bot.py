import telebot
import requests
import time
import sqlite3
import threading
import os
from flask import Flask
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- Configuration ---
API_TOKEN = '8615464194:AAGv1HnYIwoMJfsb7jPDGsEdj1zsK7A_udQ'  # <--- PASTE YOUR TOKEN HERE
bot = telebot.TeleBot(API_TOKEN)
SPAM_KEYWORDS = ["crypto", "free money", "invest", "bitcoin", "earn cash", "join channel"]
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
        cursor.execute('''CREATE TABLE IF NOT EXISTS user_strikes (user_id INTEGER PRIMARY KEY, strikes INTEGER DEFAULT 0)''')
        conn.commit()

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
    text = message.text.lower() if message.text else ""

    if is_admin(chat_id, user_id):
        return

    is_spam = any(k in text for k in SPAM_KEYWORDS) or "http" in text or "t.me/" in text

    if is_spam:
        try:
            bot.delete_message(chat_id, message.message_id)
            current_strikes = add_strike(user_id)
            
            if current_strikes >= STRIKE_LIMIT:
                bot.kick_chat_member(chat_id, user_id)
                bot.send_message(chat_id, f"🔨 User banned for reaching {STRIKE_LIMIT} spam strikes.")
                reset_strikes(user_id)
            else:
                warning = bot.send_message(chat_id, f"⚠️ Warning {current_strikes}/{STRIKE_LIMIT}: No links or spam allowed.")
                time.sleep(5)
                bot.delete_message(chat_id, warning.message_id)
        except Exception as e:
            print(f"Error handling spam: {e}")

# --- Startup Sequence ---
if __name__ == '__main__':
    init_db() # Create the database
    threading.Thread(target=run_web).start() # Start the dummy website in the background
    print("Anti-spam bot is starting up...")
    bot.infinity_polling() # Start the Telegram bot
