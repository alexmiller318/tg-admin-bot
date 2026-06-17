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

Requirements: python-telegram-bot>=20.0
Run: python bot.py
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

# ──────────────────────────────────────────────
# CONFIGURATION — edit these before running
# ──────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Flood settings: more than FLOOD_MAX_MESSAGES in FLOOD_WINDOW seconds → mute
FLOOD_MAX_MESSAGES: int = 5
FLOOD_WINDOW: int = 5          # seconds
FLOOD_MUTE_DURATION: int = 300  # seconds (5 min)

# Number of warnings before a user is automatically kicked
WARN_KICK_THRESHOLD: int = 3

# Persistent data is saved here
DATA_FILE = Path("bot_data.json")

# ──────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# PERSISTENT DATA HELPERS
# ──────────────────────────────────────────────

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

# ──────────────────────────────────────────────
# PERMISSION HELPERS
# ──────────────────────────────────────────────

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
        await update.message.reply_text("⛔ Admin only.")
        return False
    return True


def get_target_user(update: Update):
    """Return (user_id, display_name) from a reply or first mention in command args."""
    msg = update.message
    if msg.reply_to_message:
        u = msg.reply_to_message.from_user
        return u.id, u.full_name
    # Try @mention or numeric id in args
    args = msg.text.split()[1:]
    if args:
        arg = args[0].lstrip("@")
        if arg.isdigit():
            return int(arg), arg
        # We can't resolve a username to an id without a DB; tell the user to reply instead
    return None, None


# ──────────────────────────────────────────────
# ANTI-SPAM / FLOOD PROTECTION
# ──────────────────────────────────────────────

async def flood_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check message rate; mute users who exceed the limit."""
    if not update.message or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    now = time.monotonic()

    # Skip admins
    admins = await context.bot.get_chat_administrators(chat_id)
    if any(a.user.id == user_id for a in admins):
        return

    tracker = flood_tracker[chat_id][user_id]
    tracker.append(now)
    # Keep only timestamps within the window
    flood_tracker[chat_id][user_id] = [t for t in tracker if now - t <= FLOOD_WINDOW]

    if len(flood_tracker[chat_id][user_id]) > FLOOD_MAX_MESSAGES:
        flood_tracker[chat_id][user_id] = []  # reset
        until = datetime.now(tz=timezone.utc) + timedelta(seconds=FLOOD_MUTE_DURATION)
        try:
            await context.bot.restrict_chat_member(chat_id, user_id, MUTED, until_date=until)
            await update.message.reply_text(
                f"🚫 {update.effective_user.mention_html()} has been muted for "
                f"{FLOOD_MUTE_DURATION // 60} min due to flooding.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Could not mute flooder: %s", e)


# ──────────────────────────────────────────────
# WORD FILTER
# ──────────────────────────────────────────────

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
            # Apply a warn
            await _apply_warn(
                update, context, data,
                chat_id,
                str(update.effective_user.id),
                update.effective_user.full_name,
                reason=f'banned word "{word}"',
            )
            return


# ──────────────────────────────────────────────
# WELCOME MESSAGES
# ──────────────────────────────────────────────

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat_id = str(update.effective_chat.id)
    template = data["welcome"].get(
        chat_id,
        "👋 Welcome to {chat_title}, {name}! Please read the /rules.",
    )
    for member in update.message.new_chat_members:
        text = template.format(
            name=member.mention_html(),
            chat_title=update.effective_chat.title or "the group",
            username=f"@{member.username}" if member.username else member.full_name,
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ──────────────────────────────────────────────
# WARN SYSTEM (internal helper + commands)
# ──────────────────────────────────────────────

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
            await context.bot.unban_chat_member(int(chat_id), int(user_id))  # kick (not perma-ban)
        except Exception as e:
            logger.warning("Could not kick warned user: %s", e)
        await update.effective_chat.send_message(
            f"⚠️ <b>{display_name}</b> reached {WARN_KICK_THRESHOLD} warnings and has been kicked.\n"
            f"Reason: {reason}",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.effective_chat.send_message(
            f"⚠️ Warning {count}/{WARN_KICK_THRESHOLD} for <b>{display_name}</b>.\n"
            f"Reason: {reason}",
            parse_mode=ParseMode.HTML,
        )


async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/warn — warn a user (reply to their message or pass user_id)."""
    if not await require_admin(update, context):
        return
    user_id, display_name = get_target_user(update)
    if not user_id:
        await update.message.reply_text("Reply to a message or pass a user ID.")
        return
    reason_parts = update.message.text.split()[2:]  # /warn @user <reason>
    reason = " ".join(reason_parts) if reason_parts else "no reason given"
    data = load_data()
    await _apply_warn(
        update, context, data,
        str(update.effective_chat.id), str(user_id), display_name or str(user_id),
        reason=reason,
    )


async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unwarn — remove one warning from a user."""
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
        f"✅ Removed one warning from <b>{display_name}</b>. "
        f"Now at {current - 1}/{WARN_KICK_THRESHOLD}.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_warns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/warns — show all warned users in this chat."""
    if not await require_admin(update, context):
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    warns = data.get("warns", {}).get(chat_id, {})
    active = {uid: cnt for uid, cnt in warns.items() if cnt > 0}
    if not active:
        await update.message.reply_text("No active warnings in this chat.")
        return
    lines = [f"• <code>{uid}</code>: {cnt}/{WARN_KICK_THRESHOLD}" for uid, cnt in active.items()]
    await update.message.reply_text(
        "⚠️ <b>Active warnings:</b>\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ──────────────────────────────────────────────
# KICK / BAN / UNBAN
# ──────────────────────────────────────────────

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/kick — remove a user from the group (they can rejoin)."""
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
        await update.message.reply_text(f"👢 <b>{display_name}</b> has been kicked.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Could not kick: {e}")


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ban — permanently ban a user."""
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
            f"🔨 <b>{display_name}</b> has been banned.\nReason: {reason}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not ban: {e}")


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unban <user_id> — lift a ban."""
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
        await update.message.reply_text(f"✅ User <code>{user_id}</code> has been unbanned.", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Could not unban: {e}")


# ──────────────────────────────────────────────
# MUTE / UNMUTE
# ──────────────────────────────────────────────

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mute [minutes] — mute a user (default: indefinite)."""
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
            f"🔇 <b>{display_name}</b> has been muted {suffix}.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not mute: {e}")


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/unmute — restore a user's ability to send messages."""
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
            f"🔊 <b>{display_name}</b> has been unmuted.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not unmute: {e}")


# ──────────────────────────────────────────────
# WORD FILTER MANAGEMENT
# ──────────────────────────────────────────────

async def cmd_addword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addword <word> — add a word to the filter."""
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
    await update.message.reply_text(f'✅ Added "{word}" to the word filter.')


async def cmd_removeword(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/removeword <word> — remove a word from the filter."""
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
    await update.message.reply_text(f'✅ Removed "{word}" from the word filter.')


async def cmd_listwords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/listwords — show all filtered words."""
    if not await require_admin(update, context):
        return
    data = load_data()
    chat_id = str(update.effective_chat.id)
    words = data["word_filters"].get(chat_id, [])
    if not words:
        await update.message.reply_text("No banned words in this chat.")
        return
    await update.message.reply_text("🚫 Banned words: " + ", ".join(f'<code>{w}</code>' for w in words), parse_mode=ParseMode.HTML)


# ──────────────────────────────────────────────
# WELCOME MESSAGE CUSTOMIZATION
# ──────────────────────────────────────────────

async def cmd_setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setwelcome <text> — set a custom welcome message.
    Use {name}, {username}, {chat_title} as placeholders.
    """
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
    await update.message.reply_text("✅ Welcome message updated.")


async def cmd_getwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/getwelcome — show the current welcome message template."""
    if not await require_admin(update, context):
        return
    data = load_data()
    msg = data["welcome"].get(
        str(update.effective_chat.id),
        "👋 Welcome to {chat_title}, {name}! Please read the /rules.",
    )
    await update.message.reply_text(f"Current welcome message:\n\n{msg}")


# ──────────────────────────────────────────────
# RULES
# ──────────────────────────────────────────────

async def cmd_setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setrules <text> — set group rules."""
    if not await require_admin(update, context):
        return
    parts = update.message.text.split(maxsplit=1)
    if len(parts) < 2:
        await update.message.reply_text("Usage: /setrules <rules text>")
        return
    data = load_data()
    data["rules"][str(update.effective_chat.id)] = parts[1]
    save_data(data)
    await update.message.reply_text("✅ Rules updated.")


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rules — display the group rules."""
    data = load_data()
    rules = data["rules"].get(str(update.effective_chat.id))
    if not rules:
        await update.message.reply_text("No rules have been set yet. Admins can use /setrules.")
        return
    await update.message.reply_text(f"📋 <b>Group Rules:</b>\n\n{rules}", parse_mode=ParseMode.HTML)


# ──────────────────────────────────────────────
# HELP
# ──────────────────────────────────────────────

HELP_TEXT = """
<b>🤖 Admin Bot Commands</b>

<b>Moderation (admin only):</b>
/warn [reason] — warn a user (reply to their message)
/unwarn — remove one warning (reply)
/warns — list all active warnings
/kick — kick a user (reply)
/ban [reason] — permanently ban a user (reply)
/unban &lt;user_id&gt; — unban a user
/mute [minutes] — mute a user (reply)
/unmute — unmute a user (reply)

<b>Word Filter (admin only):</b>
/addword &lt;word&gt; — add banned word
/removeword &lt;word&gt; — remove banned word
/listwords — show all banned words

<b>Welcome (admin only):</b>
/setwelcome &lt;text&gt; — set welcome message
/getwelcome — see current welcome message
Placeholders: {name} {username} {chat_title}

<b>Rules:</b>
/setrules &lt;text&gt; — set group rules (admin only)
/rules — display group rules

<b>Auto-moderation:</b>
• Flood protection: >{flood_max} messages in {flood_win}s → {mute_min}-min mute
• Warns: {warn_thresh} warnings → auto-kick
• Word filter: banned words → delete + warn
""".format(
    flood_max=FLOOD_MAX_MESSAGES,
    flood_win=FLOOD_WINDOW,
    mute_min=FLOOD_MUTE_DURATION // 60,
    warn_thresh=WARN_KICK_THRESHOLD,
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


# ──────────────────────────────────────────────
# MESSAGE HANDLER (combines flood + word filter)
# ──────────────────────────────────────────────

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called for every non-command group message."""
    await flood_check(update, context)
    await word_filter_check(update, context)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    # Moderation commands
    app.add_handler(CommandHandler("warn", cmd_warn))
    app.add_handler(CommandHandler("unwarn", cmd_unwarn))
    app.add_handler(CommandHandler("warns", cmd_warns))
    app.add_handler(CommandHandler("kick", cmd_kick))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))

    # Word filter
    app.add_handler(CommandHandler("addword", cmd_addword))
    app.add_handler(CommandHandler("removeword", cmd_removeword))
    app.add_handler(CommandHandler("listwords", cmd_listwords))

    # Welcome
    app.add_handler(CommandHandler("setwelcome", cmd_setwelcome))
    app.add_handler(CommandHandler("getwelcome", cmd_getwelcome))

    # Rules
    app.add_handler(CommandHandler("setrules", cmd_setrules))
    app.add_handler(CommandHandler("rules", cmd_rules))

    # Help
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))

    # New members → welcome
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )

    # All other messages → flood + word filter
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, on_message)
    )

    logger.info("Bot started. Press Ctrl+C to stop.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
