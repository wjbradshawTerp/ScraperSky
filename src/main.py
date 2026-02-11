import argparse
from datetime import datetime
from scraper import SCRAPER_REGISTRY


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape social media platform data based on specified parameters."
    )

    parser.add_argument(
        "--platform",
        type=str,
        required=True,
        help="Platform to scrape",
    )

    parser.add_argument(
        "--keywords",
        type=str,
        help="Comma-separated list of keywords to search for (if empty, scrape followed accounts)",
    )

    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date for scraping (YYYY-MM-DD)",
    )

    parser.add_argument(
        "--end-date",
        type=str,
        help="End date for scraping (YYYY-MM-DD)",
    )

    args = parser.parse_args()
    validate_dates(args)

    return args


def validate_dates(args):
    if args.start_date and not args.end_date:
        raise ValueError("End date must be provided if start date is specified.")
    elif args.end_date and not args.start_date:
        raise ValueError("Start date must be provided if end date is specified.")

    if args.start_date and args.end_date:
        try:
            args.start_date = datetime.strptime(args.start_date, "%Y-%m-%d")
            args.end_date = datetime.strptime(args.end_date, "%Y-%m-%d")
        except ValueError as e:
            raise ValueError(f"Invalid date format: {e}")

        if args.start_date > args.end_date:
            raise ValueError("Start date must be before or equal to end date.")


def parse_keywords(keywords_str):
    if not keywords_str or keywords_str.strip() == "":
        return []
    return [kw.strip() for kw in keywords_str.split(",") if kw.strip()]


def main():
    args = parse_args()
    args.keywords = parse_keywords(args.keywords)

    print("Platform:", args.platform)
    print("Keywords:", args.keywords)
    print("Start Date:", args.start_date)
    print("End Date:", args.end_date)

    scraper_class = SCRAPER_REGISTRY.get(args.platform)
    if not scraper_class:
        raise ValueError(f"Unsupported platform: {args.platform}")

    scraper = scraper_class(
        keywords=args.keywords, start_date=args.start_date, end_date=args.end_date
    )

    scraper.run()


if __name__ == "__main__":
    main()
