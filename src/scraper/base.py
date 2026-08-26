from abc import ABC, abstractmethod


class BaseScraper(ABC):
    def __init__(self, account):
        self.account = account

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def fetch_home(self):
        """Observes the "home" target: X's algorithmic default feed (the
        "Home" tab, GraphQL operation `HomeTimeline`)."""
        pass

    @abstractmethod
    def fetch_following(self):
        """Observes the "following" target: X's reverse-chronological feed
        (the "Following" tab, GraphQL operation `HomeLatestTimeline`)."""
        pass
