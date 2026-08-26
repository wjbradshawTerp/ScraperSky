import os
import threading

import httpx

from config import settings
from scraper import SCRAPER_REGISTRY
from runtime.decision import DEFAULT_BASE_URL

# "home" = X's algorithmic default feed (GraphQL HomeTimeline, the "Home"
# tab); "following" = X's reverse-chronological feed (GraphQL
# HomeLatestTimeline, the "Following" tab). See the naming key near the top
# of src/scraper/twitter.py.
VALID_TARGETS = {"home", "following", "search"}

# Documented live-platform follow limit (see ROADMAP.md's rate-limit note) --
# used to reject an infeasible cold-start config at startup rather than
# letting it silently stall for days.
FOLLOWS_PER_DAY = 400


def validate_targets(targets, agent_runtime_enabled):
    invalid = [t for t in targets if t not in VALID_TARGETS]
    if invalid:
        raise ValueError(
            f"Invalid data_collection target(s): {invalid}. Must be one of {VALID_TARGETS}."
        )
    if not targets:
        raise ValueError("At least one data_collection target is required.")
    # Outside Agent Runtime mode the scraper is still a single-threaded
    # process that observes one timeline continuously. Agent Runtime mode
    # (roadmap Phase 4) observes a snapshot of every configured target each
    # activation, so multiple targets are fine there.
    if not agent_runtime_enabled and len(targets) != 1:
        raise ValueError(
            f"Exactly one data_collection target is supported outside Agent Runtime mode "
            f"(no sockpuppet_config.persona_prompt configured); got {targets!r}."
        )


def validate_cold_start(account):
    params = account.initialization_params
    if not params:
        return

    required = ("min_follows", "min_engagements", "max_initialization_days")
    missing = [key for key in required if key not in params]
    if missing:
        raise ValueError(
            f"Account '{account.name}': sockpuppet_config.initialization_params is missing: "
            f"{', '.join(missing)}."
        )

    if not account.cold_start_account_lists:
        raise ValueError(
            f"Account '{account.name}': sockpuppet_config.initialization_params is set but "
            f"sockpuppet_config.account_lists is empty -- no candidate accounts to follow/engage."
        )
    for list_name in account.cold_start_account_lists:
        account.get_account_list_path(list_name)

    min_follows = params["min_follows"]
    max_days = params["max_initialization_days"]
    max_reachable = FOLLOWS_PER_DAY * max_days
    if min_follows > max_reachable:
        raise ValueError(
            f"Account '{account.name}': initialization_params.min_follows={min_follows} is not "
            f"reachable within max_initialization_days={max_days} given the platform's "
            f"{FOLLOWS_PER_DAY}/day follow limit (max reachable: {max_reachable}). Raise "
            f"max_initialization_days or lower min_follows."
        )


def validate_agent_runtime(account):
    if not account.agent_runtime_enabled:
        return

    base_url = os.getenv("OLLAMA_BASE_URL", DEFAULT_BASE_URL)
    try:
        httpx.get(f"{base_url}/api/tags", timeout=3)
    except httpx.RequestError as e:
        raise ValueError(
            f"Account '{account.name}': sockpuppet_config.persona_prompt is configured (Agent Runtime "
            f"mode) but Ollama isn't reachable at {base_url} ({e}). Is `ollama serve` running? If this "
            f"is running inside Docker, 'localhost' means the container, not your host -- set "
            f"OLLAMA_BASE_URL in .env to something the container can actually reach (e.g. "
            f"http://host.docker.internal:11434 on Docker Desktop)."
        )

    # Raises if the configured path is missing/unreadable -- fail at
    # startup, not on the first decision cycle.
    account.get_persona_prompt_text()


def validate_account(account):
    if not account.platform:
        raise ValueError(
            f"Account '{account.name}': no platform set (set 'platform' in config.yaml "
            f"or override it per-account in accounts.yaml)."
        )
    if account.platform not in SCRAPER_REGISTRY:
        raise ValueError(f"Account '{account.name}': unsupported platform '{account.platform}'.")
    validate_targets(account.targets, account.agent_runtime_enabled)
    if "search" in account.targets and not account.search_query:
        raise ValueError(
            f"Account '{account.name}': data_collection.search_query is required "
            f"when targets includes 'search'."
        )
    validate_cold_start(account)
    validate_agent_runtime(account)


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
