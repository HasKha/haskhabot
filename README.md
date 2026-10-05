# haskhabot

Watches a channel for posts containing Discord timestamps (`<t:1760000000:F>`) and keeps a
sorted list of them in another channel: time · author · first lines · link. Events drop off the
list 4 hours after they start (configurable). Set up as many source → list channel pairs as you like.

The bot keeps no database. On startup it rescans each source channel, then follows new, edited and
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
   `https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot%20applications.commands&permissions=347200`
   (View Channels, Send Messages, Embed Links, Read Message History, Add Reactions, Use External Emojis.)
   Already invited the bot? Open the new URL again to re-invite it: the `/listsignups` command and the
   reactions need the extra scope and permissions. The command can take up to an hour to appear the first time.
4. In Discord: **User Settings → Advanced → Developer Mode** on, then right-click each channel →
   **Copy Channel ID** for each source and list channel you'll put in `mappings.json`.

Use a dedicated list channel where only the bot can post: deny **Send Messages** for `@everyone`
and allow it for the bot.

## Configuration

Copy `.env.example` to `.env` and fill in the token. With Docker, pass the same file via
`--env-file` or a compose `env_file:`. Copy `mappings.example.json` to `mappings.json`
(git-ignored) and list your channel pairs:

```json
[
  {"source": 111111111111111111, "list": 222222222222222222},
  {"source": 333333333333333333, "list": 444444444444444444}
]
```

Each pair watches one source channel and keeps its list in one list channel. IDs are plain numbers
(no quotes), and every channel may appear only once in the file, as either a source or a list.
If one pair can't be reached (wrong ID, missing permissions), the bot logs it and keeps retrying
while the other pairs carry on. After editing `mappings.json`, restart the bot.

The settings below apply to every pair.

| Variable | |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `MAPPINGS_FILE` | Path to the mappings file (default: `mappings.json` next to `bot.py`) |
| `SOURCE_CHANNEL_ID`, `LIST_CHANNEL_ID` | A single pair, used only if there is no `mappings.json` |
| `PREVIEW_LINES` | Lines of each post shown in the list (default 3) |
| `HISTORY_LIMIT` | Messages to scan on startup (default: whole channel) |
| `KEEP_AFTER_START_HOURS` | How long an event stays listed after it starts (default 4) |

## Signups

For every post in a source channel that has a time (or "on fill") and isn't a forward, the bot adds each
emote in the post, custom or unicode, as a reaction, so people can click one to sign up for that role.
Posts made while the bot was offline get theirs on startup, as long as they're still listed. Edits add
newly written emotes and never remove existing reactions.

In the thread started from an event post, `/listsignups` replies with one line per signup emote: the
count and who reacted. Only the bot's own emotes are listed, and the bot itself isn't counted. Anywhere
else it tells you to run it in an event's thread.

## Run

With Python 3.9+:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on Linux
.venv/Scripts/python bot.py
```

With Docker (the image holds only the code; mount the mappings file so it can change without a rebuild):

```bash
docker build -t haskhabot .
docker run -d --name haskhabot --env-file .env -v ./mappings.json:/app/mappings.json:ro --restart unless-stopped haskhabot
```

Create `mappings.json` before starting: if it's missing, Docker mounts an empty directory in its
place and the bot exits with "Can't read /app/mappings.json". With Compose, the same mount is
`volumes: ["./mappings.json:/app/mappings.json:ro"]`. Leave the mount out to use
`SOURCE_CHANNEL_ID` / `LIST_CHANNEL_ID` from `.env` instead.

## Tests

```bash
.venv/Scripts/pip install -r requirements-dev.txt   # Windows; .venv/bin/pip on Linux
.venv/Scripts/python -m pytest
```
