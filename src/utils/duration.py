import re

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)([smhd])$")


def parse_duration(value) -> float:
    """Parses a shorthand duration string (e.g. "15m", "6h", "7d", "30s"),
    as used by `sockpuppet_config.activation_schedule` and (eventually)
    `experiment_design.phase_durations`, into seconds. A bare number
    (int/float) is passed through as already-seconds.
    """
    if isinstance(value, (int, float)):
        return float(value)
    match = _PATTERN.match(str(value).strip())
    if not match:
        raise ValueError(
            f"Invalid duration {value!r}; expected e.g. '15m', '6h', '7d', or a plain number of seconds."
        )
    amount, unit = match.groups()
    return float(amount) * _UNIT_SECONDS[unit]
