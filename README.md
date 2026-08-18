# amc-scraper

Discord bot that posts daily showtimes for **AMC Fresh Meadows 7**, **AMC Bay Terrace 6**, and **AMC Lincoln Square 13**, and answers `/showtimes` and `/coming` on demand. It also watches Lincoln Square about once a minute and pings Discord when any new showtime appears.

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
./showtimes --coming
./showtimes --coming --through 2026-12-31
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
| `theater` | One configured theater, or all (default: all of them) |
| `date` | `YYYY-MM-DD` (defaults to today in `America/New_York`) |

### `/coming`

Unique movies scheduled from today as far ahead as AMC/Fandango has dates (no duplicate titles). Each movie shows the first and last date it appears. The heading uses that last listed date, not a fixed December cutoff.

| Option | Meaning |
| --- | --- |
| `theater` | One configured theater, or all (default: all of them) |
| `through` | Optional last date as `YYYY-MM-DD` if you want to stop early |

The first run can take a minute while it walks the calendar. Results are cached for a few hours.

The daily 9 AM post is still **today’s showtimes only**.

### Lincoln Square watcher

The bot polls **AMC Lincoln Square 13 only** about every 60 seconds (`WATCH_INTERVAL_SECONDS`) using Fandango’s theater calendar, then showtimes for those dates. Fresh Meadows and Bay Terrace are not watched.

- First successful poll writes a buyable-only baseline to `data/seen_showtimes.json` and does **not** ping Discord.
- Later polls `@here` the channel when a Lincoln Square showtime becomes **buyable** on Fandango (`type: available`), not when a grayed-out placeholder listing first appears.
- HTTP 403/429 backs off for 10 minutes. If a scan is still running, the next tick is skipped.
- That seen file is gitignored so a GCP restart does not re-spam the channel. After this buyable change, the first poll rebases that file (no ping).

```
WATCH_THEATRE=lincoln-square
WATCH_INTERVAL_SECONDS=60
```

```bash
./showtimes --coming --theater lincoln-square
./showtimes --coming
```

## Change or add theaters

Theater list lives in [`src/amc_scraper/theatres.py`](src/amc_scraper/theatres.py). The `/showtimes` menu, CLI `--theater` flag, and daily post all read from `THEATRES` there.

### Replace one of the current theaters

Edit the `Theatre(...)` block you want to change, then leave it in the `THEATRES` tuple.

### Add another theater

1. Open the AMC showtimes page, for example  
   `https://www.amctheatres.com/movie-theatres/new-york-city/amc-fresh-meadows-7/showtimes`  
   The path after `/movie-theatres/` is `path` (`new-york-city/amc-fresh-meadows-7`). The last segment is `amc_slug`.
2. Open the same theater on Fandango. The URL looks like  
   `https://www.fandango.com/amc-loews-fresh-meadows-7-aabtm/theater-page`  
   `fandango_slug` is the name (`amc-loews-fresh-meadows-7`) and `fandango_id` is the short code (`aabtm`).
3. Add a `Theatre(...)` and append it to `THEATRES`:

```python
LINCOLN_SQUARE = Theatre(
    key="lincoln-square",
    name="AMC Lincoln Square 13",
    path="new-york-city/amc-lincoln-square-13",
    slug="lincolnsquare",
    fandango_id="AABCD",
    amc_slug="amc-lincoln-square-13",
    fandango_slug="amc-lincoln-square-13",
)

THEATRES: tuple[Theatre, ...] = (FRESH_MEADOWS, BAY_TERRACE, LINCOLN_SQUARE)
```

`key` is the CLI / slash-command value (`./showtimes --theater lincoln-square`). Discord allows at most 25 slash-command choices, including **All**. Lincoln Square is already in `THEATRES`; the block above is only a template for adding another house.

Restart the bot after editing (`sudo systemctl restart amc-bot` on the VM).

## Run 24/7 on a free GCP VM

`./bot` only stays online while that process is running. Cloud Run / Cloud Functions will not work well here: the bot needs a persistent Discord websocket.

The fit for a free GCP account is **one always-free Compute Engine `e2-micro` VM**:

- Region must be `us-west1`, `us-central1`, or `us-east1`
- 1 non-preemptible `e2-micro` per month (enough hours to run 24/7)
- You still need a billing account (credit card). Set a **budget alert at $1** so a wrong VM size cannot surprise you
- Skip `playwright install chromium` on this VM. The usual Fandango HTTP path does not need a browser, and Chromium will not fit comfortably in 1 GB RAM

### 1. Create the VM

In [Google Cloud Console](https://console.cloud.google.com/):

1. Create a project and enable billing.
2. Compute Engine → VM instances → Create.
3. Machine: `e2-micro`, OS: Ubuntu 24.04 LTS, region: `us-east1` (or west/central).
4. Boot disk: 30 GB standard persistent disk (free-tier size).
5. Allow HTTP is not needed. SSH is enough.
6. Create, then SSH in.

### 2. Install and start the bot

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
sudo useradd --create-home --shell /bin/bash amc
sudo su - amc

git clone https://github.com/YOUR_USER/amc-scraper.git
cd amc-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
# Do not run: python3 -m playwright install chromium

cp .env.example .env
nano .env   # fill DISCORD_TOKEN and DISCORD_CHANNEL_ID
```

Then as your SSH user (not `amc`):

```bash
sudo cp /home/amc/amc-scraper/deploy/amc-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now amc-bot
sudo systemctl status amc-bot
```

Logs:

```bash
journalctl -u amc-bot -f
```

After that, `/showtimes` and the 9:00 AM Eastern daily post keep working even if you close your laptop.
