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


def parse_selection_rule(selection_rule) -> float:
    """`random70` -> 0.7, `all` -> 1.0 (paper section 3.1: "all candidate
    accounts may be selected, or a fixed proportion (e.g., 70%) may be
    randomly sampled independently for each sockpuppet").
    """
    if selection_rule is None:
        raise ValueError(
            "intervention.selection_rule is required -- e.g. 'random70' for a random 70%, or 'all'."
        )
    text = str(selection_rule).strip().lower()
    if text == "all":
        return 1.0
    if text.startswith("random"):
        digits = text[len("random"):]
        if digits.isdigit() and 0 <= int(digits) <= 100:
            return int(digits) / 100.0
    raise ValueError(
        f"Unsupported intervention.selection_rule {selection_rule!r}; expected 'all' "
        f"or 'randomNN' (e.g. 'random70')."
    )


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
    {action, target_accounts, fraction, trigger_phase, applies_to_arms}.

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
        "trigger_phase": parse_execution_time(intervention_cfg.get("execution_time")),
        "applies_to_arms": list(applies_to_arms),
    }
