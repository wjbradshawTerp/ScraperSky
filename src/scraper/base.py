from abc import ABC, abstractmethod


class BaseScraper(ABC):
    def __init__(self, account, orchestrator=None):
        self.account = account
        # Shared ExperimentOrchestrator (roadmap Phase 5), or None for any
        # account that hasn't opted in (no experiment_design.treatment_arms
        # configured) -- see main.py's opt-in gate. None here means this
        # scraper is entirely unaffected by orchestration.
        self.orchestrator = orchestrator

    @abstractmethod
    def run(self):
        pass

    @abstractmethod
    def fetch_home(self, max_pages=None):
        """Observes the "home" target: X's algorithmic default feed (the
        "Home" tab, GraphQL operation `HomeTimeline`). `max_pages` bounds
        one call so several targets can be observed in rotation."""
        pass

    @abstractmethod
    def fetch_following(self, max_pages=None):
        """Observes the "following" target: X's reverse-chronological feed
        (the "Following" tab, GraphQL operation `HomeLatestTimeline`).
        `max_pages` bounds one call, as with `fetch_home`."""
        pass
