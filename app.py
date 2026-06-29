"""
Telegram Group Admin Bot
========================
Features:
  - Anti-spam / flood protection (auto-mute)
  - Welcome messages (customizable per chat)
  - Warn / kick / ban / unban system (auto-kick at warn threshold)
  - Word filter (auto-delete + warn)
  - Mute / unmute
  - Rules system (/setrules, /rules)
  - /addword / /removeword
  - /help for admins

Requirements: python-telegram-bot>=20.0, python-dotenv
Run: python app.py
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

from telegram import (
    ChatPermissions,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ââââââââââââââââââââââââââââââââââââââââââââââ
# CONFIGURATION â edit these before running
# ââââââââââââââââââââââââââââââââââââââââââââââ

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Flood settings: more than FLOOD_MAX_MESSAGES in FLOOD_WINDOW seconds â mute
FLOOD_MAX_MESSAGES: int = 5
FLOOD_WINDOW: int = 5           # seconds
FLOOD_MUTE_DURATION: int = 300  # seconds (5 min)

# Number of warnings before a user is automatically kicked
WARN_KICK_THRESHOLD: int = 3

# Persistent data is saved here
DATA_FILE = Path("bot_data.json")

# ââââââââââââââââââââââââââââââââââââââââââââââ
# LOGGING
# ââââââââââââââââââââââââââââââââââââââââââââââ

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ââââââââââââââââââââââââââââââââââââââââââââââ
# PERSISTENT DATA AELPEPS
# ââââââââââââââââââââââââââââââââââââââââââââââ

def _default_data() -> dict:
    return {
        "warns": {},       # {chat_id: {user_id: count}}
        "word_filters": {},  # {chat_id: [word, ...]}
        "welcome": {},     # {chat_id: message_text}
        "rules": {},       # {chat_id: rules_text}
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


# In-memory flood tracker: {chat_id: {user_id: [timestamp, ...]}}
flood_tracker: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))

# ââââââââââââââââââââââââââââââââââââââââââââââ
# PERMISSION HELPERS
# ââââââââââââââââââââââââââââââââââââââââââââââ

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
    """Return True if the *sender* of the command is a chat admin."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    admins = await context.bot.get_chat_administrators(chat_id)
    return any(a.user.id == user_id for a in admins)


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Send an error reply if the sender is not an admin; return True if they are."""
    if not await is_admin(update, context):
        await update.message.reply_text("â Admin only.")
        return False
    return True


def get_target_user(update: Update):
    """Return (user_id, display_name) from a reply or first mention in command args."""
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


# ââââââââââââââââââââââââââââââââââââââââââââââ
# ANTI-SPAM / FLOOD PROTECTION
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def flood_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check message rate; mute users who exceed the limit."""
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
                f"ð« {update.effective_user.mention_html()} has been muted for "
                f"{FLOOD_MUTE_DURATION // 60} min due to flooding.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Could not mute flooder: %s", e)


# ââââââââââââââââââââââââââââââââââââââââââââââ
# WORD FILTER
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def word_filter_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete messages containing banned words and warn the sender."""
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
                reason=f'banned word "{lword}"',
            )
            return


# ââââââââââââââââââââââââââââââââââââââââââââââ
# WELCOME MESSAGES
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat_id = str(update.effective_chat.id)
    template = data["welcome"].get(
        chat_id,
        "ð Welcome to {chat_title}, {name}! Please read the /rules.",
    )
    for member in update.message.new_chat_members:
        text = template.format(
            name=member.mention_html(),
            chat_title=update.effective_chat.title or "the group",
            username=f"@{member.username}" if member.username else member.full_name,
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ââââââââââââââââââââââââââââââââââââââââââââââ
# WARN SYSTEM
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def _apply_warn(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    data: dict,
    chat_id: str,
    user_id: str,
    display_name: str,
    reason: str = "no reason given",
) -> None:
    """Increment warn count and kick if threshold reached."""
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
            f"â ï¸ <b>{display_name}</b> reached {WARN_KICK_THRESHOLD} warnings and has been kicked.\n"
            f"Reason: {reason}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_chat.send_message(
            f"â ï¸ Warning {count}/{WARN_KICK_THRESHOLD} for <b>{display_name}</b>.\n"
            f"Reason: {reason}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    reason_parts = update.message.text.split()[2:]
    reason = " ".join(reason_parts) if reason_parts else "no reason given"
    data = load_data()
    await _apply_warn(
        update, context, data,
        str(update.effective_chat.id), str(user_id), display_name or str(user_id),
        reason=reason,
    )


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        f"â Removed one warning from <b>{display_name}</b>. "
        f"Now at {current - 1}/{WARN_KICK_THRESHOLD}.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    warns = data.get("warns", {}).get(chat_id, {})
    active = {uid: cnt for uid, cnt in warns.items() if cnt > 0}
    if not active:
        await update.message.reply_text("No active warnings in this chat.")
        return
    lines = [f"â¢ <code>{uid}</code>: {cnt}/{WARN_KICK_THRESHOLD}" for uid, cnt in active.items()]
    await update.message.reply_text(
        "â ï¸ <b>Active warnings:</b>\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ââââââââââââââââââââââââââââââââââââââââââââââ
# KICK / BAN / UNBAN
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text(f"ð¢ <b>{display_name}</b> has been kicked.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"â Could not kick: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    reason_parts = update.message.text.split()[2:]
    reason = " ".join(reason_parts) if reason_parts else "no reason given"
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await update.message.reply_text(
            f"ð¨ <b>{display_name}</b> has been banned.\nReason: {reason}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"â Could not ban: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text(f"â User <code>{user_id}</code> has been unbanned.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"â Could not unban: {e}")


# ââââââââââââââââââââââââââââââââââââââââââââââ
# MUTE / UNMUTE
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text(
            f"ð <b>{display_name}</b> has been muted {suffix}.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"â Could not mute: {e}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    chat_id = update.effective_chat.id
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, UNMUTED)
        await update.message.reply_text(
            f"ð <b>{display_name}</b> has been unmuted.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"â Could not unmute: {e}")


# ââââââââââââââââââââââââââââââââââââââââââââââ
# WORD FILTER MANAGEMENT
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def cmd_addword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text(f'"{·ord}" is already in the filter.')
        return
    data["word_filters"][chat_id].append(word)
    save_data(data)
    await update.message.reply_text(f'â Added "{word}" to the word filter.')


async def cmd_removeword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await update.message.reply_text(f'"{lower(word)}" is not in the filter.')
        return
    words.remove(word)
    save_data(data)
    await update.message.reply_text(f'â Removed "{word}" from the word filter.')


async def cmd_listwords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    words = data["word_filters"].get(chat_id, [])
    if not words:
        await update.message.reply_text("No banned words in this chat.")
        return
    await update.message.reply_text(
        "ð« Banned words: " + ", ".join(f'<code>{w}</code>' for w in words),
        parse_mode=ParseMode.HTML,
    )


# ââââââââââââââââââââââââââââââââââââââââââââââ
# WELCOME MESSAGE CUSTOMIZATION
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text("â Welcome message updated.")


async def cmd_getwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    data = load_data()
    msg = data["welcome"].get(
        str(update.effective_chat.id),
        "ð Welcome to {chat_title}, {name}! Please read the /rules.",
    )
    await update.message.reply_text(f"Current welcome message:\n\n{msg}")


# ââââââââââââââââââââââââââââââââââââââââââââââ
# RULES
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /setrules <rules text>")
        return
    data = load_data()
    data["rules"][str(update.effective_chat.id)] = parts[1]
    save_data(data)
    await update.message.reply_text("â Rules updated.")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    rules = data["rules"].get(str(update.effective_chat.id))
    if not rules:
        await update.message.reply_text("No rules have been set yet. Admins can use /setrules.")
        return
    await update.message.reply_text(f"ð <b>Group Rules:</b>\n\n{rules}", parse_mode=ParseMode.HTML)


# ââââââââââââââââââââââââââââââââââââââââââââââ
# HELP
# ââââââââââââââââââââââââââââââââââââââââââââââ

# Note: {{name}} etc. are escaped so .format() doesn't consume them
HELP_TEXT = (
    "<b>ð¤ Admin Bot Commands</b>\n\n"
    "<b>Moderation (admin only):</b>\n"
    "/warn [reason] â warn a user (reply to their message)\n"
    "/unwarn â remove one warning (reply)\n"
    "/warns â list all active warnings\n"
    "/kick â kick a user (reply)\n"
    "/ban [reason] â permanently ban a user (reply)\n"
    "/unban &lt;user_id&gt; â unban a user\n"
    "/mute [minutes] â a user (reply)\n"
    "/unmute â unmute a user (reply)\n\n"
    "<b>Word Filter (admin only):</b>\n"
    "/addword &lt;word&gt; â add banned word\n"
    "/removeword &lt;word&gt; â remove banned word\n"
    "/listwords â show all banned words\n\n"
    "<b>Welcome (admin only):</b>\n"
    "/setwelcome &lt;text&gt; â set welcome message\n"
    "/getwelcome â see current welcome message\n"
    "Placeholders: {{name}} {{username}} {{chat_title}}\n\n"
    "<b>Rules:</b>\n"
    "/setrules &lt;text&gt; â set group rules (admin only)\n"
    "/rules â display group rules\n\n"
    "<b>Auto-moderation:</b>\n"
    "â¢ Flood protection: &gt;{flood_max} messages in {flood_win}s â {mute_min}-min mute\n"
    "â¢ Warns: {warn_thresh} warnings â auto-kick\n"
    "â¢ Word filter: banned words â delete + warn\n"
).format(
    flood_max=FLOOD_MAX_MESSAGES,
    flood_win=FLOOD_WINDOW,
    mute_min=FLOOD_MUTE_DURATION // 60,
    warn_thresh=WARN_KICK_THRESHOLD,
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


# ââââââââââââââââââââââââââââââââââââââââââââââ
# MESSAGE HANDLER (combines flood + word filter)
# ââââââââââââââââââââââââââââââââââââââââââââââ

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await flood_check(update, context)
    await word_filter_check(update, context)


# ââââââââââââââââââââââââââââââââââââââââââââââ
# MAIN
# ââââââââââââââââââââââââââââââââââââââââââââââ

def main() -> None:
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
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("getwelcome", cmd_getwelcome))
    app.add_handler(CommandHandler("setrules", cmd_setrules))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, on_message)
    )

    logger.info("Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
