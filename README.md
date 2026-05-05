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

## Running a scraper

### Option 1: Using Docker
