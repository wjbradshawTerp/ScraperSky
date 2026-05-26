# ScraperSky
X/Twitter bot and scraping system for the UMD iSchool

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
3. Open .env and enter:
```
TWITTER_AUTH_TOKEN
TWITTER_BEARER_TOKEN
TWITTER_CSRF_TOKEN
```

## Where to find credentials (automatic credential extraction WIP)
To get **TWITTER_AUTH_TOKEN, TWITTER_BEARER_TOKEN, TWITTER_CSRF_TOKEN**:
1. Open your Twitter account on desktop.
2. Go to Home/For You page.
3. Open developer tools, and go to the Network tab.
<img width="1206" height="470" alt="image" src="https://github.com/user-attachments/assets/e7a4ae50-d7ae-4bfe-85c6-92c66dbf5496" />

4. Find a request titled **user_flow.json**, anyone will do. If you can't find any, try scrolling down the Twitter page a little.
5. In the user_flow.json, scroll down to the **Request Headers** section.
6. Set **TWITTER_BEARER_TOKEN** to the value shown in **Authorization**.
7. Set **TWITTER_AUTH_TOKEN** to the value shown in **Cookie**, in the **auth_token** field.
8. Set **TWITTER_CSRF_TOKEN** to the value shown in **Cookie**, in the **ct0** field.

## Scraper settings
The rest of the .env file is for scraper settings.  
`SCROLL_DELAY` is the seconds between every request for more Twitter posts. A minimum of 2 is recommended.  
`HOST_OUTPUT_DIR`, `CONTAINER_OUTPUT_DIR`, and `OUTPUT_DIR` can be used to configure the output folder for tweets. By default, the output goes to `ScraperSky/data`.  
`TIMEZONE` is used by the FileManager to save data at the correct time, uses zoneinfo for timezones.  
`PLATFORM` is currently only limited to twitter.  
`MODE` determines what the bot will target, either the Home/For you page or the Following page, which are `home` and `follows` respectively.

## Running a scraper
Once .env is configured, use
```bash
docker compose up --build
```

If changes were made to the .env file but did not take effect, instead use
```bash
docker compose build --no--cache
docker compose up
```
