from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional


class BaseScraper(ABC):
    def __init__(
        self,
        keywords: Optional[List[str]] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ):
        self.keywords = keywords
        self.start_date = start_date
        self.end_date = end_date

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def scrape_keywords(self):
        pass

    @abstractmethod
    def scrape_follows(self):
        pass
