# ScraperSky
X/Twitter account orchestration and scraping system.

## Requirements
[Docker Engine](https://docs.docker.com/engine/install)

## Installation
1. Clone repo:
```bash
git clone https://github.com/your-username/ScraperSky.git
cd ScraperSky
```
2. Create env file:
```bash
cp .env.example .env
```
3. Open `.env` and fill in your credentials (see below).

## Credentials

To get **TWITTER_AUTH_TOKEN**, **TWITTER_BEARER_TOKEN**, and **TWITTER_CSRF_TOKEN**:

1. Open your Twitter account on desktop and go to the Home/For You page.
2. Open browser DevTools and go to the **Network** tab.

<img width="1206" height="470" alt="image" src="https://github.com/user-attachments/assets/e7a4ae50-d7ae-4bfe-85c6-92c66dbf5496" />

3. Find any request titled **user_flow.json** (scroll the Twitter page a little if none appear).
4. In the request, scroll to the **Request Headers** section:
   - Set `TWITTER_BEARER_TOKEN` to the value of **Authorization**
   - Set `TWITTER_AUTH_TOKEN` to the **auth_token** field inside **Cookie**
   - Set `TWITTER_CSRF_TOKEN` to the **ct0** field inside **Cookie**

> **Note:** Credentials expire when you log out or Twitter rotates them. If you get 403 errors, extract fresh credentials and update `.env`.

## Configuration

| Variable | Description | Default |
|---|---|---|
| `TWITTER_AUTH_TOKEN` | Twitter session auth token | *(required)* |
| `TWITTER_BEARER_TOKEN` | Twitter Bearer token (includes `Bearer ` prefix) | *(required)* |
| `TWITTER_CSRF_TOKEN` | Twitter CSRF token (ct0 cookie) | *(required)* |
| `MODE` | Scrape mode: `home` (For You page) or `follows` (Following page) | *(required)* |
| `PLATFORM` | Platform to scrape. Currently only `twitter` | *(required)* |
| `SCROLL_DELAY` | Seconds between timeline requests. Minimum of 2 recommended | `2` |
| `FETCH_MAX_RETRIES` | Retries for a single cursor before giving up and restarting the timeline from the top | `5` |
| `FETCH_RETRY_BACKOFF` | Base seconds for retry backoff (multiplied by attempt number) | `5` |
| `TIMEZONE` | Timezone for output file timestamps ([zoneinfo](https://docs.python.org/3/library/zoneinfo.html) format) | `America/New_York` |
| `HOST_OUTPUT_DIR` | Output folder on the host machine | `./data` |
| `CONTAINER_OUTPUT_DIR` | Output folder inside the container | `/app/data` |
| `FOLLOW_LIST_PATH` | Path to the follow list JSON inside the container | `/app/follow_user_ids.json` |

## Follow list

When `MODE=follows`, the scraper will follow all accounts listed in `follow_user_ids.json` before scraping. The file format is:

```json
{
  "users": [
    { "user_id": "155659213", "_comment": "Ronaldo" },
    { "user_id": "1178432333764009989", "_comment": "NOlivier17" }
  ]
}
```

`_comment` is optional and used for logging only.

### Rate limits
Twitter enforces the following limits on follows:
- **15 follows per 15-minute window**
- **400 follows per day** (platform-wide, applies to web and API equally)

The scraper respects these limits — it stops immediately if rate limited and reports the reset time.

## Continuous scraping

The timeline scraper is designed to run indefinitely rather than stop when it reaches the end of what Twitter's pagination will offer:

- **Bad responses are retried, not fatal.** Non-JSON responses, rate limits (`429`), and malformed/empty response bodies no longer crash the process. The scraper retries the same cursor with backoff (`FETCH_RETRY_BACKOFF` seconds × attempt number, up to `FETCH_MAX_RETRIES` times).
- **Exhausted pagination restarts from the top.** When the bottom cursor stops advancing or a cursor keeps failing after all retries, the scraper logs it and restarts pagination from the top of the timeline instead of exiting — so it keeps collecting new tweets as they arrive rather than terminating.
- **Home ("For You") avoids re-collecting the same posts.** The "For You" ranking endpoint only has a finite pool of candidates at any moment, so naively restarting from the top would otherwise just re-serve the same batch. `MODE=home` mirrors what the real web app does to avoid this: it echoes the tweet IDs from the page it just received back to Twitter as `seenTweetIds` on the next request, telling the ranking backend not to re-serve them, and separately keeps a local cache of every tweet ID collected during the run so any duplicate that slips through anyway is filtered out before being written to the output file. `MODE=follows` (the chronological Following timeline) doesn't need this and is unaffected.

> **Note:** Twitter periodically rotates the internal GraphQL query hash and feature flags the Home timeline endpoint expects (`TWITTER_HOME_TIMELINE_HASH` / `TWITTER_HOME_TIMELINE_FEATURES` in `src/scraper/twitter.py`). If `MODE=home` collection degrades or plateaus again, capture a fresh `HomeTimeline` request from the browser DevTools Network tab (same request used to pull `TWITTER_BEARER_TOKEN`/`TWITTER_CSRF_TOKEN` above) and update those constants to match.

## Running

Once `.env` is configured:
```bash
docker compose up --build
```

Output is saved as JSONL files under `data/<date>/twitter/`.

If `.env` changes don't take effect:
```bash
docker compose build --no-cache
docker compose up
```
