# haskhabot

Watches a channel for posts containing Discord timestamps (`<t:1760000000:F>`) and keeps a
sorted list of them in another channel: time · author · first lines · link. Events drop off the
list 4 hours after they start (configurable). Set up as many source → list channel pairs as you like, sharing sources and lists between them.

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
   `https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot%20applications.commands&permissions=85056`
   (View Channels, Send Messages, Embed Links, Read Message History, Add Reactions.)
   Already invited the bot? Open the new URL again to re-invite it: the `/listsignups` command and the
   reactions need the extra scope and permissions. The command registers when the bot starts and should show
   up right away; if it doesn't, reload Discord (Ctrl+R) and check the bot's log for "Couldn't register".
4. In Discord: **User Settings → Advanced → Developer Mode** on, then right-click each channel →
   **Copy Channel ID** for each source and list channel you'll put in `config.json`.

Use a dedicated list channel where only the bot can post: deny **Send Messages** for `@everyone`
and allow it for the bot.

## Configuration

Copy `.env.example` to `.env` and fill in the token; real environment variables override it. With
Docker, pass the same file via `--env-file` or a compose `env_file:`. Copy `config.example.json` to
`config.json` (git-ignored) and list your channel pairs under `mappings`:

```json
{
  "mappings": [
    {"source": 111111111111111111, "list": 222222222222222222},
    {"source": 111111111111111111, "list": 444444444444444444},
    {"source": 333333333333333333, "list": 444444444444444444}
  ],
  "reaction_channels": [111111111111111111]
}
```

Each pair sends one source channel's events to one list channel. A source can feed several lists and
a list can collect several sources: above, the first source's events appear in both lists, and the
second list combines both sources into one list. A channel can't be both a source and a list. IDs are
plain numbers (no quotes). Unknown keys are rejected, so a misspelled setting stops the bot with an error
instead of being ignored.

Each list channel works on its own: if one can't be reached (wrong ID, missing permissions in it or
in one of its sources), the bot logs it and keeps retrying while the other lists carry on. After
editing `config.json`, restart the bot.

The settings below apply to every pair. `.env` and relative paths, like the default `config.json`,
are looked up in the working directory: run the bot from the folder that holds them.

| Variable | |
|---|---|
| `DISCORD_TOKEN` | Bot token (required) |
| `CONFIG_FILE` | Path to the config file (default: `config.json`) |
| `PREVIEW_LINES` | Lines of each post shown in the list (default 3) |
| `HISTORY_LIMIT` | Messages to scan on startup (default: whole channel) |
| `KEEP_AFTER_START_HOURS` | How long an event stays listed after it starts (default 4) |

## Signups

When a post with a time (or "on fill") is sent in a source channel, the bot adds each emote in it, custom
or unicode, as a reaction, so people can click one to sign up for that role. When the post is edited,
only emotes the edit added get a reaction. Each emote is tried once per post, so an emote the bot can't
use (e.g. a custom one from another server) is skipped with one warning in the log, and a bot that keeps
editing its own post triggers nothing. Forwards never get reactions, and neither do posts that existed
before the bot started: the first edit the bot sees of such a post only records its emotes.

Reactions are added in the source channels unless `reaction_channels` in `config.json` says otherwise.
It is separate from `mappings`: list a channel there to react in it without listing its posts, or leave
a source out to list its posts without reacting (`"reaction_channels": []` turns reactions off). A list
channel can't be a reaction channel. `/listsignups` works in threads of both listed posts and reaction
channels' posts.

This needs the **Add Reactions** permission in each reaction channel. Without it the bot logs one warning,
doesn't react, and keeps the list and `/listsignups` working; it notices within a minute when the
permission is granted.

In the thread started from an event post, `/listsignups` replies with one line per signup emote: the
count and who reacted. Only the bot's own emotes are listed, and the bot itself isn't counted. Anywhere
else it tells you to run it in an event's thread.

## Run

With Python 3.9+:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin/pip on Linux
.venv/Scripts/python -m haskhabot
```

With Docker (the image holds only the code; mount the config file so it can change without a rebuild):

```bash
docker build -t haskhabot .
docker run -d --name haskhabot --env-file .env -v ./config.json:/app/config.json:ro --restart unless-stopped haskhabot
```

Create `config.json` before starting: if it's missing, Docker mounts an empty directory in its
place and the bot exits with "Can't read /app/config.json". With Compose, the same mount is
`volumes: ["./config.json:/app/config.json:ro"]`.

## Tests

```bash
.venv/Scripts/pip install -r requirements-dev.txt   # Windows; .venv/bin/pip on Linux
.venv/Scripts/python -m pytest
```

## Code layout

Everything is in the `haskhabot/` package; run it with `python -m haskhabot`.

- `bot.py`: the Discord client, event routing and `/listsignups`.
- `settings.py`: `config.json` and `.env` loading.
- `events.py`: reading a post into an entry, and rendering the list.
- `mapping.py`: one list channel and its sources: scanning, syncing and permission checks.
- `signups.py`: signup emotes, reacting, and the signup tally.
