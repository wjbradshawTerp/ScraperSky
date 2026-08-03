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
4. Review `config.yaml` and adjust `data_collection.targets`/`platform`/etc. as needed (defaults work out of the box).

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

Settings are split across two files:
- **`.env`** — secrets, plus Docker volume paths (machine/deployment-specific, not experiment behavior).
- **`config.yaml`** — everything about how the scraper behaves. Non-secret and safe to share/commit.

### `.env`

| Variable | Description | Default |
|---|---|---|
| `TWITTER_AUTH_TOKEN` | Twitter session auth token | *(required)* |
| `TWITTER_BEARER_TOKEN` | Twitter Bearer token (includes `Bearer ` prefix) | *(required)* |
| `TWITTER_CSRF_TOKEN` | Twitter CSRF token (ct0 cookie) | *(required)* |
| `HOST_OUTPUT_DIR` | Output folder on the host machine | `./data` |
| `CONTAINER_OUTPUT_DIR` | Output folder inside the container | `/app/data` |
| `OUTPUT_DIR` | Output folder as seen by the app itself (matches `CONTAINER_OUTPUT_DIR` when run via Docker) | `/app/data` |
| `CONFIG_PATH` | Path to `config.yaml` (see below) | `config.yaml` |

### `config.yaml`

| Key | Description | Default |
|---|---|---|
| `platform` | Platform to scrape. Currently only `twitter` | *(required)* |
| `data_collection.targets` | Which timeline(s) to observe. One of `for_you_feed` (algorithmic "For You"), `home_timeline` (chronological Following), or `search` (keyword search) — exactly one entry for now (see below) | *(required)* |
| `data_collection.search_query` | Query text for the `search` target ("All of these words" — plain keywords, no query operators yet). Required if `search` is a target | *(required if using `search`)* |
| `actions.follow_all` | Whether to run `follow_all()` at startup, following every account in the `follow_list` account list | `false` |
| `scroll_delay` | Seconds between timeline requests. Minimum of 2 recommended | `2` |
| `fetch_max_retries` | Retries for a single cursor before giving up and restarting the timeline from the top | `5` |
| `fetch_retry_backoff` | Base seconds for retry backoff (multiplied by attempt number) | `5` |
| `timezone` | Timezone for output file timestamps ([zoneinfo](https://docs.python.org/3/library/zoneinfo.html) format) | `America/New_York` |
| `account_lists` | Named account lists (see below) | *(required — at least `follow_list`)* |

```yaml
platform: twitter
data_collection:
  targets:
    - home_timeline
  search_query: trump   # only read when "search" is a target
actions:
  follow_all: true
scroll_delay: 2
fetch_max_retries: 5
fetch_retry_backoff: 5
timezone: America/New_York
account_lists:
  follow_list: follow_user_ids.json
```

Observation and actions are independent: `data_collection.targets` picks what gets scraped, `actions.follow_all` separately controls whether `follow_all()` runs at startup — you can follow without scraping, scrape without following, or both. `follow_all()` reads the `follow_list` account list; add more named lists as needed, nothing else changes until code reads a given name.

Only one `data_collection.targets` entry is supported per run today — the scraper is still a single-threaded process that observes one timeline continuously. Simultaneous multi-target polling (observing `for_you_feed` and `home_timeline` together every cycle, as the eventual Agent Runtime needs) is planned but not yet built.

Actions beyond `follow_all` (liking, retweeting, muting) are implemented (`favorite_tweet()`, `retweet()`, `mute_user()`, `follow_user()`) and reachable through a single dispatcher, `TwitterScraper.execute_action(action, target)`, but nothing calls it automatically yet outside of `follow_all()` — that wiring is the future LLM-driven decision loop's job.

> **Known issue:** the `search` target is fully wired (query hash, variables, response parsing) but currently returns `404` against the live API. `x-client-transaction-id` and `content-type: application/json` have been added to GET requests as likely fixes; neither has confirmed it yet. `for_you_feed` and `home_timeline` are unaffected and working.

## Follow list

Each account list is a JSON file shaped like `follow_user_ids.json`:

```json
{
  "users": [
    { "user_id": "155659213", "_comment": "Ronaldo" },
    { "user_id": "1178432333764009989", "_comment": "NOlivier17" }
  ]
}
```

`_comment` is optional and used for logging only. The scraper tracks which accounts it has already followed (`data/state/twitter_agent_state.json`) and skips ones already followed on subsequent runs, so restarting the container doesn't re-send follow requests for accounts it followed in a previous run.

### Rate limits
Twitter enforces the following limits on follows:
- **15 follows per 15-minute window**
- **400 follows per day** (platform-wide, applies to web and API equally)

The scraper respects these limits — it stops immediately if rate limited and reports the reset time.

## Continuous scraping

The timeline scraper is designed to run indefinitely rather than stop when it reaches the end of what Twitter's pagination will offer:

- **Bad responses are retried, not fatal.** Non-JSON responses, rate limits (`429`), and malformed/empty response bodies no longer crash the process. The scraper retries the same cursor with backoff (`fetch_retry_backoff` seconds × attempt number, up to `fetch_max_retries` times).
- **Exhausted pagination restarts from the top.** When the bottom cursor stops advancing or a cursor keeps failing after all retries, the scraper logs it and restarts pagination from the top of the timeline instead of exiting — so it keeps collecting new tweets as they arrive rather than terminating.
- **`for_you_feed` and `search` avoid re-collecting the same posts.** Both endpoints rank against a finite pool of candidates at any moment, so naively restarting from the top would otherwise just re-serve the same batch. `fetch_for_you_feed()` and `fetch_search_timeline()` mirror what the real web app does to avoid this: they echo the tweet IDs from the page just received back to Twitter as `seenTweetIds` on the next request, telling the ranking backend not to re-serve them, and separately keep a local cache of every tweet ID collected during the run so any duplicate that slips through anyway is filtered out before being written to the output file. `home_timeline` (the chronological Following timeline) doesn't need this and is unaffected.

> **Note:** Twitter periodically rotates the internal GraphQL query hash and feature flags each timeline endpoint expects (`TWITTER_HOME_TIMELINE_HASH`/`TWITTER_HOME_TIMELINE_FEATURES`, `TWITTER_SEARCH_TIMELINE_HASH`/`TWITTER_SEARCH_TIMELINE_FEATURES` in `src/scraper/twitter.py`). If collection degrades or plateaus, capture a fresh request for that endpoint from the browser DevTools Network tab (same request used to pull `TWITTER_BEARER_TOKEN`/`TWITTER_CSRF_TOKEN` above) and update the matching constants.

## Running

Once `.env` and `config.yaml` are configured:
```bash
docker compose up --build
```

Output is saved as JSONL files under `data/<date>/twitter/`.

`config.yaml` is bind-mounted into the container (see `docker-compose.yml`), so changes to it take effect on the next restart without rebuilding. If `.env` changes don't take effect:
```bash
docker compose build --no-cache
docker compose up
```
