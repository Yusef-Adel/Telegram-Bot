import requests
import asyncio
import os
import sqlite3
from telethon import Button, TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    RPCError,
    UsernameNotOccupiedError,
    PeerIdInvalidError
)
from dotenv import load_dotenv
import re
import html  # For escaping HTML characters
import logging
from logging.handlers import RotatingFileHandler

# -----------------------------
# 1. Configure Logging with Log Rotation
# -----------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # Change to DEBUG for more detailed logs

os.makedirs("logs", exist_ok=True)
handler = RotatingFileHandler(
    os.path.join("logs", "telegram_monitor_text_only.log"),
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=5,
    encoding='utf-8'
)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# -----------------------------
# 2. Load Environment Variables
# -----------------------------
load_dotenv()

API_ID = os.getenv('TELEGRAM_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH')
BOT_TOKEN = os.getenv('BOT_TOKEN')
TARGET_CHANNELS = os.getenv('TARGET_CHANNELS')  # Comma-separated list of channels

if not all([API_ID, API_HASH, BOT_TOKEN, TARGET_CHANNELS]):
    logger.error("❌ One or more required environment variables are missing. Check your .env file.")
    exit(1)

TARGET_CHANNELS = [ch.strip() for ch in TARGET_CHANNELS.split(',') if ch.strip()]

# -----------------------------
# 3. SQLite Database Setup
# -----------------------------
DB_FILE = "subscribers.db"

def init_db():
    """
    Creates the necessary table if it doesn't exist 
    to store subscribed user IDs.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscribed_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def add_subscribed_user(user_id):
    """
    Inserts the user ID into the subscribed_users table (if not already present).
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO subscribed_users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def remove_subscribed_user(user_id):
    """
    Removes the user ID from the subscribed_users table.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscribed_users WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def is_user_subscribed(user_id):
    """
    Checks if a user is already subscribed.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM subscribed_users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row is not None

def get_all_subscribed_users():
    """
    Retrieves all subscribed user IDs from the database.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM subscribed_users")
    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]

# Initialize the SQLite database/table
init_db()

# -----------------------------
# 4. Define "Call" Message Filtering Criteria
# -----------------------------
# Updated Regex: Matches "XAUUSD buy", "XAUUSD sell", or any message with "buy" or "sell" (case-insensitive)
CALL_PATTERN = re.compile(r'\b(XAUUSD.*(?:buy|sell)|(?:buy|sell))\b', re.IGNORECASE)

# -----------------------------
# 5. Initialize Telegram Clients
# -----------------------------
os.makedirs("sessions", exist_ok=True)
USER_SESSION = os.path.join("sessions", "telegram_monitor_text_only_user_session")
BOT_SESSION = os.path.join("sessions", "telegram_monitor_text_only_bot_session")

user_client = TelegramClient(USER_SESSION, int(API_ID), API_HASH)
bot_client = TelegramClient(BOT_SESSION, int(API_ID), API_HASH)

# -----------------------------
# 6. Helper Functions
# -----------------------------
def get_channel_display_name(event):
    """
    Returns a formatted display name for the channel from which the message originated.
    """
    try:
        chat = event.chat
        if hasattr(chat, 'title') and chat.title:
            return f"**{chat.title}**"
        elif hasattr(chat, 'username') and chat.username:
            return f"@{chat.username}"
        else:
            return f"Channel ID {chat.id}"
    except Exception as e:
        logger.error(f"❌ Error retrieving channel display name: {e}")
        return "Unknown Channel"

def escape_html(text):
    """
    Escapes HTML special characters in the text.
    """
    return html.escape(text)

def fetch_xauusd_price():
    """
    Fetches the live price of XAUUSD (gold vs USD) using the Gold API.
    """
    api_key = os.getenv('GOLD_API_KEY')
    url = "https://www.goldapi.io/api/XAU/USD"
    headers = {
        "x-access-token": api_key,
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()

        data = response.json()
        price = data.get("price")
        if price is not None:
            logger.info(f"💰 Current XAUUSD Price: {price}")
            return price
        else:
            logger.error("❌ Failed to retrieve price from the Gold API response.")
            return None
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Error fetching XAUUSD price: {e}")
        return None

# -----------------------------
# 7. Main Async Function
# -----------------------------
async def main():
    # Start the user client
    try:
        await user_client.start()
    except SessionPasswordNeededError:
        logger.error("❌ Two-Step Verification is enabled for the user account.")
        return
    except RPCError as e:
        logger.error(f"❌ RPC Error during user client start: {e}")
        return

    # Start the bot client
    try:
        await bot_client.start(bot_token=BOT_TOKEN)
    except RPCError as e:
        logger.error(f"❌ RPC Error during bot client start: {e}")
        return

    logger.info("✅ Bot is running and monitoring **call** messages...")

    # -----------------------------
    # Bot Command Handler: /start
    # -----------------------------
    @bot_client.on(events.NewMessage(pattern=r'^/start$'))
    async def start_handler(event):
        user_id = event.sender_id
        if not is_user_subscribed(user_id):
            add_subscribed_user(user_id)
            await event.respond("✅ You have been subscribed to gold signals.")
            logger.info(f"User {user_id} subscribed via /start.")
        else:
            await event.respond("You are already subscribed to gold signals.")

    # -----------------------------
    # Inline Button Callback Handler
    # -----------------------------
    @bot_client.on(events.CallbackQuery)
    async def callback_query_handler(event):
        data = event.data.decode('utf-8')

        # 1) "Get XAUUSD Price"
        if data == "get_xauusd_price":
            price = fetch_xauusd_price()
            if price is not None:
                await event.answer(
                    f"💰 Current XAUUSD Price: {price} USD",
                    alert=True
                )
            else:
                await event.answer(
                    "❌ Error fetching XAUUSD price. Please try again later.",
                    alert=True
                )
        
        # 2) "Unsubscribe"
        elif data == "unsubscribe_me":
            user_id = event.sender_id
            if is_user_subscribed(user_id):
                remove_subscribed_user(user_id)
                await event.answer("You have been unsubscribed from gold signals.", alert=True)
                logger.info(f"User {user_id} unsubscribed via inline button.")
            else:
                await event.answer("You are not currently subscribed.", alert=True)

    # -----------------------------
    # User Client: Forward Matching Messages
    # -----------------------------
    @user_client.on(events.NewMessage(chats=TARGET_CHANNELS))
    async def handler(event):
        message_text = event.message.message or ""
        if not message_text.strip():
            logger.info(f"📄 Skipped forwarding an empty or non-text message from {get_channel_display_name(event)}.")
            return

        if not CALL_PATTERN.search(message_text):
            logger.info(f"📄 Skipped forwarding a non-call message from {get_channel_display_name(event)}: {message_text}")
            return

        is_forwarded = event.message.fwd_from is not None
        escaped_message_text = escape_html(message_text)
        channel_name = get_channel_display_name(event)
        forward_text = f"🔔 **New Signal in {channel_name}:**\n\n{escaped_message_text}"
        if is_forwarded:
            forward_text += "\n*This message was forwarded from another chat.*"

        buttons = [
            [
                Button.inline("Get XAUUSD Price", b"get_xauusd_price"),
                Button.inline("Unsubscribe", b"unsubscribe_me")
            ]
        ]

        subscribed_users = get_all_subscribed_users()
        if not subscribed_users:
            logger.info("No subscribed users to forward this signal to.")
            return

        for user_id in subscribed_users:
            try:
                await bot_client.send_message(
                    entity=user_id,
                    message=forward_text,
                    buttons=buttons
                )
                logger.info(f"📩 Forwarded signal to user ID {user_id}: {message_text}")
            except Exception as e:
                logger.error(f"❌ Error when forwarding to user ID {user_id}: {e}")

    # Keep both clients running until manually stopped
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

# -----------------------------
# 8. Run the Script
# -----------------------------
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\n🔒 Bot stopped by user.")
    except Exception as e:
        logger.error(f"\n❌ An unexpected error occurred: {e}")
