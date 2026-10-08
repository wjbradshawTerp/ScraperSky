"""Translation between the paper's published configuration schema
(section 3.1) and the normalized shape the orchestrator works with.

The paper names intervention fields `action`/`target_accounts`/
`selection_rule`/`execution_time` and expresses the treated proportion as a
rule string (`random70`) rather than a float. Keeping that exact vocabulary
in config.yaml matters for a framework meant to be reused straight from the
paper, but the orchestrator internally wants a concrete phase name and
fraction -- so the translation lives here, in one place, rather than being
spread across main.py's validation and the orchestrator's execution path.
"""

from utils.duration import parse_duration

# Paper section 3.1: "The framework currently supports simple random
# assignment and can be extended to support stratified or blocked
# randomization." Anything else is rejected rather than silently treated as
# simple random, since a wrong randomization procedure invalidates the
# experiment's causal claims.
SUPPORTED_RANDOMIZATION_METHODS = ("simple_random",)

# Paper section 3.1: `execution_time: end_of_pre_treatment`. "The
# `execution_time` specifies when the intervention is applied during the
# experimental lifecycle, typically immediately following completion of the
# pre-treatment period" -- i.e. the moment the next phase begins, which is
# how the orchestrator's phase state machine expresses it.
EXECUTION_TIME_TO_PHASE = {
    "end_of_pre_treatment": "treatment",
    "end_of_treatment": "post_treatment",
}

# Paper section 3.1/4: initialization "may terminate adaptively once
# predefined stopping criteria are satisfied" (sockpuppet_config's
# initialization_params), so it has no fixed duration to configure.
ADAPTIVE_PHASE_DURATION = "adaptive"


# Selection-rule prefixes -> sampling method. `randomNN` draws a simple
# random NN% of the target list; `stratifiedNN` treats the list's own order
# as the stratification key, splits it into consecutive blocks of
# STRATUM_SIZE and draws NN% of every block, so the sample is spread evenly
# along whatever the list was sorted by (e.g. followed_by / total_engagement).
SELECTION_METHOD_PREFIXES = {"random": "simple", "stratified": "stratified"}
STRATUM_SIZE = 10


def _split_selection_rule(selection_rule):
    if selection_rule is None:
        raise ValueError(
            "intervention.selection_rule is required -- e.g. 'random70' for a random 70%, "
            "'stratified70' for 70% of every block of 10, or 'all'."
        )
    text = str(selection_rule).strip().lower()
    if text == "all":
        return "all", 1.0
    for prefix, method in SELECTION_METHOD_PREFIXES.items():
        if text.startswith(prefix):
            digits = text[len(prefix):]
            if digits.isdigit() and 0 <= int(digits) <= 100:
                return method, int(digits) / 100.0
    raise ValueError(
        f"Unsupported intervention.selection_rule {selection_rule!r}; expected 'all', "
        f"'randomNN' (e.g. 'random70') or 'stratifiedNN' (e.g. 'stratified70')."
    )


def parse_selection_rule(selection_rule) -> float:
    """`random70` / `stratified70` -> 0.7, `all` -> 1.0 (paper section 3.1:
    "all candidate accounts may be selected, or a fixed proportion (e.g.,
    70%) may be randomly sampled independently for each sockpuppet").
    """
    return _split_selection_rule(selection_rule)[1]


def parse_selection_method(selection_rule) -> str:
    """`random70` -> 'simple', `stratified70` -> 'stratified', `all` -> 'all'."""
    return _split_selection_rule(selection_rule)[0]


def parse_execution_time(execution_time) -> str:
    """Maps the paper's `execution_time` onto the phase whose start fires
    the intervention.
    """
    if execution_time is None:
        raise ValueError(
            f"intervention.execution_time is required -- one of "
            f"{sorted(EXECUTION_TIME_TO_PHASE)}."
        )
    text = str(execution_time).strip().lower()
    if text not in EXECUTION_TIME_TO_PHASE:
        raise ValueError(
            f"Unsupported intervention.execution_time {execution_time!r}; expected one of "
            f"{sorted(EXECUTION_TIME_TO_PHASE)}."
        )
    return EXECUTION_TIME_TO_PHASE[text]


def parse_randomization_method(method) -> str:
    if method is None:
        return SUPPORTED_RANDOMIZATION_METHODS[0]
    text = str(method).strip().lower()
    if text not in SUPPORTED_RANDOMIZATION_METHODS:
        raise ValueError(
            f"Unsupported experiment_design.randomization.method {method!r}; this framework "
            f"currently implements {list(SUPPORTED_RANDOMIZATION_METHODS)} (the paper notes "
            f"stratified/blocked assignment as future extensions)."
        )
    return text


def parse_intervention(intervention_cfg) -> dict:
    """Normalizes the paper's `intervention` namespace into
    {action, target_accounts, fraction, selection_method, trigger_phase,
    applies_to_arms}.

    Returns None for an empty/absent config (an account with no
    intervention configured, e.g. every control-arm-only deployment).

    `applies_to_arms` is this implementation's explicit form of the paper's
    otherwise implicit rule ("Treatment agents receive the configured
    intervention, whereas control agents receive none") -- naming the arms
    outright beats inferring which of `treatment_arms` counts as control.
    """
    if not intervention_cfg:
        return None

    action = intervention_cfg.get("action")
    if not action:
        raise ValueError("intervention.action is required (e.g. 'mute').")

    target_accounts = intervention_cfg.get("target_accounts")
    if not target_accounts:
        raise ValueError(
            "intervention.target_accounts is required -- the name of an account_lists entry "
            "holding the candidate accounts the intervention may be applied to."
        )

    applies_to_arms = intervention_cfg.get("applies_to_arms") or []
    if not applies_to_arms:
        raise ValueError(
            "intervention.applies_to_arms is required -- which treatment_arms values receive "
            "this intervention (the paper's 'treatment agents receive the configured "
            "intervention, whereas control agents receive none')."
        )

    return {
        "action": action,
        "target_accounts": target_accounts,
        "fraction": parse_selection_rule(intervention_cfg.get("selection_rule")),
        "selection_method": parse_selection_method(intervention_cfg.get("selection_rule")),
        "trigger_phase": parse_execution_time(intervention_cfg.get("execution_time")),
        "applies_to_arms": list(applies_to_arms),
        "target_interval": parse_target_interval(intervention_cfg.get("target_interval")),
        "rejection_backoff": parse_rejection_backoff(intervention_cfg.get("rejection_backoff")),
    }


def parse_rejection_backoff(rejection_backoff) -> tuple:
    """`intervention.rejection_backoff: {backoff, max_backoff}` -> (initial,
    cap) seconds. When the platform REJECTS an intervention action (rate
    limit, "looks automated", authorization), the account waits `backoff`,
    doubled for every consecutive rejection, capped at `max_backoff`; a
    success resets it. Defaults to 1m / 30m -- on by default, since it only
    ever slows an intervention down.
    """
    cfg = rejection_backoff or {}
    if not isinstance(cfg, dict):
        raise ValueError(
            f"intervention.rejection_backoff must be {{backoff, max_backoff}}, got {rejection_backoff!r}."
        )
    backoff = parse_duration(cfg.get("backoff", "1m"))
    max_backoff = parse_duration(cfg.get("max_backoff", "30m"))
    if backoff <= 0 or max_backoff < backoff:
        raise ValueError(
            f"intervention.rejection_backoff needs 0 < backoff <= max_backoff; got {rejection_backoff!r}."
        )
    return (backoff, max_backoff)


def parse_target_interval(target_interval) -> tuple:
    """`intervention.target_interval: {min_interval, max_interval}` -> the
    (min, max) seconds to wait between consecutive intervention targets,
    drawn uniformly per gap. Absent means (0, 0): no pacing, the original
    back-to-back behavior.

    Pacing exists because a few hundred mutes fired back to back is both a
    rate-limit risk (a failed target is only retried for ~35s) and an
    obvious automation signal.
    """
    if not target_interval:
        return (0.0, 0.0)
    if not isinstance(target_interval, dict):
        raise ValueError(
            f"intervention.target_interval must be {{min_interval, max_interval}}, got {target_interval!r}."
        )
    min_interval = parse_duration(target_interval.get("min_interval", 0))
    max_interval = parse_duration(target_interval.get("max_interval", min_interval))
    if min_interval < 0 or max_interval < min_interval:
        raise ValueError(
            f"intervention.target_interval needs 0 <= min_interval <= max_interval; got {target_interval!r}."
        )
    return (min_interval, max_interval)
