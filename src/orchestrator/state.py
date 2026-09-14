import datetime
import json
import os
import re
import threading


def _slugify(name: str) -> str:
    """Filesystem-safe slug for an experiment_name, since it's free-text
    YAML today -- anything outside [a-zA-Z0-9_-] becomes '_'.
    """
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", name or "")
    return slug or "unnamed_experiment"


class ExperimentState:
    """Persists global, cross-account experiment state -- treatment
    assignments, the current phase, and intervention status -- shared by
    every opted-in account's thread via ExperimentOrchestrator (roadmap
    Phase 5).

    Unlike AgentState (one instance, one owning thread, no locking needed),
    this object is touched concurrently by every orchestrated account's
    thread, so every mutation is guarded by a lock, and writes are atomic
    (write-to-temp then os.replace) -- a torn write here would corrupt
    randomization/intervention history that's much harder to reconstruct
    than a per-account AgentState file.
    """

    def __init__(self, output_dir, experiment_name):
        self.path = os.path.join(
            output_dir, "state", "_experiments", _slugify(experiment_name), "orchestrator_state.json"
        )
        self._lock = threading.Lock()
        self.experiment_name = experiment_name
        self.created_at = None
        self.randomization_seed = None
        self.phase = None
        self.phase_entered_at = None
        self.participating_accounts = []
        self.treatment_assignments = {}
        self.interventions = {}
        self.experiment_status = "not_started"
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.experiment_name = data.get("experiment_name", self.experiment_name)
        self.created_at = data.get("created_at")
        self.randomization_seed = data.get("randomization_seed")
        self.phase = data.get("phase")
        self.phase_entered_at = data.get("phase_entered_at")
        self.participating_accounts = data.get("participating_accounts", [])
        self.treatment_assignments = data.get("treatment_assignments", {})
        self.interventions = data.get("interventions", {})
        self.experiment_status = data.get("experiment_status", "not_started")

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "experiment_name": self.experiment_name,
                    "created_at": self.created_at,
                    "randomization_seed": self.randomization_seed,
                    "phase": self.phase,
                    "phase_entered_at": self.phase_entered_at,
                    "participating_accounts": self.participating_accounts,
                    "treatment_assignments": self.treatment_assignments,
                    "interventions": self.interventions,
                    "experiment_status": self.experiment_status,
                },
                f,
                indent=2,
            )
        os.replace(tmp_path, self.path)

    def initialize_if_new(self, participating_accounts, randomization_seed, starting_phase):
        """First-ever construction for this experiment_name: records the
        account roster/seed/starting phase and marks it running. A no-op
        (state already persisted from a prior run) on every subsequent
        restart -- callers must not assume this always writes, and must
        read back whatever was actually persisted (e.g. `randomization_seed`
        below) rather than trusting the value they just passed in, since a
        restart with a changed config must not silently reassign an
        already-committed experiment.
        """
        with self._lock:
            if self.experiment_status != "not_started":
                return
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self.created_at = now
            self.randomization_seed = randomization_seed
            self.participating_accounts = list(participating_accounts)
            self.phase = starting_phase
            self.phase_entered_at = now
            self.experiment_status = "running"
            self._save()

    def get_randomization_seed(self):
        with self._lock:
            return self.randomization_seed

    def get_participating_accounts(self) -> list:
        with self._lock:
            return list(self.participating_accounts)

    def has_all_assignments(self, account_names) -> bool:
        with self._lock:
            return all(name in self.treatment_assignments for name in account_names)

    def set_treatment_assignments(self, assignments: dict):
        """Persists a full assignment map in one shot -- only ever called
        once, immediately after `initialize_if_new`, when
        `has_all_assignments` was False. Uses setdefault per account so a
        partial prior write (e.g. a crash between this call and the last
        one) can never clobber an assignment that already landed.
        """
        with self._lock:
            for name, arm in assignments.items():
                self.treatment_assignments.setdefault(name, arm)
            self._save()

    def get_treatment_arm(self, account_name):
        with self._lock:
            return self.treatment_assignments.get(account_name)

    def get_phase(self) -> str:
        with self._lock:
            return self.phase

    def get_phase_entered_at(self):
        with self._lock:
            return self.phase_entered_at

    def transition_phase(self, new_phase) -> str:
        with self._lock:
            old_phase = self.phase
            self.phase = new_phase
            self.phase_entered_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._save()
            return old_phase

    def get_experiment_status(self) -> str:
        with self._lock:
            return self.experiment_status

    def mark_experiment_completed(self):
        with self._lock:
            self.experiment_status = "completed"
            self._save()

    def set_paused(self, paused: bool) -> str:
        """Paper section 3.4 lists `experiment_status` as running / paused /
        completed. Pausing freezes phase transitions and stops agents acting
        without ending the experiment; a completed experiment can't be
        un-completed by pausing/resuming it.
        """
        with self._lock:
            if self.experiment_status == "completed":
                return self.experiment_status
            self.experiment_status = "paused" if paused else "running"
            self._save()
            return self.experiment_status

    def get_intervention_status(self, account_name) -> str:
        with self._lock:
            return self.interventions.get(account_name, {}).get("status", "not_applicable")

    def mark_intervention_pending(self, account_name):
        """Idempotent -- only sets "pending" if this account has no
        intervention record yet, so a re-tick after a transition (or a
        process restart landing mid-phase) never resets an already
        running/completed intervention back to pending.
        """
        with self._lock:
            if account_name not in self.interventions:
                self.interventions[account_name] = {"status": "pending"}
                self._save()

    def claim_intervention(self, account_name) -> bool:
        """Atomically claims this account's intervention for execution --
        True if the caller won the claim (status was "pending", now
        "running"), False otherwise (already running/completed, or not
        pending). Must be called and return True before any
        execute_action() calls, so two callers (e.g. a late
        register_scraper() racing the tick loop) can never both run the
        same account's intervention.
        """
        with self._lock:
            current = self.interventions.get(account_name, {}).get("status")
            if current != "pending":
                return False
            self.interventions[account_name] = {"status": "running"}
            self._save()
            return True

    def complete_intervention(self, account_name, results):
        with self._lock:
            self.interventions[account_name] = {
                "status": "completed",
                "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "results": results,
            }
            self._save()

    def fail_intervention(self, account_name, error):
        """Terminal failure status -- e.g. an unexpected exception escaped
        `_run_intervention` entirely (not just one target's execute_action
        failing, which is recorded per-target in `complete_intervention`'s
        `results` instead). Must still be a *terminal* status: leaving an
        account stuck at "running" would permanently block every future
        phase transition (see ExperimentOrchestrator._interventions_settled).
        """
        with self._lock:
            self.interventions[account_name] = {
                "status": "failed",
                "error": error,
                "failed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            self._save()

    def reset_intervention_to_pending(self, account_name):
        """A persisted "running" status can only mean the process was
        killed mid-intervention -- claim, execute, and complete/fail all
        happen within one synchronous call in this process, so "running"
        never legitimately survives a restart. Resetting it to "pending"
        lets it be retried instead of staying stuck forever.
        """
        with self._lock:
            self.interventions[account_name] = {"status": "pending"}
            self._save()
