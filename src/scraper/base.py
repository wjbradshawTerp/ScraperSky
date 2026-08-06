from abc import ABC, abstractmethod


class BaseScraper(ABC):
    def __init__(self, account):
        self.account = account

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def fetch_for_you_feed(self):
        pass

    @abstractmethod
    def fetch_home_timeline(self):
        pass
