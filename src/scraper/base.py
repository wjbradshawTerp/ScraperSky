from abc import ABC, abstractmethod


class BaseScraper(ABC):
    def __init__(
        self,
        mode: str,
    ):
        self.mode = mode

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def scrape_home(self):
        pass

    @abstractmethod
    def scrape_follows(self):
        pass
