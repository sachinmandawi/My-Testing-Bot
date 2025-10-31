# -*- coding: utf-8 -*-
"""
AutoApproveBot (v4.9 - With Auto-Delete for Old Backups)
Single DB: Local JSON only (data.json)
Author: Adapted for Sachin Sir 🔥
Features:
 - Keeps only the last 5 backups in owner chat, older ones are auto-deleted.
 - Import (overwrite), Import & Merge, Export, Clear DB (with backup)
 - UNDO (restore last backup) via "last_backup.json"
 - Automatic periodic backups to owners (default 60 minutes) + custom interval
 - Force-join, delayed approval, owner panel, broadcast, owner management
"""

import json
import os
import shutil
import traceback
import re
from datetime import datetime, timedelta

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    ChatJoinRequest,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ChatJoinRequestHandler,
)
from telegram.ext.filters import BaseFilter

# ================ CONFIG =================
BOT_TOKEN = "7666547004:AAHArJPZXZCia2aJqc52cyJy5v-HyOnlTK0"  # <-- Put your bot token here
OWNER_ID = 8070535163  # default owner; can be managed via bot
# --- File Names ---
DATA_FILE = "data.json"
LAST_BACKUP_FILE = "last_backup.json"  # used for UNDO
# =========================================

WELCOME_TEXT = (
    "🤡 Hey you! \n"
    "I auto-approve faster than your crush ignores your texts. \n"
    "But I can’t work outside the group — add me there so I can show off!"
)

DEFAULT_DATA = {
    "subscribers": [],
    "owners": [OWNER_ID],
    "force": {
        "enabled": False,
        "channels": [],
        "check_btn_text": "✅ Verify",
    },
    "approval_delay_minutes": 0,
    "known_chats": [],
    "auto_backup": {
        "enabled": True,
        "interval_minutes": 60  # default 60 minutes
    },
    # --- NEW: To track sent backup messages for auto-deletion ---
    "sent_backup_messages": {}, # format: {"owner_id": [msg_id1, msg_id2, ...]}
}


# ---------- Local DB Helpers ----------
def _ensure_data_keys(data):
    for key, value in DEFAULT_DATA.items():
        data.setdefault(key, value)
    if "force" in data:
        for k, v in DEFAULT_DATA["force"].items():
            data["force"].setdefault(k, v)
    if "auto_backup" not in data:
        data["auto_backup"] = DEFAULT_DATA["auto_backup"].copy()
    # --- NEW: Ensure the backup message log exists ---
    if "sent_backup_messages" not in data:
        data["sent_backup_messages"] = {}
    return data


def load_data_from_local():
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_DATA, f, indent=2)
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except Exception:
            data = DEFAULT_DATA.copy()
    return _ensure_data_keys(data)


def save_data_to_local(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# Convenience wrappers
def load_data():
    return load_data_from_local()


def save_data(data):
    save_data_to_local(data)


def is_owner(uid: int) -> bool:
    data = load_data()
    return uid in data.get("owners", [])


class IsOwnerFilter(BaseFilter):
    def filter(self, message):
        if not message or not getattr(message, "from_user", None):
            return False
        return is_owner(message.from_user.id)


is_owner_filter = IsOwnerFilter()


# ---------- Merge helpers ----------
def _unique_by_key(list_of_dicts, key):
    seen = set()
    out = []
    for d in list_of_dicts:
        k = d.get(key)
        if k is None:
            k = json.dumps(d, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def merge_data(existing: dict, new: dict):
    merged = dict(existing)  # shallow copy
    summary = {"owners_added": 0, "subs_added": 0, "chats_added": 0, "force_channels_added": 0, "delay_changed": False}

    # Owners
    e_owners = set(existing.get("owners", []))
    n_owners = set(new.get("owners", []))
    combined_owners = list(e_owners.union(n_owners))
    summary["owners_added"] = max(0, len(combined_owners) - len(e_owners))
    merged["owners"] = combined_owners

    # Subscribers
    e_subs = set(existing.get("subscribers", []))
    n_subs = set(new.get("subscribers", []))
    combined_subs = list(e_subs.union(n_subs))
    summary["subs_added"] = max(0, len(combined_subs) - len(e_subs))
    merged["subscribers"] = combined_subs

    # Known chats (union by chat_id)
    e_chats = existing.get("known_chats", []) or []
    n_chats = new.get("known_chats", []) or []
    combined_chats = e_chats.copy()
    existing_ids = {c.get("chat_id") for c in e_chats}
    added_chats = 0
    for c in n_chats:
        if c.get("chat_id") not in existing_ids:
            combined_chats.append(c)
            existing_ids.add(c.get("chat_id"))
            added_chats += 1
    merged["known_chats"] = combined_chats
    summary["chats_added"] = added_chats

    # Force channels merge (unique by chat_id or invite)
    e_force = existing.get("force", {}) or {}
    n_force = new.get("force", {}) or {}
    e_channels = e_force.get("channels", []) or []
    n_channels = n_force.get("channels", []) or []
    combined_channels = e_channels.copy()
    seen = set()
    for ch in e_channels:
        key = ch.get("chat_id") or ch.get("invite")
        if key is not None:
            seen.add(key)
    added_force = 0
    for ch in n_channels:
        key = ch.get("chat_id") or ch.get("invite")
        if key not in seen:
            combined_channels.append(ch)
            seen.add(key)
            added_force += 1
    merged_force = dict(e_force)
    merged_force["channels"] = combined_channels
    if n_force.get("check_btn_text"):
        merged_force["check_btn_text"] = n_force.get("check_btn_text")
    merged["force"] = merged_force
    summary["force_channels_added"] = added_force

    # approval_delay_minutes: prefer new if >0 else existing
    e_delay = existing.get("approval_delay_minutes", 0)
    n_delay = new.get("approval_delay_minutes", 0)
    try:
        n_delay_int = int(n_delay)
    except Exception:
        n_delay_int = 0
    if n_delay_int and n_delay_int > 0 and n_delay_int != e_delay:
        merged["approval_delay_minutes"] = n_delay_int
        summary["delay_changed"] = True
    else:
        merged["approval_delay_minutes"] = e_delay

    # copy other keys that might be present in new but not in existing
    for k, v in new.items():
        if k not in merged:
            merged[k] = v

    # preserve auto_backup settings if not provided in new
    if "auto_backup" not in merged:
        merged["auto_backup"] = existing.get("auto_backup", DEFAULT_DATA["auto_backup"]).copy()
    # DO NOT merge "sent_backup_messages"; always keep the existing log
    merged["sent_backup_messages"] = existing.get("sent_backup_messages", {})


    return merged, summary


# ---------- Utility functions ----------
def _normalize_channel_entry(raw):
    if isinstance(raw, dict):
        return {
            "chat_id": raw.get("chat_id") or raw.get("chat") or None,
            "invite": raw.get("invite") or raw.get("url") or None,
            "join_btn_text": raw.get("join_btn_text") or raw.get("button") or None,
        }
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("http://") or text.startswith("https://"):
            return {"chat_id": None, "invite": text, "join_btn_text": None}
        else:
            return {"chat_id": text, "invite": None, "join_btn_text": None}
    return {"chat_id": None, "invite": None, "join_btn_text": None}


def _derive_query_chat_from_entry(ch):
    chat_id = ch.get("chat_id")
    invite = ch.get("invite")
    if chat_id:
        return chat_id
    if invite and "t.me/" in invite:
        parts = invite.rstrip("/").split("/")
        possible = parts[-1] if parts else ""
        if possible and not possible.lower().startswith(("joinchat", "+")):
            return possible if possible.startswith("@") else f"@{possible}"
    return None


def build_join_keyboard_for_channels_list(ch_list, force_cfg):
    buttons = []
    for ch in ch_list:
        join_label = ch.get("join_btn_text") or "🔗 Join Channel"
        if ch.get("invite"):
            try:
                btn = InlineKeyboardButton(join_label, url=ch["invite"])
            except Exception:
                btn = InlineKeyboardButton(join_label, callback_data="force_no_invite")
        else:
            chat = ch.get("chat_id") or ""
            if chat and str(chat).startswith("@"):
                btn = InlineKeyboardButton(join_label, url=f"https://t.me/{str(chat).lstrip('@')}")
            else:
                btn = InlineKeyboardButton(join_label, callback_data="force_no_invite")
        buttons.append(btn)

    rows = []
    i = 0
    while i < len(buttons):
        if i + 1 < len(buttons):
            rows.append([buttons[i], buttons[i + 1]])
            i += 2
        else:
            rows.append([buttons[i]])
            i += 1

    check_label = force_cfg.get("check_btn_text") or "✅ Verify"
    rows.append([InlineKeyboardButton(check_label, callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


async def get_missing_channels(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    data = load_data()
    force = data.get("force", {})
    raw_channels = force.get("channels", []) or []
    normalized = [_normalize_channel_entry(c) for c in raw_channels]

    if not normalized:
        return [], False

    any_check_attempted = False
    any_check_succeeded = False
    missing = []

    for ch in normalized:
        query_chat = _derive_query_chat_from_entry(ch)
        if query_chat:
            try:
                any_check_attempted = True
                member = await context.bot.get_chat_member(chat_id=query_chat, user_id=user_id)
                any_check_succeeded = True
                if member.status in ("left", "kicked"):
                    missing.append(ch)
            except Exception:
                missing.append(ch)
                continue
        else:
            missing.append(ch)

    check_failed = not any_check_attempted and any_check_succeeded is False
    return missing, check_failed


async def prompt_user_with_missing_channels(update: Update, context: ContextTypes.DEFAULT_TYPE, missing_norm_list, check_failed=False):
    if not missing_norm_list and not check_failed:
        return

    if update.callback_query:
        recipient_id = update.callback_query.message.chat_id
    elif isinstance(update, Update) and hasattr(update, "chat_join_request") and update.chat_join_request:
        recipient_id = update.chat_join_request.from_user.id
    else:
        recipient_id = update.message.chat_id

    if missing_norm_list:
        total = len(load_data().get("force", {}).get("channels", []))
        missing_count = len(missing_norm_list)
        joined_count = max(0, total - missing_count)

        if joined_count == 0:
            text = (
                "🔒 *Access Restricted*\n\n"
                "You need to join the required channels before being approved.\n\n"
                "Tap each **Join** button below, join those channels, and then press **Verify** to continue."
            )
        else:
            text = (
                "🔒 *Access Restricted*\n\n"
                "You’ve joined some channels, but a few are still left.\n\n"
                "Tap the **Join** buttons below for the remaining channels, then press **Verify** once done."
            )

        kb = build_join_keyboard_for_channels_list(missing_norm_list, load_data().get("force", {}))
    else:
        text = "⚠️ I couldn't verify memberships (bot may not have access). Owner, please check bot permissions."
        kb = None

    try:
        if update.callback_query:
            await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
        elif isinstance(update, Update) and hasattr(update, "chat_join_request") and update.chat_join_request:
            await context.bot.send_message(recipient_id, text, parse_mode="Markdown", reply_markup=kb)
            try:
                await context.bot.decline_chat_join_request(
                    chat_id=update.chat_join_request.chat.id,
                    user_id=update.chat_join_request.from_user.id,
                )
            except Exception as e:
                print(f"Failed to decline join request for {recipient_id}: {e}")
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        print(f"Failed to send prompt message to user {recipient_id}: {e}")


# ---------- Keyboards ----------
def owner_panel_kb():
    kb = [
        [
            InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast"),
            InlineKeyboardButton("🔒 Force Join", callback_data="owner_force"),
        ],
        [
            InlineKeyboardButton("🧑‍💼 Manage Owner", callback_data="owner_manage"),
            InlineKeyboardButton("🕒 Set Delay", callback_data="owner_set_delay"),
        ],
        [InlineKeyboardButton("🗄️ Database", callback_data="owner_db")],
        [InlineKeyboardButton("⬅️ Close", callback_data="owner_close")],
    ]
    return InlineKeyboardMarkup(kb)


def db_panel_kb():
    kb = [
        [InlineKeyboardButton("📥 Import (overwrite)", callback_data="db_import"), InlineKeyboardButton("📤 Export", callback_data="db_export")],
        [InlineKeyboardButton("📥 Import & Merge", callback_data="db_import_merge")],
        [InlineKeyboardButton("🧹 Clear DB", callback_data="db_clear")],
        [InlineKeyboardButton("↩️ Undo Last Backup", callback_data="db_undo")],
        [InlineKeyboardButton("⚙️ Auto Backup", callback_data="db_autobackup")],
        [InlineKeyboardButton("⬅️ Back to Owner Panel", callback_data="db_back")],
    ]
    return InlineKeyboardMarkup(kb)


def autobackup_kb(data):
    ab = data.get("auto_backup", {})
    enabled = ab.get("enabled", False)
    interval = ab.get("interval_minutes", 60)
    kb = [
        [InlineKeyboardButton(f"🔁 Toggle Auto-Backup ({'On' if enabled else 'Off'})", callback_data="db_backup_toggle")],
        [InlineKeyboardButton(f"⏱️ Set Interval ({interval} minutes)", callback_data="db_backup_set_interval")],
        [InlineKeyboardButton("⬅️ Back", callback_data="db_back")],
    ]
    return InlineKeyboardMarkup(kb)


def broadcast_target_kb():
    kb = [
        [InlineKeyboardButton("👥 Users", callback_data="broadcast_target_users"), InlineKeyboardButton("🏷️ Groups", callback_data="broadcast_target_chats")],
        [InlineKeyboardButton("🌐 All", callback_data="broadcast_target_all"), InlineKeyboardButton("⬅️ Back", callback_data="owner_back_from_broadcast")],
    ]
    return InlineKeyboardMarkup(kb)


def force_setting_kb(force: dict):
    kb = [
        [InlineKeyboardButton("🔁 Toggle Force-Join", callback_data="force_toggle"), InlineKeyboardButton("➕ Add Channel", callback_data="force_add")],
        [InlineKeyboardButton("🗑️ Remove Channel", callback_data="force_remove"), InlineKeyboardButton("📜 List Channel", callback_data="force_list")],
        [InlineKeyboardButton("⬅️ Back", callback_data="force_back")],
    ]
    return InlineKeyboardMarkup(kb)


def cancel_btn():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)


# ---------- Auto-backup helpers ----------
def parse_interval_to_minutes(text: str) -> int:
    """
    Accepts inputs:
      - "30" -> 30 minutes
      - "30m" -> 30 minutes
      - "2h" -> 120 minutes
      - "1h30m" -> 90 minutes
    Returns minutes (int) or raises ValueError.
    """
    if not text or not isinstance(text, str):
        raise ValueError("Invalid interval format.")
    s = text.strip().lower()
    # only digits -> minutes
    if re.fullmatch(r"\d+", s):
        return int(s)
    total = 0
    # match e.g. 1h30m or 2h or 45m
    m = re.findall(r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?", s)
    if not m:
        raise ValueError("Invalid interval format.")
    hours = 0
    minutes = 0
    # m is list of tuples; take first
    hh, mm = m[0]
    if hh:
        hours = int(hh)
    if mm:
        minutes = int(mm)
    total = hours * 60 + minutes
    if total <= 0:
        raise ValueError("Interval must be positive.")
    return total


# --- MODIFIED FUNCTION ---
async def perform_and_send_backup(context: ContextTypes.DEFAULT_TYPE):
    """
    Create a backup, send to owners, log message IDs, and delete old backups.
    """
    try:
        data = load_data()
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        fname = f"auto_backup_{timestamp}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)

        # Update last backup file for UNDO
        try:
            shutil.copyfile(fname, LAST_BACKUP_FILE)
        except Exception:
            with open(LAST_BACKUP_FILE, "w", encoding="utf-8") as lf:
                json.dump(data, lf, indent=4)

        owners = data.get("owners", []) or [OWNER_ID]
        backup_log = data.setdefault("sent_backup_messages", {})

        for o in owners:
            try:
                # Send the new backup
                sent_message = await context.bot.send_document(
                    chat_id=o,
                    document=open(fname, "rb"),
                    caption=f"📦 Auto-backup: {timestamp}"
                )

                # Log the new backup message ID
                owner_log = backup_log.setdefault(str(o), [])
                owner_log.append(sent_message.message_id)

                # If log exceeds 5, delete the oldest one
                if len(owner_log) > 5:
                    msg_to_delete = owner_log.pop(0)  # Get and remove the oldest ID
                    try:
                        await context.bot.delete_message(chat_id=o, message_id=msg_to_delete)
                    except Exception as e:
                        print(f"Could not delete old backup message {msg_to_delete} for owner {o}: {e}")

            except Exception as send_err:
                print(f"Failed to send backup to owner {o}: {send_err}")
                continue

        # Save data once after all owners are processed
        save_data(data)

        # remove local timestamped file to keep disk clean
        try:
            os.remove(fname)
        except Exception:
            pass
    except Exception as e:
        # notify owners about failure
        try:
            owners = load_data().get("owners", []) or [OWNER_ID]
            for o in owners:
                try:
                    await context.bot.send_message(o, f"⚠️ Auto-backup failed: {e}")
                except Exception:
                    pass
        except Exception:
            pass


def schedule_auto_backup_job(application: Application, interval_minutes: int):
    """
    Cancel existing auto-backup jobs and schedule a new one.
    """
    # cancel existing
    try:
        existing = application.job_queue.get_jobs_by_name("auto_backup")
        for j in existing:
            j.schedule_removal()
    except Exception:
        pass

    # schedule new
    interval_seconds = max(30, int(interval_minutes) * 60)  # minimum 30 seconds safety
    try:
        application.job_queue.run_repeating(perform_and_send_backup, interval=interval_seconds, first=10, name="auto_backup")
    except Exception as e:
        print(f"[ERR] Failed to schedule auto-backup: {e}")


# ---------- Commands ----------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = load_data()

    chat = update.effective_chat
    if chat and chat.type in ("group", "supergroup", "channel"):
        known = data.setdefault("known_chats", [])
        exists = any(k.get("chat_id") == chat.id for k in known)
        if not exists:
            known.append({"chat_id": chat.id, "title": chat.title or chat.username or str(chat.id), "type": chat.type})
            save_data(data)

    if not is_owner(user.id):
        force = data.get("force", {})
        if force.get("enabled", False):
            if force.get("channels"):
                missing, check_failed = await get_missing_channels(context, user.id)
                if not missing:
                    subs = data.setdefault("subscribers", [])
                    if user.id not in subs:
                        subs.append(user.id)
                        save_data(data)
                else:
                    subs = data.setdefault("subscribers", [])
                    if user.id in subs:
                        subs.remove(user.id)
                        save_data(data)
                    await prompt_user_with_missing_channels(update, context, missing, check_failed)
                    return
            else:
                await update.message.reply_text("⚠️ Force-Join is enabled but no channels are configured. Owner, please configure channels via /owner.")
                return

    subs = data.setdefault("subscribers", [])
    if user.id not in subs:
        subs.append(user.id)
        save_data(data)

    bot_username = (await context.bot.get_me()).username
    add_to_group_button = InlineKeyboardButton("➕ Add Me To Your Group ➕", url=f"https://t.me/{bot_username}?startgroup=true")
    keyboard = InlineKeyboardMarkup([[add_to_group_button]])

    await update.message.reply_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=keyboard)


async def owner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ Only owners can access this panel.")
        return
    await update.message.reply_text("🔧 *Owner Panel*\n\nChoose an option:", parse_mode="Markdown", reply_markup=owner_panel_kb())


# ---------- Callback Handler ----------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    payload = query.data
    data = load_data()

    if not is_owner(uid) and payload.startswith(("owner_", "db_", "mgr_", "force_")):
        await query.message.reply_text("❌ Only owners can use this function.")
        return

    # DB Management
    if payload == "owner_db":
        await query.message.edit_text("🗄️ *Database Management*\n\nManage database settings, backups, imports and merges.", parse_mode="Markdown", reply_markup=db_panel_kb())
        return

    if payload == "db_back":
        await query.message.edit_text("🔧 *Owner Panel*\n\nChoose an option:", parse_mode="Markdown", reply_markup=owner_panel_kb())
        return

    if payload == "db_export":
        try:
            data_to_export = load_data()
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_filename = f"backup_{timestamp}.json"
            with open(backup_filename, "w", encoding="utf-8") as f:
                json.dump(data_to_export, f, indent=4)
            await context.bot.send_document(chat_id=query.message.chat_id, document=open(backup_filename, "rb"), caption="📄 Here is the database export.")
            os.remove(backup_filename)
        except Exception as e:
            await query.message.reply_text(f"❌ Export failed: {e}")
        return

    if payload == "db_import":
        context.user_data["flow"] = "db_import_file"
        await query.message.reply_text("📥 Please upload the `.json` backup file to IMPORT (this will overwrite current DB).", reply_markup=cancel_btn())
        return

    if payload == "db_import_merge":
        context.user_data["flow"] = "db_import_merge_file"
        await query.message.reply_text("📥 Please upload the `.json` backup file to MERGE with current DB. A backup will be created automatically first.", reply_markup=cancel_btn())
        return

    # DB clear (with backup)
    if payload == "db_clear":
        kb = [
            [InlineKeyboardButton("✅ Confirm Clear (backup then clear)", callback_data="db_confirm_clear")],
            [InlineKeyboardButton("❌ Cancel", callback_data="db_back")],
        ]
        await query.message.edit_text(
            "⚠️ *Clear Database*\n\nThis will BACKUP the current database and then CLEAR all bot data (reset to defaults).\nThis action is irreversible except via the backup file.\n\nAre you sure?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if payload == "db_confirm_clear":
        await query.message.edit_text("⏳ Backing up database and clearing... Please wait.")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_filename = f"pre_clear_backup_local_{timestamp}.json"
        try:
            current_data = load_data()
            with open(backup_filename, "w", encoding="utf-8") as f:
                json.dump(current_data, f, indent=4)

            # Also update last backup (for UNDO)
            try:
                shutil.copyfile(backup_filename, LAST_BACKUP_FILE)
            except Exception:
                with open(LAST_BACKUP_FILE, "w", encoding="utf-8") as lf:
                    json.dump(current_data, lf, indent=4)

            # try sending backup to owner chat; fallback to owner ids
            try:
                await context.bot.send_document(chat_id=query.message.chat_id, document=open(backup_filename, "rb"), caption="📦 Backup before clearing DB")
            except Exception:
                for o in current_data.get("owners", []) or [OWNER_ID]:
                    try:
                        await context.bot.send_document(chat_id=o, document=open(backup_filename, "rb"), caption="📦 Backup before clearing DB")
                    except Exception:
                        pass

            # Now clear local DB
            new_data = DEFAULT_DATA.copy()
            # --- MODIFIED: Preserve owners after clear ---
            new_data["owners"] = current_data.get("owners", [OWNER_ID])
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(new_data, f, indent=2)


            try:
                os.remove(backup_filename)
            except Exception:
                pass

            await query.message.edit_text("✅ Database cleared. A backup was sent. Owners have been preserved.")
        except Exception as e:
            await query.message.edit_text(f"❌ Failed to clear DB: `{e}`", parse_mode="Markdown", reply_markup=db_panel_kb())
        return

    # UNDO last backup
    if payload == "db_undo":
        if not os.path.exists(LAST_BACKUP_FILE):
            await query.message.reply_text("ℹ️ No last backup found to restore.", reply_markup=db_panel_kb())
            return
        kb = [
            [InlineKeyboardButton("✅ Confirm Restore Last Backup", callback_data="db_confirm_undo")],
            [InlineKeyboardButton("❌ Cancel", callback_data="db_back")],
        ]
        await query.message.edit_text("⚠️ *Restore Last Backup*\n\nThis will overwrite current DB with the most recent backup. Continue?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kb))
        return

    if payload == "db_confirm_undo":
        await query.message.edit_text("⏳ Restoring last backup... Please wait.")
        try:
            with open(LAST_BACKUP_FILE, "r", encoding="utf-8") as f:
                backup_data = json.load(f)
            if not isinstance(backup_data, dict) or "owners" not in backup_data:
                await query.message.edit_text("❌ Last backup file seems corrupted or invalid. Undo aborted.", reply_markup=db_panel_kb())
                return
            save_data(backup_data)
            await query.message.edit_text("✅ Restored database from last backup.", reply_markup=db_panel_kb())
        except Exception as e:
            await query.message.edit_text(f"❌ Failed to restore last backup: `{e}`", parse_mode="Markdown", reply_markup=db_panel_kb())
        return

    # Auto-backup settings
    if payload == "db_autobackup":
        data = load_data()
        await query.message.edit_text("⚙️ *Auto Backup Settings*\nManage automatic backups to owners.", parse_mode="Markdown", reply_markup=autobackup_kb(data))
        return

    if payload == "db_backup_toggle":
        data = load_data()
        ab = data.setdefault("auto_backup", {})
        new_state = not bool(ab.get("enabled", False))
        ab["enabled"] = new_state
        save_data(data)
        # schedule/unschedule job
        app = context.application
        if new_state:
            interval = ab.get("interval_minutes", 60)
            schedule_auto_backup_job(app, interval)
            await query.message.edit_text(f"✅ Auto-backup ENABLED. Interval: {interval} minutes.", reply_markup=autobackup_kb(data))
        else:
            jobs = app.job_queue.get_jobs_by_name("auto_backup")
            for j in jobs:
                j.schedule_removal()
            await query.message.edit_text("✅ Auto-backup DISABLED.", reply_markup=autobackup_kb(data))
        return

    if payload == "db_backup_set_interval":
        context.user_data["flow"] = "set_backup_interval"
        await query.message.reply_text("⏱️ Send new interval. Examples: 30m | 2h | 1h30m", reply_markup=cancel_btn())
        return

    # Owner close
    if payload == "owner_close":
        await query.message.edit_text("✅ Owner panel closed.")
        return

    # Set delay
    if payload == "owner_set_delay":
        current_delay = data.get("approval_delay_minutes", 0)
        context.user_data["flow"] = "set_delay_time"
        await query.message.reply_text(
            f"🕒 *Set Approval Delay*\n\nCurrent delay is `{current_delay}` minutes.\n\nSend the new delay time in minutes (e.g., `5`). Send `0` for immediate approval.",
            parse_mode="Markdown",
            reply_markup=cancel_btn(),
        )
        return

    # Broadcast
    if payload == "owner_broadcast":
        await query.message.edit_text("📢 *Broadcast*\nChoose target:", parse_mode="Markdown", reply_markup=broadcast_target_kb())
        return

    if payload in ("broadcast_target_users", "broadcast_target_chats", "broadcast_target_all"):
        target_map = {
            "broadcast_target_users": "users",
            "broadcast_target_chats": "chats",
            "broadcast_target_all": "all",
        }
        target = target_map.get(payload)
        context.user_data["flow"] = "broadcast_text"
        context.user_data["broadcast_target"] = target
        await query.message.reply_text(f"📢 Send the message to broadcast to *{target}*:", parse_mode="Markdown", reply_markup=cancel_btn())
        return

    if payload == "owner_back_from_broadcast":
        await query.message.edit_text("🔧 *Owner Panel*\n\nChoose an option:", parse_mode="Markdown", reply_markup=owner_panel_kb())
        return

    # Owner manage
    if payload == "owner_manage":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Add Owner", callback_data="mgr_add"), InlineKeyboardButton("📜 List Owners", callback_data="mgr_list")],
                [InlineKeyboardButton("🗑️ Remove Owner", callback_data="mgr_remove"), InlineKeyboardButton("⬅️ Back", callback_data="mgr_back")],
            ]
        )
        await query.message.edit_text("🧑‍💼 *Manage Owner*", parse_mode="Markdown", reply_markup=kb)
        return

    if payload == "mgr_add":
        context.user_data["flow"] = "mgr_add"
        await query.message.reply_text("➕ Send numeric user ID to add as owner:", reply_markup=cancel_btn())
        return

    if payload == "mgr_list":
        owners = data.get("owners", [])
        msg = "🧑‍💼 *Owners:*\n" + "\n".join([f"{i+1}. `{o}`" for i, o in enumerate(owners)])
        await query.message.reply_text(msg, parse_mode="Markdown")
        return

    if payload == "mgr_remove":
        owners = data.get("owners", [])
        if len(owners) <= 1:
            await query.message.reply_text("❌ At least one owner must remain.")
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Remove: {o}", callback_data=f"mgr_rem_{i}")] for i, o in enumerate(owners)])
        await query.message.reply_text("Select an owner to remove:", reply_markup=kb)
        return

    if payload.startswith("mgr_rem_"):
        idx = int(payload.split("_")[-1])
        try:
            removed = data["owners"].pop(idx)
            save_data(data)
            await query.message.edit_text(f"✅ Removed owner `{removed}`", parse_mode="Markdown")
            try:
                await context.bot.send_message(
                    chat_id=removed,
                    text="ℹ️ You have been removed as an owner of this bot."
                )
            except Exception as e:
                print(f"[INFO] Could not notify removed owner {removed}: {e}")
        except Exception:
            await query.message.reply_text("❌ Invalid selection.")
        return

    if payload == "mgr_back":
        await query.message.edit_text("🔧 *Owner Panel*\n\nChoose an option:", parse_mode="Markdown", reply_markup=owner_panel_kb())
        return

    # Force Join
    if payload == "owner_force":
        force = data.get("force", {})
        status_text = "Enabled ✅" if force.get("enabled", False) else "Disabled ❌"
        msg = f"🔒 *Force Join Setting*\n\nStatus: `{status_text}`\n\nChoose an action:"
        await query.message.edit_text(msg, parse_mode="Markdown", reply_markup=force_setting_kb(force))
        return

    if payload == "force_toggle":
        data = load_data()
        force = data.setdefault("force", {})
        new_state = not force.get("enabled", False)
        force["enabled"] = new_state
        save_data(data)
        status_text = "Enabled ✅" if new_state else "Disabled ❌"
        msg = f"🔒 *Force Join Setting*\n\nStatus: `{status_text}`\n\nChoose an action:"
        await query.message.edit_text(msg, parse_mode="Markdown", reply_markup=force_setting_kb(force))
        if new_state and not force.get("channels"):
            await query.message.reply_text("⚠️ Force-Join enabled but no channels configured. Add channels using Add Channel.", parse_mode="Markdown")
        return

    if payload == "force_add":
        context.user_data["flow"] = "force_add_step1"
        await query.message.reply_text(
            "➕ *Add Channel*\n\nSend channel identifier or invite link.\nExamples:\n - `@MyChannel`\n - `-1001234567890`\n - `https://t.me/joinchat/XXXX`",
            parse_mode="Markdown",
            reply_markup=cancel_btn(),
        )
        return

    if payload == "force_remove":
        channels = data.get("force", {}).get("channels", [])
        if not channels:
            await query.message.reply_text("ℹ️ No channels configured.")
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Remove: {ch.get('chat_id') or ch.get('invite') or str(i)}", callback_data=f"force_rem_{i}")] for i, ch in enumerate(channels)])
        await query.message.reply_text("Select channel to remove:", reply_markup=kb)
        return

    if payload.startswith("force_rem_"):
        try:
            idx = int(payload.split("_")[-1])
            channels = data.get("force", {}).get("channels", [])
            removed = channels.pop(idx)
            data["force"]["channels"] = channels
            save_data(data)
            await query.message.reply_text(f"✅ Removed channel `{removed.get('chat_id') or removed.get('invite')}`", parse_mode="Markdown")
        except Exception:
            await query.message.reply_text("❌ Invalid selection.")
        return

    if payload == "force_list":
        channels = data.get("force", {}).get("channels", [])
        if not channels:
            await query.message.reply_text("ℹ️ No channels configured.")
            return
        lines = ["📜 *Configured Channels:*"]
        for i, ch in enumerate(channels, start=1):
            lines.append(f"{i}. `chat_id`: `{ch.get('chat_id') or '—'}`\n   `invite`: `{ch.get('invite') or '—'}`\n   `button`: `{ch.get('join_btn_text') or '🔗 Join Channel'}`")
        await query.message.reply_text("\n\n".join(lines), parse_mode="Markdown")
        return

    if payload == "force_back":
        await query.message.edit_text("🔧 *Owner Panel*\n\nChoose an option:", parse_mode="Markdown", reply_markup=owner_panel_kb())
        return

    if payload == "force_no_invite":
        await query.message.reply_text("⚠️ No invite URL configured for this channel. Contact the owner.")
        return

    # Verification: check_join
    if payload == "check_join":
        uid = query.from_user.id
        data = load_data()

        if is_owner(uid) or not data.get("force", {}).get("enabled", False):
            await query.message.reply_text("✅ Verification passed. Access granted.")
            return

        missing, check_failed = await get_missing_channels(context, uid)

        if not missing:
            subs = data.setdefault("subscribers", [])
            if uid not in subs:
                subs.append(uid)
                save_data(data)

            await query.message.reply_text("✅ Verification complete!")
            bot_username = (await context.bot.get_me()).username
            add_to_group_button = InlineKeyboardButton("➕ Add Me To Your Group ➕", url=f"https://t.me/{bot_username}?startgroup=true")
            keyboard = InlineKeyboardMarkup([[add_to_group_button]])

            await query.message.reply_text(WELCOME_TEXT, parse_mode="Markdown", reply_markup=keyboard)
        else:
            subs = data.setdefault("subscribers", [])
            if uid in subs:
                subs.remove(uid)
                save_data(data)
            try:
                await query.message.delete()
            except Exception:
                pass
            await prompt_user_with_missing_channels(update, context, missing, check_failed=check_failed)
        return

    # fallback
    await query.message.reply_text("Unknown action.")


# ---------- Owner Text & File Handler ----------
async def owner_flow_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        return

    flow = context.user_data.get("flow")

    # --- DB import (overwrite) Flow (File Upload) ---
    if flow == "db_import_file" and update.message.document:
        if not update.message.document.file_name.endswith(".json"):
            await update.message.reply_text("❌ Invalid file type. Please upload a `.json` file.", reply_markup=ReplyKeyboardRemove())
            context.user_data.clear()
            return
        try:
            # Backup current data first
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_filename = f"pre_import_backup_{timestamp}.json"
            current_data = load_data()
            with open(backup_filename, "w", encoding="utf-8") as f:
                json.dump(current_data, f, indent=4)

            # Also update last backup (for UNDO)
            try:
                shutil.copyfile(backup_filename, LAST_BACKUP_FILE)
            except Exception:
                with open(LAST_BACKUP_FILE, "w", encoding="utf-8") as lf:
                    json.dump(current_data, lf, indent=4)

            try:
                await context.bot.send_document(chat_id=update.message.chat_id, document=open(backup_filename, "rb"), caption="📦 Backup before import (overwrite)")
            except Exception:
                for o in current_data.get("owners", []) or [OWNER_ID]:
                    try:
                        await context.bot.send_document(chat_id=o, document=open(backup_filename, "rb"), caption="📦 Backup before import (overwrite)")
                    except Exception:
                        pass

            # load uploaded file
            json_file = await update.message.document.get_file()
            file_content = await json_file.download_as_bytearray()
            new_data = json.loads(file_content.decode("utf-8"))
            if not isinstance(new_data, dict) or "owners" not in new_data:
                await update.message.reply_text("❌ Invalid JSON structure.", reply_markup=ReplyKeyboardRemove())
                context.user_data.clear()
                try:
                    os.remove(backup_filename)
                except Exception:
                    pass
                return

            # Overwrite but preserve some settings
            if "auto_backup" not in new_data:
                new_data["auto_backup"] = current_data.get("auto_backup", DEFAULT_DATA["auto_backup"]).copy()
            if "sent_backup_messages" not in new_data:
                new_data["sent_backup_messages"] = current_data.get("sent_backup_messages", {})


            save_data(new_data)
            await update.message.reply_text("✅ Database successfully imported and overwritten.", reply_markup=ReplyKeyboardRemove())

            try:
                os.remove(backup_filename)
            except Exception:
                pass

            context.user_data.clear()
        except Exception as e:
            await update.message.reply_text(f"❌ Import failed: `{e}`", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
            context.user_data.clear()
        return

    # --- DB Import & Merge Flow (File Upload) ---
    if flow == "db_import_merge_file" and update.message.document:
        if not update.message.document.file_name.endswith(".json"):
            await update.message.reply_text("❌ Invalid file type. Please upload a `.json` file.", reply_markup=ReplyKeyboardRemove())
            context.user_data.clear()
            return
        try:
            # 1) Backup current DB
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            backup_filename = f"pre_merge_backup_{timestamp}.json"
            current_data = load_data()
            with open(backup_filename, "w", encoding="utf-8") as f:
                json.dump(current_data, f, indent=4)

            # Also update last backup (for UNDO)
            try:
                shutil.copyfile(backup_filename, LAST_BACKUP_FILE)
            except Exception:
                with open(LAST_BACKUP_FILE, "w", encoding="utf-8") as lf:
                    json.dump(current_data, lf, indent=4)

            # send backup to owner
            try:
                await context.bot.send_document(chat_id=update.message.chat_id, document=open(backup_filename, "rb"), caption="📦 Backup before merging DB")
            except Exception:
                for o in current_data.get("owners", []) or [OWNER_ID]:
                    try:
                        await context.bot.send_document(chat_id=o, document=open(backup_filename, "rb"), caption="📦 Backup before merging DB")
                    except Exception:
                        pass

            # 2) load uploaded file
            json_file = await update.message.document.get_file()
            file_content = await json_file.download_as_bytearray()
            new_data = json.loads(file_content.decode("utf-8"))
            if not isinstance(new_data, dict) or "owners" not in new_data:
                await update.message.reply_text("❌ Invalid JSON structure.", reply_markup=ReplyKeyboardRemove())
                context.user_data.clear()
                try:
                    os.remove(backup_filename)
                except Exception:
                    pass
                return

            # 3) perform merge
            existing = current_data
            merged, summary = merge_data(existing, new_data)
            save_data(merged)

            # 4) report summary
            msg_lines = [
                "✅ Merge completed.",
                f"Owners added: {summary['owners_added']}",
                f"Subscribers added: {summary['subs_added']}",
                f"Known chats added: {summary['chats_added']}",
                f"Force-channels added: {summary['force_channels_added']}",
                f"Approval delay changed: {'Yes' if summary['delay_changed'] else 'No'}",
            ]
            await update.message.reply_text("\n".join(msg_lines), reply_markup=ReplyKeyboardRemove())

            # cleanup backup file
            try:
                os.remove(backup_filename)
            except Exception:
                pass

            context.user_data.clear()
        except Exception as e:
            await update.message.reply_text(f"❌ Merge failed: `{e}`", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
            context.user_data.clear()
        return

    # If not a file flow, continue with text flows
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Cancel
    if text == "❌ Cancel":
        context.user_data.clear()
        await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
        return

    # Set approval delay
    if flow == "set_delay_time":
        try:
            delay_minutes = int(text)
            if delay_minutes < 0:
                await update.message.reply_text("❌ Please send non-negative number.")
                return
        except ValueError:
            await update.message.reply_text("❌ Invalid input. Please send numeric minutes.")
            return
        data = load_data()
        data["approval_delay_minutes"] = delay_minutes
        save_data(data)
        context.user_data.clear()
        await update.message.reply_text(f"✅ Approval delay set to `{delay_minutes}` minutes.", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        return

    # Set backup interval flow
    if flow == "set_backup_interval":
        try:
            minutes = parse_interval_to_minutes(text)
            if minutes < 1:
                raise ValueError("Interval must be at least 1 minute.")
        except Exception as e:
            await update.message.reply_text(f"❌ Invalid interval: {e}. Examples: `30`, `30m`, `2h`, `1h30m`.", reply_markup=cancel_btn())
            return

        data = load_data()
        ab = data.setdefault("auto_backup", {})
        ab["interval_minutes"] = minutes
        save_data(data)

        # reschedule job immediately if enabled
        app = context.application
        if ab.get("enabled", False):
            schedule_auto_backup_job(app, minutes)

        context.user_data.clear()
        await update.message.reply_text(f"✅ Auto-backup interval set to `{minutes}` minutes.", reply_markup=ReplyKeyboardRemove())
        return

    # Broadcast flow
    if flow == "broadcast_text":
        target = context.user_data.get("broadcast_target", "users")
        msg_text = text
        sent = 0
        failed = 0
        data = load_data()

        if target in ("users", "all"):
            subs = data.get("subscribers", []) or []
            for u in list(set(subs)):
                try:
                    await context.bot.send_message(u, msg_text)
                    sent += 1
                except Exception:
                    failed += 1
                    continue

        if target in ("chats", "all"):
            known = data.get("known_chats", []) or []
            for ch in known:
                cid = ch.get("chat_id")
                if cid is None:
                    continue
                try:
                    await context.bot.send_message(cid, msg_text)
                    sent += 1
                except Exception:
                    failed += 1
                    continue

        await update.message.reply_text(f"✅ Broadcast done. Sent: {sent}, Failed: {failed}", reply_markup=ReplyKeyboardRemove())
        context.user_data.clear()
        return

    # Add owner flow
    if flow == "mgr_add":
        try:
            new_owner = int(text)
        except Exception:
            await update.message.reply_text("❌ Please send numeric ID.")
            return
        data = load_data()
        owners = data.setdefault("owners", [])
        if new_owner in owners:
            await update.message.reply_text("Already an owner.")
            context.user_data.clear()
            return
        owners.append(new_owner)
        save_data(data)
        context.user_data.clear()
        await update.message.reply_text(f"✅ Added owner `{new_owner}`", parse_mode="Markdown", reply_markup=ReplyKeyboardRemove())
        try:
            await context.bot.send_message(
                chat_id=new_owner,
                text="🎉 Congratulations! You have been promoted to an owner of this bot."
            )
        except Exception as e:
            print(f"[INFO] Could not notify new owner {new_owner}: {e}")

        return

    # Force add steps
    if flow == "force_add_step1":
        entry = {"chat_id": None, "invite": None, "join_btn_text": None}
        if text.startswith("http://") or text.startswith("https://"):
            entry["invite"] = text
        else:
            entry["chat_id"] = text
        context.user_data["force_add_entry"] = entry
        context.user_data["flow"] = "force_add_step2"
        await update.message.reply_text(
            f"✅ Channel detected: `{entry.get('chat_id') or entry.get('invite')}`\n\nNow send the button text (e.g. `🔗 Join Channel`).",
            parse_mode="Markdown",
            reply_markup=cancel_btn(),
        )
        return

    if flow == "force_add_step2":
        entry = context.user_data.get("force_add_entry")
        if not entry:
            context.user_data.clear()
            await update.message.reply_text("❌ Unexpected error. Try again.", reply_markup=ReplyKeyboardRemove())
            return
        btn = text
        if len(btn) > 40:
            await update.message.reply_text("❌ Button text too long (max 40 chars).")
            return
        entry["join_btn_text"] = btn
        data = load_data()
        channels = data.setdefault("force", {}).setdefault("channels", [])
        channels.append(entry)
        data["force"]["channels"] = channels
        save_data(data)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Channel added!\n`{entry.get('chat_id') or entry.get('invite')}`\nButton: `{btn}`",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await update.message.reply_text("Unknown or expired operation. Use /owner to open owner panel.")


# ---------- Delayed Approval, Processing ----------
async def _approve_user_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    user_id = job.data["user_id"]
    try:
        await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        try:
            await context.bot.send_message(user_id, "✅ You have been automatically approved!")
        except Exception:
            pass
    except Exception as e:
        print(f"[ERR] Delayed approval failed for user {user_id} in chat {chat_id}: {e}")
        data = load_data()
        owners = data.get("owners", [])
        for o in owners:
            try:
                await context.bot.send_message(o, f"❗ Delayed approval failed for `{user_id}` in `{chat_id}`.\nError: `{e}`", parse_mode="Markdown")
            except Exception:
                pass


async def _process_approval(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    data = load_data()
    delay_minutes = data.get("approval_delay_minutes", 0)
    if delay_minutes and delay_minutes > 0:
        delay_seconds = int(delay_minutes) * 60
        try:
            context.job_queue.run_once(_approve_user_job, when=delay_seconds, data={"chat_id": chat_id, "user_id": user_id}, name=f"approve-{chat_id}-{user_id}")
        except Exception as e:
            print(f"[ERR] Failed to schedule approval for {user_id} in {chat_id}: {e}")
            for o in data.get("owners", []):
                try:
                    await context.bot.send_message(o, f"❗ Failed to schedule approval for `{user_id}` in `{chat_id}`.\nError: `{e}`", parse_mode="Markdown")
                except Exception:
                    pass
    else:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            try:
                await context.bot.send_message(user_id, "✅ You have been automatically approved!")
            except Exception:
                pass
        except Exception as e:
            print(f"[ERR] Failed to approve user {user_id} to {chat_id}: {e}")
            for o in data.get("owners", []):
                try:
                    await context.bot.send_message(o, (
                        f"❗ Failed to approve user `{user_id}` to chat `{chat_id}`.\n\n"
                        f"Error: `{e}`\n\n"
                        "Common causes:\n"
                        "- Bot is not admin in the chat.\n"
                        "- Bot lacks 'Invite Users via Link' permission."
                    ), parse_mode="Markdown")
                except Exception:
                    pass


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_join_request: ChatJoinRequest = update.chat_join_request
    user_id = chat_join_request.from_user.id
    chat_id = chat_join_request.chat.id
    data = load_data()

    if is_owner(user_id):
        await _process_approval(context, chat_id, user_id)
        return

    force = data.get("force", {})
    if force.get("enabled", False) and force.get("channels"):
        missing, check_failed = await get_missing_channels(context, user_id)
        if not missing:
            await _process_approval(context, chat_id, user_id)
        else:
            await prompt_user_with_missing_channels(update, context, missing, check_failed)
    else:
        await _process_approval(context, chat_id, user_id)


async def record_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if not chat:
        return
    if chat.type in ("group", "supergroup", "channel"):
        data = load_data()
        known = data.setdefault("known_chats", [])
        exists = any(k.get("chat_id") == chat.id for k in known)
        if not exists:
            known.append({"chat_id": chat.id, "title": chat.title or chat.username or str(chat.id), "type": chat.type})
            save_data(data)


# ---------- Main ----------
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("Please set BOT_TOKEN at the top of the script before running.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("owner", owner_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    app.add_handler(MessageHandler((filters.ChatType.GROUP | filters.ChatType.SUPERGROUP | filters.ChatType.CHANNEL) & ~filters.COMMAND, record_chat_handler))

    app.add_handler(MessageHandler(is_owner_filter & (filters.TEXT | filters.Document.FileExtension("json")) & ~filters.COMMAND, owner_flow_handler))

    # Schedule auto-backup job if enabled in data
    try:
        data = load_data()
        ab = data.get("auto_backup", {})
        if ab.get("enabled", False):
            interval = int(ab.get("interval_minutes", 60))
            schedule_auto_backup_job(app, interval)
            print(f"[SCHEDULE] Auto-backup scheduled every {interval} minutes.")
    except Exception as e:
        print(f"[WARN] Could not schedule auto-backup at startup: {e}")

    print("🤖 AutoApproveBot v4.9 (with auto-delete for old backups) running...")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
