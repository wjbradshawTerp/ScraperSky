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


def stratified_sample(ordered_ids, fraction, rng, stratum_size=10) -> list:
    """Stratified draw over an already-ordered list.

    The list's order is the stratification key: it is cut into consecutive
    blocks of `stratum_size`, and floor(stratum_size * fraction) ids are
    drawn from every full block. The shortfall against the overall target
    (round(len * fraction)) is then drawn from the leftover partial block
    first, and only if that is too small, from the not-yet-drawn ids of the
    full blocks. E.g. 434 ids at 0.7 -> 43 blocks x 7 = 301, plus 3 of the
    last 4 = 304.
    """
    ids = list(ordered_ids)
    target = min(round(len(ids) * fraction), len(ids))
    per_block = int(stratum_size * fraction)
    num_blocks = len(ids) // stratum_size

    sampled = []
    for j in range(num_blocks):
        block = ids[j * stratum_size:(j + 1) * stratum_size]
        sampled.extend(rng.sample(block, per_block))

    shortfall = target - len(sampled)
    if shortfall > 0:
        leftover = ids[num_blocks * stratum_size:]
        extra = rng.sample(leftover, min(shortfall, len(leftover)))
        sampled.extend(extra)
        shortfall -= len(extra)
    if shortfall > 0:
        chosen = set(sampled)
        pool = [uid for uid in ids if uid not in chosen]
        sampled.extend(rng.sample(pool, shortfall))
    return sampled


# Re-exported from utils.seeding so there is exactly one implementation --
# cold-start follow selection needs it too, and a scraper importing from
# the orchestrator package would invert the dependency.
seeded_rng = _seeded_rng
