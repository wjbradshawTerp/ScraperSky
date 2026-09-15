"""Deterministic, namespaced RNG shared by everything that needs a
reproducible draw.

Lives in utils/ rather than orchestrator/ because cold-start follow
selection (src/scraper/twitter.py) needs it too, and a scraper importing
from the orchestrator would invert the dependency -- the orchestrator
drives scrapers, not the other way round.
"""

import random


def seeded_rng(seed, *namespace_parts) -> random.Random:
    """A Random instance seeded deterministically from a base seed plus a
    namespace (e.g. account_name, "intervention") -- so different uses of
    randomness within one experiment (treatment assignment vs. intervention
    target sampling vs. cold-start follows) never share a draw sequence,
    while all remaining reproducible from the one recorded seed.
    """
    key = "|".join([str(seed)] + [str(part) for part in namespace_parts])
    return random.Random(key)
