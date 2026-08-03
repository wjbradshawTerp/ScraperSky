from config import settings
from scraper import SCRAPER_REGISTRY

VALID_TARGETS = {"for_you_feed", "home_timeline", "search"}


def validate_targets(targets):
    invalid = [t for t in targets if t not in VALID_TARGETS]
    if invalid:
        raise ValueError(
            f"Invalid data_collection target(s): {invalid}. Must be one of {VALID_TARGETS}."
        )
    # The scraper is still a single-threaded process that observes one
    # timeline continuously; simultaneous multi-target polling needs the
    # Agent Runtime's per-cycle scheduler (roadmap Phase 4).
    if len(targets) != 1:
        raise ValueError(
            f"Exactly one data_collection target is supported for now; got {targets!r}."
        )


def main():
    settings.validate()
    validate_targets(settings.DATA_COLLECTION_TARGETS)
    if "search" in settings.DATA_COLLECTION_TARGETS and not settings.SEARCH_QUERY:
        raise ValueError(
            "config.yaml's data_collection.search_query is required when targets includes 'search'."
        )
    scraper_class = SCRAPER_REGISTRY.get(settings.PLATFORM)
    if not scraper_class:
        raise ValueError(f"Unsupported platform: {settings.PLATFORM}")

    scraper = scraper_class(
        targets=settings.DATA_COLLECTION_TARGETS,
        actions=settings.ACTIONS,
    )

    scraper.run()


if __name__ == "__main__":
    main()
