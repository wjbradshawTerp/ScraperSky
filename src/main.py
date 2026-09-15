import os
import threading

import httpx

from config import settings, DATA_STREAM_TARGETS
from scraper import SCRAPER_REGISTRY
from runtime.decision import DEFAULT_BASE_URL
from orchestrator import spec
from orchestrator.orchestrator import ExperimentOrchestrator
from utils.rate_budget import build_budgets

# "home" = X's algorithmic default feed (GraphQL HomeTimeline, the "Home"
# tab); "following" = X's reverse-chronological feed (GraphQL
# HomeLatestTimeline, the "Following" tab). See the naming key near the top
# of src/scraper/twitter.py.
VALID_TARGETS = {"home", "following", "search"}

# paper section 3.1: `observation_frequency: every_visit`. One observation
# per activation is what the runtime does; nothing else is implemented.
SUPPORTED_OBSERVATION_FREQUENCIES = ("every_visit",)

# Actions a `sockpuppet_config.action_rate_limits` entry may pace -- the
# same set execute_action() can dispatch (no_action needs no budget).
VALID_RATE_LIMITED_ACTIONS = {"like", "retweet", "follow", "mute"}

# Documented live-platform follow limit (see ROADMAP.md's rate-limit note) --
# used to reject an infeasible cold-start config at startup rather than
# letting it silently stall for days.
FOLLOWS_PER_DAY = 400


def validate_targets(targets):
    """`targets` here is the observation targets only -- Account already
    split out the paper's derived data streams (engagement_log,
    intervention_history), which aren't feeds to observe.
    """
    invalid = [t for t in targets if t not in VALID_TARGETS]
    if invalid:
        raise ValueError(
            f"Invalid data_collection target(s): {invalid}. Must be one of "
            f"{sorted(VALID_TARGETS)} (observation feeds) or {list(DATA_STREAM_TARGETS)} "
            f"(derived data streams)."
        )
    if not targets:
        raise ValueError("At least one observable data_collection target is required.")


def validate_action_rate_limits(account):
    """Parses `sockpuppet_config.action_rate_limits` so a malformed entry
    (or an action name nothing can execute) fails at startup rather than
    mid-run. No-op when none are configured.
    """
    limits = account.action_rate_limits
    if not limits:
        return
    unknown = [a for a in limits if a not in VALID_RATE_LIMITED_ACTIONS]
    if unknown:
        raise ValueError(
            f"Account '{account.name}': action_rate_limits names action(s) {unknown} that "
            f"can't be executed; expected any of {sorted(VALID_RATE_LIMITED_ACTIONS)}."
        )
    try:
        build_budgets(limits)
    except ValueError as e:
        raise ValueError(f"Account '{account.name}': {e}") from e


def validate_observation_frequency(account):
    if account.observation_frequency not in SUPPORTED_OBSERVATION_FREQUENCIES:
        raise ValueError(
            f"Account '{account.name}': unsupported data_collection.observation_frequency "
            f"{account.observation_frequency!r}; this framework implements "
            f"{list(SUPPORTED_OBSERVATION_FREQUENCIES)} (one observation per activation)."
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

    max_follows = params.get("max_follows")
    if max_follows is not None:
        if not isinstance(max_follows, int) or max_follows < 1:
            raise ValueError(
                f"Account '{account.name}': initialization_params.max_follows must be a positive "
                f"integer; got {max_follows!r}."
            )
        if params["min_follows"] > max_follows:
            raise ValueError(
                f"Account '{account.name}': initialization_params.min_follows="
                f"{params['min_follows']} exceeds max_follows={max_follows}, so cold-start could "
                f"never satisfy its own stopping criteria and would run until "
                f"max_initialization_days elapsed."
            )

    mix = params.get("follow_mix")
    if mix is not None:
        if not isinstance(mix, dict) or not mix:
            raise ValueError(
                f"Account '{account.name}': initialization_params.follow_mix must be a non-empty "
                f"mapping of account-list name to count; got {mix!r}."
            )
        unknown = [name for name in mix if name not in account.cold_start_account_lists]
        if unknown:
            raise ValueError(
                f"Account '{account.name}': initialization_params.follow_mix names "
                f"{unknown}, which are not in sockpuppet_config.account_lists "
                f"({list(account.cold_start_account_lists)})."
            )
        for name, count in mix.items():
            if not isinstance(count, int) or count < 0:
                raise ValueError(
                    f"Account '{account.name}': initialization_params.follow_mix.{name} must be a "
                    f"non-negative integer; got {count!r}."
                )
        total = sum(mix.values())
        if total == 0:
            raise ValueError(
                f"Account '{account.name}': initialization_params.follow_mix sums to 0 -- "
                f"cold-start would have no accounts to follow."
            )
        if max_follows is not None and total > max_follows:
            raise ValueError(
                f"Account '{account.name}': initialization_params.follow_mix sums to {total}, "
                f"which exceeds max_follows={max_follows}. Lower the mix or raise max_follows -- "
                f"silently trimming would break the composition the mix exists to guarantee."
            )

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


def validate_intervention(account):
    """No-op if this account has no `intervention` configured. Otherwise
    parses it through the paper's published schema (action/target_accounts/
    selection_rule/execution_time -- see orchestrator/spec.py) and checks
    that `target_accounts` actually resolves to a real account list, so a
    bad intervention config fails at startup rather than when it's due to
    fire mid-experiment.
    """
    if not account.intervention:
        return
    try:
        parsed = spec.parse_intervention(account.intervention)
    except ValueError as e:
        raise ValueError(f"Account '{account.name}': {e}") from e
    account.get_account_list_path(parsed["target_accounts"])
    unknown_arms = [arm for arm in parsed["applies_to_arms"] if arm not in account.treatment_arms]
    if unknown_arms:
        raise ValueError(
            f"Account '{account.name}': intervention.applies_to_arms names arm(s) "
            f"{unknown_arms} that aren't in experiment_design.treatment_arms "
            f"({account.treatment_arms})."
        )


def validate_experiment_orchestration(accounts):
    """Every account with `experiment_design.treatment_arms` configured
    opts into the shared Experiment Orchestrator (roadmap Phase 5). At
    most one experiment is supported per process (one accounts.yaml, one
    orchestrator instance) -- reject a config naming more than one, or
    where opted-in accounts disagree on the experiment-wide
    `randomization`/`phase_durations` values (a likely copy-paste mistake,
    since those apply to the whole experiment, not per-account).

    Returns the list of opted-in accounts (empty if none), so main() can
    decide whether to construct an orchestrator at all.
    """
    experiment_accounts = [a for a in accounts if a.treatment_arms]
    if not experiment_accounts:
        return []

    names = {a.experiment_design.get("experiment_name") for a in experiment_accounts}
    if len(names) > 1:
        raise ValueError(
            f"Accounts with treatment_arms configured must share one experiment_design.experiment_name "
            f"(one Experiment Orchestrator per process); found: {sorted(n for n in names if n)}."
        )

    # Rejects an unsupported randomization procedure at startup rather than
    # after the pre-treatment period, when it's far too late to fix.
    for account in experiment_accounts:
        try:
            spec.parse_randomization_method(account.randomization_method)
        except ValueError as e:
            raise ValueError(f"Account '{account.name}': {e}") from e

    first = experiment_accounts[0]
    for account in experiment_accounts[1:]:
        if account.experiment_design.get("randomization") != first.experiment_design.get("randomization"):
            raise ValueError(
                f"Account '{account.name}': experiment_design.randomization must match every other "
                f"opted-in account's -- it's experiment-wide, not per-account."
            )
        if account.experiment_design.get("phase_durations") != first.experiment_design.get("phase_durations"):
            raise ValueError(
                f"Account '{account.name}': experiment_design.phase_durations must match every other "
                f"opted-in account's -- it's experiment-wide, not per-account."
            )

    for account in experiment_accounts:
        validate_intervention(account)

    return experiment_accounts


def validate_account(account):
    if not account.platform:
        raise ValueError(
            f"Account '{account.name}': no platform set (set 'platform' in config.yaml "
            f"or override it per-account in accounts.yaml)."
        )
    if account.platform not in SCRAPER_REGISTRY:
        raise ValueError(f"Account '{account.name}': unsupported platform '{account.platform}'.")
    validate_targets(account.targets)
    validate_observation_frequency(account)
    validate_action_rate_limits(account)
    if "search" in account.targets and not account.search_query:
        raise ValueError(
            f"Account '{account.name}': data_collection.search_query is required "
            f"when targets includes 'search'."
        )
    validate_cold_start(account)
    validate_agent_runtime(account)


def run_account(account, orchestrator=None):
    try:
        scraper = SCRAPER_REGISTRY[account.platform](account, orchestrator=orchestrator)
        scraper.run()
    except Exception as e:
        print(f"[{account.name}] Fatal error, this account's scraper has stopped:")
        if orchestrator is not None:
            orchestrator.log_failure(account.name, repr(e))
        raise


def main():
    settings.validate()
    accounts = settings.load_accounts()

    # Validate every account's resolved config up front, before spinning up
    # any threads, so a typo in one account's overrides fails fast instead
    # of surfacing only after the others are already mid-run.
    for account in accounts:
        validate_account(account)
    experiment_accounts = validate_experiment_orchestration(accounts)

    # Each account's scraper.run() loops forever observing its own target,
    # so every account keeps its own thread -- the ExperimentOrchestrator
    # (roadmap Phase 5) is a shared coordinator these threads consult, not
    # a replacement for them. An account with no treatment_arms configured
    # gets orchestrator=None even when one exists for sibling accounts, so
    # it's entirely unaffected.
    orchestrator = None
    if experiment_accounts:
        first = experiment_accounts[0]
        orchestrator = ExperimentOrchestrator(
            experiment_name=first.experiment_design.get("experiment_name") or "unnamed_experiment",
            participating_accounts=[a.name for a in experiment_accounts],
            experiment_design=first.experiment_design,
            intervention_by_account={a.name: a.intervention for a in experiment_accounts},
            output_dir=settings.OUTPUT_DIR,
            timezone=first.timezone,
            agent_id_by_account={a.name: a.agent_id for a in experiment_accounts},
            action_rate_limits_by_account={
                a.name: a.action_rate_limits for a in experiment_accounts
            },
        )
        orchestrator.start()
        print(
            f"Experiment Orchestrator started for '{orchestrator.experiment_name}' "
            f"({len(experiment_accounts)} account(s)): {', '.join(a.name for a in experiment_accounts)}"
        )

    threads = [
        threading.Thread(
            target=run_account,
            args=(account, orchestrator if account.treatment_arms else None),
            name=account.name,
            daemon=True,
        )
        for account in accounts
    ]

    print(f"Starting {len(threads)} account(s): {', '.join(a.name for a in accounts)}")
    for thread in threads:
        thread.start()
    # Each account thread returns on its own once the experiment completes
    # (paper section 3.4 responsibility #5: "the experiment terminates after
    # completion of the post-treatment phase") -- every long-running loop in
    # the scraper checks orchestrator.should_stop(). So joining here is what
    # actually shuts the process down cleanly at the end of an experiment,
    # rather than blocking forever.
    for thread in threads:
        thread.join()

    if orchestrator is not None and orchestrator.should_stop():
        print(
            f"Experiment '{orchestrator.experiment_name}' complete "
            f"(phase: {orchestrator.get_phase()}); all accounts stopped."
        )
    else:
        print("All account threads have stopped.")


if __name__ == "__main__":
    main()
