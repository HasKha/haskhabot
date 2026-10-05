# haskhabot

Watches a channel for posts containing Discord timestamps (`<t:1760000000:F>`) and keeps a
sorted list of them in another channel: time · author · first lines · link. Events drop off the
list 4 hours after they start (configurable).

The bot keeps no database. On startup it rescans the source channel, then follows new, edited and
deleted posts, editing its own list messages in place.

## What gets listed

- **Timestamps** anywhere in a post: its text, embeds, or layout components (the newer message
  format some bots use). A post with several timestamps (e.g. start and end) is listed under the
  earliest.
- **"on fill"** (also "on-fill", "onfill", any case): the event starts as soon as enough people
  join. It's listed at the time it was posted and shown as *On fill*. If the post also has an
  earlier timestamp, that wins.
- **Forwards**: a post forwarded from another server is read from Discord's copy of the original,
  so the bot doesn't need to be in that server. The entry links to the original and shows
  *fwd by* the member who forwarded it. The copy is frozen: later edits to the original aren't seen.

Each entry starts with a random custom emoji from the server, which stays the same for that event.
Plain links to messages in other servers can't be read: the bot only sees servers it's in.

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

## Tests

```bash
.venv/Scripts/pip install -r requirements-dev.txt   # Windows; .venv/bin/pip on Linux
.venv/Scripts/python -m pytest
```
