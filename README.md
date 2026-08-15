# amc-scraper

Discord bot that posts daily showtimes for **AMC Fresh Meadows 7** and **AMC Bay Terrace 6**, and answers `/showtimes` on demand.

## Data source

AMC publishes an official REST API at `https://api.amctheatres.com`, but it requires a vendor key from [developers.amctheatres.com](https://developers.amctheatres.com/). The public website (`amctheatres.com`) sits behind Queue-it / Cloudflare, so a plain HTTP scrape is blocked.

This project therefore:

1. Uses the **official AMC API** when `AMC_VENDOR_KEY` is set.
2. Otherwise reads Fandango's public theater JSON for those same AMC locations (HTTP, with a normal browser `Referer`).
3. Falls back to a Playwright scrape of the AMC showtimes page if Fandango is unavailable.

Listings always link back to the AMC theater showtimes page.

## Setup

Python 3.11+ is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python3 -m playwright install chromium
cp .env.example .env
```

This Mac does not have a `python` command; use `python3` or the venv’s `.venv/bin/python`. After `source .venv/bin/activate`, `python` works inside that shell.

### Discord bot

1. Create an application at the [Discord Developer Portal](https://discord.com/developers/applications).
2. Add a bot and copy the token into `DISCORD_TOKEN`.
3. Invite the bot with scopes `bot` and `applications.commands`, and permissions **Send Messages** + **Embed Links**.
4. Enable Developer Mode in Discord, right-click the target channel, copy the ID into `DISCORD_CHANNEL_ID`.
5. Optional: copy your server ID into `DISCORD_GUILD_ID` so `/showtimes` syncs immediately while developing.

`.env`:

```
DISCORD_TOKEN=...
DISCORD_CHANNEL_ID=...
DISCORD_GUILD_ID=...
POST_TIME=09:00
TIMEZONE=America/New_York
```

## Run

Print today's remaining showtimes in the terminal (no Discord token needed):

```bash
./showtimes
./showtimes --theater fresh-meadows --date 2026-08-16 --all-times
```

Or with the venv activated:

```bash
source .venv/bin/activate
python -m amc_scraper
```

Start the bot (slash command + daily 9:00 AM Eastern post, configurable via `POST_TIME`):

```bash
./bot
```

Keep that process running. Slash commands need a connected gateway, and the daily post is scheduled inside the same process.

### `/showtimes`

| Option | Meaning |
| --- | --- |
| `theater` | Fresh Meadows 7, Bay Terrace 6, or both (default) |
| `date` | `YYYY-MM-DD` (defaults to today in `America/New_York`) |

The daily post always includes both theaters.
