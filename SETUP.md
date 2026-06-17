# Telegram Group Admin Bot — Setup Guide

## 1. Create your bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the **API token** you receive

## 2. Install dependencies

```bash
pip install -r requirements.txt
```

## 3. Configure the token

Set it as an environment variable (recommended):

```bash
export BOT_TOKEN="123456789:ABC-your-token-here"
```

Or edit `bot.py` directly and replace `"YOUR_BOT_TOKEN_HERE"` with your token.

## 4. Add the bot to your group

1. Add the bot as a member of your Telegram group
2. **Promote it to admin** and grant these permissions:
   - Delete messages
   - Ban users
   - Restrict members

> Without admin rights the bot cannot mute, kick, or delete messages.

## 5. Run the bot

```bash
python bot.py
```

The bot will start polling for updates. Keep this terminal open (or run it as a service).

---

## Available Commands

### Moderation (admins only)
| Command | Description |
|---------|-------------|
| `/warn [reason]` | Warn a user (reply to their message) |
| `/unwarn` | Remove one warning (reply) |
| `/warns` | List all active warnings in this chat |
| `/kick` | Kick a user — they can rejoin (reply) |
| `/ban [reason]` | Permanently ban a user (reply) |
| `/unban <user_id>` | Unban a user |
| `/mute [minutes]` | Mute a user (reply); omit minutes for indefinite |
| `/unmute` | Restore a user's ability to send messages (reply) |

### Word Filter (admins only)
| Command | Description |
|---------|-------------|
| `/addword <word>` | Add a word to the filter |
| `/removeword <word>` | Remove a word from the filter |
| `/listwords` | Show all banned words |

### Welcome & Rules (admins only)
| Command | Description |
|---------|-------------|
| `/setwelcome <text>` | Set welcome message (placeholders: `{name}` `{username}` `{chat_title}`) |
| `/getwelcome` | Preview the current welcome message |
| `/setrules <text>` | Set group rules |

### Public
| Command | Description |
|---------|-------------|
| `/rules` | Display the group rules |
| `/help` | Show this command list |

---

## Auto-moderation Defaults

These are set near the top of `bot.py` and can be changed:

| Setting | Default | Variable |
|---------|---------|----------|
| Flood messages | 5 msgs / 5 sec | `FLOOD_MAX_MESSAGES` / `FLOOD_WINDOW` |
| Flood mute duration | 5 minutes | `FLOOD_MUTE_DURATION` |
| Warn-to-kick threshold | 3 warnings | `WARN_KICK_THRESHOLD` |

---

## Running as a Background Service (Linux)

Create `/etc/systemd/system/tgbot.service`:

```ini
[Unit]
Description=Telegram Admin Bot
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/bot.py
Environment="BOT_TOKEN=your_token_here"
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl enable tgbot
sudo systemctl start tgbot
```
