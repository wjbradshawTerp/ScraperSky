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
