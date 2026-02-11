# ScraperSky
Scraper system for MurkySky expansion

## Installation
1. Clone repo.
2. `cd ScraperSky`
3. `poetry install`
4. `poetry run playwright install chromium`
5. Copy `.env.example`, making a new file `.env`. Fill in `.env` with your information.

# Running a scraper
Basic run
`poetry run python src/main.py --platform twitter`

Run with keywords
`poetry run python src/main.py --platform twitter --keywords "new york times, fox news"`

Run with date range
`poetry run python src/main.py --platform twitter --start-date 2023-01-01 --end-date 2023-12-31`
Both start date and end date must be used

Run with keywords and date range
`poetry run python src/main.py --platform twitter --keywords "new york times, fox news" --start-date 2023-01-01 --end-date 2023-12-31`