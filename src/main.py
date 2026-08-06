import threading

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


def validate_account(account):
    if not account.platform:
        raise ValueError(
            f"Account '{account.name}': no platform set (set 'platform' in config.yaml "
            f"or override it per-account in accounts.yaml)."
        )
    if account.platform not in SCRAPER_REGISTRY:
        raise ValueError(f"Account '{account.name}': unsupported platform '{account.platform}'.")
    validate_targets(account.targets)
    if "search" in account.targets and not account.search_query:
        raise ValueError(
            f"Account '{account.name}': data_collection.search_query is required "
            f"when targets includes 'search'."
        )


def run_account(account):
    try:
        scraper = SCRAPER_REGISTRY[account.platform](account)
        scraper.run()
    except Exception:
        print(f"[{account.name}] Fatal error, this account's scraper has stopped:")
        raise


def main():
    settings.validate()
    accounts = settings.load_accounts()

    # Validate every account's resolved config up front, before spinning up
    # any threads, so a typo in one account's overrides fails fast instead
    # of surfacing only after the others are already mid-run.
    for account in accounts:
        validate_account(account)

    # Each account's scraper.run() loops forever observing its own target,
    # so every account needs its own thread -- this is a stand-in for the
    # Experiment Orchestrator's per-agent activation scheduler (roadmap
    # Phase 5), not that scheduler itself.
    threads = [
        threading.Thread(
            target=run_account,
            args=(account,),
            name=account.name,
            daemon=True,
        )
        for account in accounts
    ]

    print(f"Starting {len(threads)} account(s): {', '.join(a.name for a in accounts)}")
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
