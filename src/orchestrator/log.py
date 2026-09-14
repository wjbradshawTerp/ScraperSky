import datetime

from storage.file_manager import FileManager

# FileManager's `account_name` param is required and feeds the output
# path, but reproducibility-log events aren't owned by any one account --
# this sentinel fills that slot, matching the leading-underscore convention
# ExperimentState's own on-disk path uses for the same reason.
_SENTINEL_ACCOUNT_NAME = "_experiment"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ReproducibilityLog:
    """Records every scheduling decision, treatment assignment,
    intervention execution, phase transition, and failure for an
    experiment (roadmap Phase 5, paper responsibility #7).

    Reuses FileManager's exact JSONL/append-only/envelope convention
    rather than inventing a new logger. `platform="_orchestrator"` (not
    "twitter") since these events aren't platform-specific -- the
    orchestrator is constructed before any platform-specific scraper
    exists, and nesting under "twitter" would be wrong once a second
    platform is added.
    """

    def __init__(self, output_dir, timezone):
        self._file_manager = FileManager(
            output_dir, "_orchestrator", "reproducibility_log",
            _SENTINEL_ACCOUNT_NAME, timezone, stream="reproducibility_log",
        )

    def _write(self, event, **fields):
        self._file_manager.save_data({"event": event, "occurred_at": _now_iso(), **fields})

    def log_scheduling_decision(self, account_name, resource, outcome, **extra):
        self._write("scheduling_decision", account_name=account_name, resource=resource, outcome=outcome, **extra)

    def log_treatment_assignment(self, account_name, arm, seed, **extra):
        self._write("treatment_assignment", account_name=account_name, arm=arm, seed=seed, **extra)

    def log_intervention_execution(self, account_name, action, target_accounts, fraction, results, **extra):
        self._write(
            "intervention_execution", account_name=account_name, action=action,
            target_accounts=target_accounts, selection_fraction=fraction,
            target_count=len(results), results=results, **extra,
        )

    def log_phase_transition(self, from_phase, to_phase, reason=None):
        self._write("phase_transition", from_phase=from_phase, to_phase=to_phase, reason=reason)

    def log_failure(self, account_name, error, phase, **extra):
        self._write("failure", account_name=account_name, error=error, phase=phase, **extra)

    def log_termination(self, account_name, reason):
        self._write("termination", account_name=account_name, reason=reason)
