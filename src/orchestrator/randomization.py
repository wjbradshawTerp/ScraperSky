import random

from utils.seeding import seeded_rng as _seeded_rng


def assign_treatment_arms(account_names, arms, ratios, seed) -> dict:
    """Deterministic, seeded, exact-ratio treatment-arm assignment.

    A single seeded shuffle + slice-by-cumulative-ratio, rather than a
    per-account hash draw, so the split matches `ratios` exactly -- a
    hash-based per-account draw only matches the target ratio in
    expectation, which is a poor guarantee for a small account roster (the
    realistic case here). Returns {account_name: arm}.
    """
    if not arms:
        raise ValueError("experiment_design.treatment_arms must be a non-empty list to assign treatment arms.")
    if not ratios:
        even = 1.0 / len(arms)
        ratios = {arm: even for arm in arms}

    missing = [arm for arm in arms if arm not in ratios]
    if missing:
        raise ValueError(f"experiment_design.randomization.ratios is missing arm(s): {missing}")

    rng = random.Random(seed)
    shuffled = list(account_names)
    rng.shuffle(shuffled)

    n = len(shuffled)
    assignments = {}
    cursor = 0
    cumulative = 0.0
    for i, arm in enumerate(arms):
        cumulative += ratios[arm]
        is_last_arm = i == len(arms) - 1
        # The last arm always absorbs whatever's left, so rounding on the
        # earlier arms' boundaries can't drop or double-count an account.
        end = n if is_last_arm else round(cumulative * n)
        for name in shuffled[cursor:end]:
            assignments[name] = arm
        cursor = end
    return assignments


# Re-exported from utils.seeding so there is exactly one implementation --
# cold-start follow selection needs it too, and a scraper importing from
# the orchestrator package would invert the dependency.
seeded_rng = _seeded_rng
