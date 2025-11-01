# SearchBot_Final_Pagination.py
import sys
import asyncio
import uuid
from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler
)
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

# ========== CONFIG ==========
API_ID = 23292615
API_HASH = 'fc15ff59f3a1d77e4d86ff6f3ded9d44'
BOT_TOKEN = '7666547004:AAHArJPZXZCia2aJqc52cyJy5v-HyOnlTK0'
USER_SESSION = 'user_session'
SEARCH_LIMIT = 200  # Fetch more results for pagination
RESULTS_PER_PAGE = 10 # Results to show on one page

# Global telethon client placeholder
tele_client: TelegramClient | None = None

async def search_telegram_public(q: str, limit: int = SEARCH_LIMIT):
    """Uses Telethon to search for public channels and groups."""
    global tele_client
    if tele_client is None:
        raise RuntimeError("Telethon client not initialized")
    if not tele_client.is_connected():
        await tele_client.connect()
        
    res = await tele_client(SearchRequest(q, limit))
    
    found = []
    for ch in res.chats:
        if getattr(ch, 'username', None): # Only add if it has a username
            item = {
                'title': getattr(ch, 'title', None) or getattr(ch, 'first_name', None) or "<no title>",
                'username': ch.username
            }
            found.append(item)
    return found

# ========== Bot Handlers ==========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    await update.message.reply_text(
        "Hi — bhejo /search <keyword> and I'll try to find public Telegram groups & channels matching it.\n\n"
        "Example: /search python help"
    )

async def display_page(update: Update, context: ContextTypes.DEFAULT_TYPE, search_id: str, page: int = 0):
    """Displays a specific page of results and the navigation buttons."""
    chat_id = update.effective_chat.id
    query, results = context.chat_data['searches'][search_id]

    start_index = page * RESULTS_PER_PAGE
    end_index = start_index + RESULTS_PER_PAGE
    
    paginated_results = results[start_index:end_index]

    escaped_query = escape_markdown(query, version=2)
    header = f"🔎 *Search Results for '{escaped_query}'*"
    lines = [header, ""]

    if not paginated_results:
        lines.append("No more results found.")
    else:
        for r in paginated_results:
            title = escape_markdown(r['title'], version=2)
            username = r['username']
            lines.append(f"» [{title}](https://t.me/{username})")

    # --- Create Pagination Buttons ---
    buttons = []
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️ Previous", callback_data=f"page_{search_id}_{page-1}"))
    
    if end_index < len(results):
        row.append(InlineKeyboardButton("Next ▶️", callback_data=f"page_{search_id}_{page+1}"))
    
    if row:
        buttons.append(row)
        
    reply_markup = InlineKeyboardMarkup(buttons)
    message_text = "\n".join(lines)

    # If it's a button click, edit the message. Otherwise, send a new one.
    if update.callback_query:
        await update.callback_query.edit_message_text(
            message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )
    else:
        await context.bot.send_message(
            chat_id,
            message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )

async def new_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    """Initiates a new search, fetches data, and displays the first page."""
    await update.message.reply_text(f"Searching for: `{escape_markdown(query, version=2)}`\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    
    try:
        results = await search_telegram_public(query)
        if not results:
            await update.message.reply_text("No channels/groups with a public username found for that query.")
            return

        # Store results in user's chat_data
        if 'searches' not in context.chat_data:
            context.chat_data['searches'] = {}

        search_id = str(uuid.uuid4()) # Generate a unique ID for this search
        context.chat_data['searches'][search_id] = (query, results)
        
        await display_page(update, context, search_id, page=0)

    except Exception as e:
        print(f"An error occurred during search: {e}")
        await update.message.reply_text(f"Search failed: {e}")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search <keyword>")
        return
    query = " ".join(context.args)
    await new_search(update, context, query)

async def echo_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text and not text.startswith('/'):
        await new_search(update, context, text)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button clicks for pagination."""
    query = update.callback_query
    await query.answer() # Acknowledge the button press

    data = query.data.split("_")
    if data[0] != "page":
        return

    search_id = data[1]
    page = int(data[2])

    if 'searches' not in context.chat_data or search_id not in context.chat_data['searches']:
        await query.edit_message_text("This search has expired. Please start a new one.")
        return

    await display_page(update, context, search_id, page)

# ========== Entrypoint ==========
async def main():
    """Starts the Telethon client and runs the Telegram bot asynchronously."""
    global tele_client
    
    # Build the python-telegram-bot Application first
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_all))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Initialize Telethon client
    tele_client = TelegramClient(USER_SESSION, API_ID, API_HASH)

    print("Starting Telethon client...")
    await tele_client.start()
    print("Telethon client started successfully.")

    print("Bot is running... Press Ctrl+C to stop.")
    
    # Run PTB and Telethon concurrently
    async with app, tele_client:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        
        # Keep the script running until you press Ctrl+C
        await tele_client.run_until_disconnected()
        
        await app.updater.stop()
        await app.stop()


if __name__ == "__main__":
    # Windows compatibility
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    asyncio.run(main())
