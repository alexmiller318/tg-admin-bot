"""
Telegram Group Admin Bot
Features: anti-spam/flood, welcome messages, warn/kick/ban/unban,
word filter, mute/unmute, rules system.
Requires: python-telegram-bot>=20.7, python-dotenv
"""

import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from telegram import ChatPermissions, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------
# CONFIGURATION
# ---------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]

FLOOD_MAX_MESSAGES: int = 5
FLOOD_WINDOW: int = 5       # seconds
FLOOD_MUTE_DURATION: int = 300  # seconds (5 min)

WARN_KICK_THRESHOLD: int = 3

DATA_FILE = Path("bot_data.json")

# ---------------------------------
# LOGGING
# ---------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------
# PERSISTENT DATA HELPERS
# ---------------------------------

def _default_data() -> dict:
    return {
        "warns": {},
        "word_filters": {},
        "banned_name_words": {},
        "welcome": {},
        "rules": {},
    }


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return _default_data()


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# In-memory flood tracker: {chat_id: {user_id: [timestamps]}}
flood_tracker: dict = defaultdict(lambda: defaultdict(list))

# ---------------------------------
# PERMISSIONS
# ---------------------------------

MUTED = ChatPermissions(
    can_send_messages=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

UNMUTED = ChatPermissions(
    can_send_messages=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
)


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    admins = await context.bot.get_chat_administrators(chat_id)
    return any(a.user.id == user_id for a in admins)


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("Admin only.")
        return False
    return True


def get_target_user(update: Update):
    msg = update.message
    if msg.reply_to_message:
        u = msg.reply_to_message.from_user
        return u.id, u.full_name
    args = msg.text.split()[1:]
    if args:
        arg = args[0].lstrip("@")
        if arg.isdigit():
            return int(arg), arg
    return None, None


# ---------------------------------
# FLOOD PROTECTION
# ---------------------------------

async def flood_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    now = time.monotonic()
    admins = await context.bot.get_chat_administrators(chat_id)
    if any(a.user.id == user_id for a in admins):
        return
    tracker = flood_tracker[chat_id][user_id]
    tracker.append(now)
    flood_tracker[chat_id][user_id] = [t for t in tracker if now - t <= FLOOD_WINDOW]
    if len(flood_tracker[chat_id][user_id]) > FLOOD_MAX_MESSAGES:
        flood_tracker[chat_id][user_id] = []
        until = datetime.now(tz=timezone.utc) + timedelta(seconds=FLOOD_MUTE_DURATION)
        try:
            await context.bot.restrict_chat_member(chat_id, user_id, MUTED, until_date=until)
            await update.message.reply_text(
                f"{update.effective_user.mention_html()} muted for "
                f"{FLOOD_MUTE_DURATION // 60} min due to flooding.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Could not mute flooder: %s", e)


# ---------------------------------
# WORD FILTER CHECK
# ---------------------------------

async def word_filter_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    banned = data["word_filters"].get(chat_id, [])
    if not banned:
        return
    text_lower = update.message.text.lower()
    for word in banned:
        if word.lower() in text_lower:
            try:
                await update.message.delete()
            except Exception:
                pass
            await _apply_warn(
                update, context, data,
                chat_id,
                str(update.effective_user.id),
                update.effective_user.full_name,
                reason=f"banned word: {word}",
            )
            return


# ---------------------------------
# USERNAME / DISPLAY NAME FILTER
# ---------------------------------

def _banned_name_match(name: str, banned_words: list) -> str | None:
    """Return the first matching banned word found in name, or None."""
    name_lower = name.lower()
    for word in banned_words:
        if word.lower() in name_lower:
            return word
    return None


async def username_filter_check(
    chat_id_int: int,
    user,
    context: ContextTypes.DEFAULT_TYPE,
    notify_chat_id: int | None = None,
) -> bool:
    """
    Check user's display name and @username against the banned name words list.
    If matched, kick the user and send a notification.
    Returns True if the user was kicked.
    """
    data = load_data()
    chat_id = str(chat_id_int)
    banned = data.get("banned_name_words", {}).get(chat_id, [])
    if not banned:
        return False

    full_name = user.full_name or ""
    username = user.username or ""

    matched = _banned_name_match(full_name, banned) or _banned_name_match(username, banned)
    if not matched:
        return False

    try:
        await context.bot.ban_chat_member(chat_id_int, user.id)
        await context.bot.unban_chat_member(chat_id_int, user.id)
    except Exception as e:
        logger.warning("Could not kick user with banned name: %s", e)

    note_chat = notify_chat_id or chat_id_int
    try:
        await context.bot.send_message(
            note_chat,
            f"Kicked <b>{user.full_name}</b> "
            f"(<code>{user.id}</code>) - name contains banned word: <code>{matched}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("Could not send username-kick notice: %s", e)

    return True


# ---------------------------------
# USERNAME FILTER MANAGEMENT
# ---------------------------------

async def cmd_adduserword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    args = update.message.text.split()[1:]
    if not args:
        await update.message.reply_text("Usage: /adduserword <word>")
        return
    word = args[0].lower()
    data = load_data()
    chat_id = str(update.effective_chat.id)
    data.setdefault("banned_name_words", {})
    data["banned_name_words"].setdefault(chat_id, [])
    if word in data["banned_name_words"][chat_id]:
        await update.message.reply_text(f'"{word}" is already in the username filter.')
        return
    data["banned_name_words"][chat_id].append(word)
    save_data(data)
    await update.message.reply_text(f'Added "{word}" to the username filter.')


async def cmd_removeuserword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    args = update.message.text.split()[1:]
    if not args:
        await update.message.reply_text("Usage: /removeuserword <word>")
        return
    word = args[0].lower()
    data = load_data()
    chat_id = str(update.effective_chat.id)
    words = data.get("banned_name_words", {}).get(chat_id, [])
    if word not in words:
        await update.message.reply_text(f'"{word}" is not in the username filter.')
        return
    words.remove(word)
    save_data(data)
    await update.message.reply_text(f'Removed "{word}" from the username filter.')


async def cmd_listuserwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    words = data.get("banned_name_words", {}).get(chat_id, [])
    if not words:
        await update.message.reply_text("No banned username words in this chat.")
        return
    await update.message.reply_text(
        "Banned username words: " + ", ".join(f"<code>{w}</code>" for w in words),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------
# WELCOME MESSAGES
# ---------------------------------

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    chat_id = str(update.effective_chat.id)
    chat_id_int = update.effective_chat.id
    template = data["welcome"].get(
        chat_id,
        "Welcome to {chat_title}, {name}! Please read the /rules.",
    )
    for member in update.message.new_chat_members:
        # Kick immediately if their name/username is banned
        kicked = await username_filter_check(chat_id_int, member, context, notify_chat_id=chat_id_int)
        if kicked:
            continue
        text = template.format(
            name=member.mention_html(),
            chat_title=update.effective_chat.title or "the group",
            username=f"@{member.username}" if member.username else member.full_name,
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ---------------------------------
# WARN SYSTEM
# ---------------------------------

async def _apply_warn(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict,
    chat_id: str,
    user_id: str,
    display_name: str,
    reason: str = "no reason",
):
    data["warns"].setdefault(chat_id, {})
    data["warns"][chat_id][user_id] = data["warns"][chat_id].get(user_id, 0) + 1
    count = data["warns"][chat_id][user_id]
    save_data(data)
    if count >= WARN_KICK_THRESHOLD:
        data["warns"][chat_id][user_id] = 0
        save_data(data)
        try:
            await context.bot.ban_chat_member(int(chat_id), int(user_id))
            await context.bot.unban_chat_member(int(chat_id), int(user_id))
        except Exception as e:
            logger.warning("Could not kick warned user: %s", e)
        await update.effective_chat.send_message(
            f"<b>{display_name}</b> reached {WARN_KICK_THRESHOLD} warnings and has been kicked. Reason: {reason}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_chat.send_message(
            f"Warning {count}/{WARN_KICK_THRESHOLD} for <b>{display_name}</b>. Reason: {reason}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    reason_parts = update.message.text.split()[2:]
    reason = " ".join(reason_parts) if reason_parts else "no reason"
    data = load_data()
    await _apply_warn(
        update, context, data,
        str(update.effective_chat.id), str(user_id), display_name or str(user_id),
        reason=reason,
    )


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    current = data.get("warns", {}).get(chat_id, {}).get(str(user_id), 0)
    if current == 0:
        await update.message.reply_text(f"{display_name} has no warnings.")
        return
    data["warns"][chat_id][str(user_id)] = current - 1
    save_data(data)
    await update.message.reply_text(
        f"Removed one warning from <b>{display_name}</b>. Now at {current - 1}/{WARN_KICK_THRESHOLD}.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    warns = data.get("warns", {}).get(chat_id, {})
    active = {uid: cnt for uid, cnt in warns.items() if cnt > 0}
    if not active:
        await update.message.reply_text("No active warnings in this chat.")
        return
    lines = [f"<code>{uid}</code>: {cnt}/{WARN_KICK_THRESHOLD}" for uid, cnt in active.items()]
    await update.message.reply_text(
        "Active warnings:\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------
# KICK / BAN / UNBAN
# ---------------------------------

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
        await update.message.reply_text(f"<b>{display_name}</b> has been kicked.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Could not kick: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    reason_parts = update.message.text.split()[2:]
    reason = " ".join(reason_parts) if reason_parts else "no reason"
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat]ember(chat_id, user_id)
        await update.message.reply_text(
            f"<b>{display_name}</b> has been banned. Reason: {reason}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"Could not ban: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    args = update.message.text.split()[1:]
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    user_id = int(args[0])
    chat_id = update.effective_chat.id
    try:
        await context.bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        await update.message.reply_text(f"User <code>{user_id}</code> unbanned.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Could not unban: {e}")


# ---------------------------------
# MUTE / UNMUTE
# ---------------------------------

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    args = update.message.text.split()
    duration_min = None
    for arg in args[1:]:
        if arg.isdigit():
            duration_min = int(arg)
            break
    chat_id = update.effective_chat.id
    until = None
    if duration_min:
        until = datetime.now(tz=timezone.utc) + timedelta(minutes=duration_min)
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, MUTED, until_date=until)
        suffix = f"for {duration_min} min" if duration_min else "indefinitely"
        await update.message.reply_text(f"<b>{display_name}</b> muted {suffix}.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Could not mute: {e}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, UNMUTED)
        await update.message.reply_text(f"<b>{display_name}</b> unmuted.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"Could not unmute: {e}")


# ---------------------------------
# WORD FILTER MANAGEMENT
# ---------------------------------

async def cmd_addword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    args = update.message.text.split()[1:]
    if not args:
        await update.message.reply_text("Usage: /addword <word>")
        return
    word = args[0].lower()
    data = load_data()
    chat_id = str(update.effective_chat.id)
    data["word_filters"].setdefault(chat_id, [])
    if word in data["word_filters"][chat_id]:
        await update.message.reply_text(f'"{word}" is already in the filter.')
        return
    data["word_filters"][chat_id].append(word)
    save_data(data)
    await update.message.reply_text(f'Added "{word}" to the word filter.')


async def cmd_removeword(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    args = update.message.text.split()[1:]
    if not args:
        await update.message.reply_text("Usage: /removeword <word>")
        return
    word = args[0].lower()
    data = load_data()
    chat_id = str(update.effective_chat.id)
    words = data["word_filters"].get(chat_id, [])
    if word not in words:
        await update.message.reply_text(f'"{word}" is not in the filter.')
        return
    words.remove(word)
    save_data(data)
    await update.message.reply_text(f'Removed "{word}" from the word filter.')


async def cmd_listwords(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    words = data["word_filters"].get(chat_id, [])
    if not words:
        await update.message.reply_text("No banned words in this chat.")
        return
    await update.message.reply_text(
        "Banned words: " + ", ".join(f"<code>{w}</code>" for w in words),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------
# WELCOME MESSAGE CUSTOMIZATION
# ---------------------------------

async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text(
            "Usage: /setwelcome <message>\nPlaceholders: {name} {username} {chat_title}"
        )
        return
    data = load_data()
    data["welcome"][str(update.effective_chat.id)] = parts[1]
    save_data(data)
    await update.message.reply_text("Welcome message updated.")


async def cmd_getwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    data = load_data()
    msg = data["welcome"].get(
        str(update.effective_chat.id),
        "Welcome to {chat_title}, {name}! Please read the /rules.",
    )
    await update.message.reply_text(f"Current welcome message:\n\n{msg}")


# ---------------------------------
# RULES
# ---------------------------------

async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /setrules <rules text>")
        return
    data = load_data()
    data["rules"][str(update.effective_chat.id)] = parts[1]
    save_data(data)
    await update.message.reply_text("Rules updated.")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    rules = data["rules"].get(str(update.effective_chat.id))
    if not rules:
        await update.message.reply_text("No rules set yet. Use /setrules.")
        return
    await update.message.reply_text(f"<b>Group Rules:</b>\n\n{rules}", parse_mode=ParseMode.HTML)


# ---------------------------------
# HELP
# ---------------------------------

HELP_TEXT = (
    "<b>Admin Bot Commands</b>\n\n"
    "<b>Moderation (admin):</b>\n"
    "/warn [reason] - warn a user (reply)\n"
    "/unwarn - remove one warning (reply)\n"
    "/warns - list active warnings\n"
    "/kick - kick a user (reply)\n"
    "/ban [reason] - ban a user (reply)\n"
    "/unban [user_id] - unban a user\n"
    "/mute [minutes] - mute a user (reply)\n"
    "/unmute - unmute a user (reply)\n\n"
    "<b>Word Filter (admin):</b>\n"
    "/addword [word] - add banned word (messages)\n"
    "/removeword [word] - remove banned word\n"
    "/listwords - show banned words\n\n"
    "<b>Username Filter (admin):</b>\n"
    "/adduserword [word] - ban users whose name contains word\n"
    "/removeuserword [word] - remove from username filter\n"
    "/listuserwords - show username filter list\n\n"
    "<b>Welcome (admin):</b>\n"
    "/setwelcome [text] - set welcome message\n"
    "/getwelcome - see current welcome\n"
    "Placeholders: {name} {username} {chat_title}\n\n"
    "<b>Rules:</b>\n"
    "/setrules [text] - set group rules (admin)\n"
    "/rules - display group rules\n\n"
    "<b>Auto-moderation:</b>\n"
    f"Flood: &gt;{FLOOD_MAX_MESSAGES} msgs in {FLOOD_WINDOW}s -&gt; {FLOOD_MUTE_DURATION // 60}min mute\n"
    f"Warns: {WARN_KICK_THRESHOLD} warnings -&gt; auto-kick\n"
    "Word filter: banned words -&gt; delete + warn\n"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


# ---------------------------------
# MESSAGE HANDLER
# ---------------------------------

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user:
        kicked = await username_filter_check(
            update.effective_chat.id,
            update.effective_user,
            context,
            notify_chat_id=update.effective_chat.id,
        )
        if kicked:
            try:
                await update.message.delete()
            except Exception:
                pass
            return
    await flood_check(update, context)
    await word_filter_check(update, context)


# ---------------------------------
# MAIN
# ---------------------------------

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn))
    app.add_handler(CommandHandler("warns", cmd_warns))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("removeword", cmd_removeword))
    app.add_handler(CommandHandler("listwords", cmd_listwords))
    app.add_handler(CommandHandler("adduserword", cmd_adduserword))
    app.add_handler(CommandHandler("removeuserword", cmd_removeuserword))
    app.add_handler(CommandHandler("listuserwords", cmd_listuserwords))
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("getwelcome", cmd_getwelcome))
    app.add_handler(CommandHandler("setrules", cmd_setrules))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, on_message))
    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
