# haskhabot

Watches a channel for posts containing Discord timestamps (`<t:1760000000:F>`) and keeps a
sorted list of them in another channel: time · author · first lines · link. Events drop off the
list 4 hours after they start (configurable).

The bot keeps no database. On startup it rescans the source channel, then follows new, edited and
deleted posts, editing its own list messages in place.

## Create the bot in Discord

1. Go to <https://discord.com/developers/applications> → **New Application**.
2. **Bot** tab:
   - **Reset Token** → copy it (shown once) into `.env` as `DISCORD_TOKEN`.
   - Enable **Message Content Intent** (required to read the posts).
   - Optionally turn off **Public Bot** so only you can invite it.
3. **OAuth2** tab → copy the **Client ID**, then open this URL (replace `CLIENT_ID`) and pick your server:
   `https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot&permissions=84992`
   (View Channels, Send Messages, Embed Links, Read Message History.)
4. In Discord: **User Settings → Advanced → Developer Mode** on, then right-click each channel →
   **Copy Channel ID** for `SOURCE_CHANNEL_ID` and `LIST_CHANNEL_ID`.

Use a dedicated list channel where only the bot can post: deny **Send Messages** for `@everyone`
and allow it for the bot.

## Configuration

Copy `.env.example` to `.env` and fill it in. With Docker, pass the same file via `--env-file`
or a compose `env_file:`.

| Variable | |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `SOURCE_CHANNEL_ID` | Channel to watch (required) |
| `LIST_CHANNEL_ID` | Channel the list is kept in (required) |
| `PREVIEW_LINES` | Lines of each post shown in the list (default 3) |
| `HISTORY_LIMIT` | Messages to scan on startup (default: whole channel) |
| `KEEP_AFTER_START_HOURS` | How long an event stays listed after it starts (default 4) |

## Run

With Python 3.9+:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on Linux
.venv/Scripts/python bot.py
```

With Docker:

```bash
docker build -t haskhabot .
docker run -d --name haskhabot --env-file .env --restart unless-stopped haskhabot
```
