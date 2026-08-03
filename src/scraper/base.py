from abc import ABC, abstractmethod


class BaseScraper(ABC):
    def __init__(
        self,
        targets: list,
        actions: dict,
    ):
        self.targets = targets
        self.actions = actions

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def fetch_for_you_feed(self):
        pass

    @abstractmethod
    def fetch_home_timeline(self):
        pass
