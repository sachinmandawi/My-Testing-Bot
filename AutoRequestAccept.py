import sys
import asyncio
import signal
import uuid
from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat, User
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)
from telegram.constants import ParseMode
from telegram.helpers import escape_markdown

# ========== CONFIG ==========
API_ID = 23292615
API_HASH = 'fc15ff59f3a1d77e4d86ff6f3ded9d44'
BOT_TOKEN = '7666547004:AAHArJPZXZCia2aJqc52cyJy5v-HyOnlTK0'
USER_SESSION = 'user_session'
SEARCH_LIMIT = 200
RESULTS_PER_PAGE = 10

tele_client: TelegramClient | None = None
app = None
stop_event = asyncio.Event()


# ========== TELEGRAM SEARCH ==========
async def search_telegram_public(q: str, limit: int = SEARCH_LIMIT):
    global tele_client
    if tele_client is None:
        raise RuntimeError("Telethon client not initialized")
    if not tele_client.is_connected():
        await tele_client.connect()

    res = await tele_client(SearchRequest(q, limit))
    found = []

    for ch in res.chats:
        if getattr(ch, "username", None):
            item_type = "Group" if isinstance(ch, Chat) or getattr(ch, "megagroup", False) else "Channel"
            found.append(
                {
                    "title": getattr(ch, "title", None)
                    or getattr(ch, "first_name", None)
                    or "<no title>",
                    "username": ch.username,
                    "type": item_type,
                }
            )

    for user in res.users:
        if user.bot and getattr(user, "username", None):
            title = f"{user.first_name or ''} {user.last_name or ''}".strip() or "<no title>"
            found.append({"title": title, "username": user.username, "type": "Bot"})

    return found


# ========== HANDLERS ==========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_text = (
        "🔍 *Find anything on Telegram — instantly\\.*\n\n"
        "I’ll help you connect with the best Telegram **communities** and **groups** in seconds\\.\n\n"
        "_Type your keyword to begin your discovery ✨_"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN_V2)


async def display_page(update: Update, context: ContextTypes.DEFAULT_TYPE, search_id: str, page: int = 0):
    chat_id = update.effective_chat.id

    if "searches" not in context.chat_data or search_id not in context.chat_data["searches"]:
        await context.bot.send_message(chat_id, "This search has expired. Please start a new one.")
        return

    query, results = context.chat_data["searches"][search_id]
    start_index = page * RESULTS_PER_PAGE
    end_index = start_index + RESULTS_PER_PAGE
    paginated_results = results[start_index:end_index]

    escaped_query = escape_markdown(query, version=2)
    header = f"🔎 *Search Results for '{escaped_query}'*"
    lines = [header, ""]

    if not paginated_results and page == 0:
        lines.append("No public channels, groups, or bots found.")
    elif not paginated_results:
        lines.append("No more results found.")
    else:
        for r in paginated_results:
            title = escape_markdown(r["title"], version=2)
            username = r["username"]
            item_type = escape_markdown(f"[{r['type']}]", version=2)
            lines.append(f"» {item_type} [{title}](https://t.me/{username})")

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

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                message_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except Exception as e:
            print(f"Failed to edit message: {e}")
    else:
        await context.bot.send_message(
            chat_id,
            message_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )


async def new_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    message = update.message or update.callback_query.message
    await message.reply_text(
        f"Searching for: `{escape_markdown(query, version=2)}`\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    try:
        results = await search_telegram_public(query)
        if not results:
            await message.reply_text("No channels, groups, or bots found for that query.")
            return

        if "searches" not in context.chat_data:
            context.chat_data["searches"] = {}

        search_id = str(uuid.uuid4())
        context.chat_data["searches"][search_id] = (query, results)

        if len(context.chat_data["searches"]) > 10:
            oldest_key = next(iter(context.chat_data["searches"]))
            del context.chat_data["searches"][oldest_key]

        await display_page(update, context, search_id, page=0)
    except Exception as e:
        print(f"An error occurred: {e}")
        await message.reply_text(f"Search failed: {e}")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search <keyword>")
        return
    query = " ".join(context.args)
    await new_search(update, context, query)


async def echo_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if text and not text.startswith("/"):
        await new_search(update, context, text)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    if data[0] != "page":
        return
    search_id = data[1]
    page = int(data[2])
    await display_page(update, context, search_id, page)


# ========== ENTRYPOINT ==========
async def shutdown():
    global tele_client, app
    print("\nShutting down gracefully...")
    if app:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
    if tele_client:
        await tele_client.disconnect()
    stop_event.set()
    print("✅ Bot stopped.")


def handle_exit(signum, frame):
    asyncio.create_task(shutdown())


async def main():
    global tele_client, app

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    tele_client = TelegramClient(USER_SESSION, API_ID, API_HASH)
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), echo_all))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Starting Telethon client...")
    await tele_client.start()
    print("Telethon client started successfully.")
    print("Bot is running... Press Ctrl+C to stop.\n")

    async with app, tele_client:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        await stop_event.wait()  # waits until Ctrl+C
        await shutdown()


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
