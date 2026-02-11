from scraper.base import BaseScraper


class TwitterScraper(BaseScraper):
    def run(self):
        print("Running Twitter Scraper with the following parameters:")
        print("Keywords:", self.keywords)
        print("Start Date:", self.start_date)
        print("End Date:", self.end_date)

        if self.keywords:
            self.scrape_keywords()
        else:
            self.scrape_follows()

    def scrape_keywords(self):
        print("Scraping Twitter for keywords:", ", ".join(self.keywords))
        # Implement actual scraping logic here

    def scrape_follows(self):
        print("Scraping Twitter for all tweets from followed accounts.")
        # Implement actual scraping logic here
