import datetime
import json
import threading
import time

from orchestrator import randomization, spec
from orchestrator.log import ReproducibilityLog
from orchestrator.state import ExperimentState
from utils.duration import parse_duration
from utils.rate_budget import build_budgets

# Same values as twitter.py's per-account throttle (see ROADMAP.md's
# rate-limit note) -- this is the shared, cross-account budget that
# supersedes it for orchestrated accounts (roadmap Phase 5 responsibility
# #2: "must also respect the 15/15min follow rate limit across
# concurrently-active agents").
FOLLOW_RATE_LIMIT = 15
FOLLOW_RATE_WINDOW_SECONDS = 15 * 60

# Collapses the paper's "init/pre-treatment" into a single starting phase,
# since cold-start's own "initialization" phase (per-account,
# agent_state-driven, unrelated to this state machine) already covers the
# "init" half. A phase with no matching `phase_durations` entry never
# auto-transitions -- the experiment just stays there indefinitely.
PHASE_SEQUENCE = ["pre_treatment", "treatment", "post_treatment"]

# "Selected actions" for a phase's max_action_count exit condition --
# real, agent-selected actions only. no_action is deliberately excluded:
# it's the absence of an action, not one, and counting it would make the
# threshold measure activation frequency rather than actual engagement.
REAL_ACTIONS = ("like", "retweet", "follow", "mute")


class ExperimentOrchestrator:
    """Shared, thread-safe coordinator for every account that opts into a
    formal experiment (`experiment_design.treatment_arms` configured),
    roadmap Phase 5. One instance per process, constructed once in
    main.py and passed into each opted-in account's TwitterScraper --
    every account keeps running its own already-verified
    run_cold_start()/run_agent_runtime() loop in its own thread unchanged;
    those loops just consult this object instead of static per-account
    config for phase/treatment-arm/follow-budget/logging.

    An account with no `treatment_arms` configured never receives a
    reference to this object at all (see main.py's opt-in gate) -- so
    nothing here can affect it, even if a global orchestrator exists for
    sibling accounts in the same process.
    """

    def __init__(
        self, experiment_name, participating_accounts, experiment_design,
        intervention_by_account, output_dir, timezone, check_interval_seconds=60,
        agent_id_by_account=None, action_rate_limits_by_account=None,
    ):
        self.experiment_name = experiment_name
        self.experiment_design = experiment_design
        # Normalized from the paper's published intervention schema -- see
        # orchestrator/spec.py. {account_name: normalized dict | None}.
        self.intervention_by_account = {
            name: spec.parse_intervention(cfg) for name, cfg in (intervention_by_account or {}).items()
        }
        self.check_interval_seconds = check_interval_seconds
        self.agent_id_by_account = agent_id_by_account or {}
        self.recovery_policy = experiment_design.get("recovery_policy") or {}

        self.state = ExperimentState(output_dir, experiment_name)
        self.repro_log = ReproducibilityLog(output_dir, timezone)

        randomization_cfg = experiment_design.get("randomization") or {}
        self.arms = experiment_design.get("treatment_arms") or []
        self.ratios = randomization_cfg.get("ratios")
        # Rejects an unsupported procedure outright rather than silently
        # running simple random assignment under a different label.
        self.randomization_method = spec.parse_randomization_method(randomization_cfg.get("method"))

        self.state.initialize_if_new(participating_accounts, randomization_cfg.get("seed", 0), PHASE_SEQUENCE[0])
        self.participating_accounts = self.state.get_participating_accounts()

        # `initialization: adaptive` is skipped rather than parsed: the
        # paper's initialization phase "may terminate adaptively once
        # predefined stopping criteria are satisfied" (section 3.1), i.e.
        # it's governed by sockpuppet_config.initialization_params and
        # cold-start's own loop, not by this phase state machine.
        self.phase_exit_conditions = {
            phase: self._parse_phase_exit_condition(value)
            for phase, value in (experiment_design.get("phase_durations") or {}).items()
            if str(value).strip().lower() != spec.ADAPTIVE_PHASE_DURATION
        }

        self._follow_lock = threading.Lock()
        # Keyed per account, NOT one pooled window. X enforces the follow
        # limit per authenticated account, so a pooled budget both wasted
        # each account's real allowance (15/15min split N ways) and -- worse
        # -- made one agent's follows block another's, which is interference
        # between experimental units in exactly the quantity this study
        # measures. Matches how sockpuppet_config.action_rate_limits already
        # works. See ROADMAP's 2026-09-14 entry.
        self._follow_timestamps = {}

        self._scrapers_lock = threading.Lock()
        self._scrapers = {}

        self._failures_lock = threading.Lock()
        self._consecutive_failures = {}

        # Per-account, per-action pacing budgets (sockpuppet_config
        # .action_rate_limits). Per-account rather than shared, because the
        # limits these pace against are per-account on the platform --
        # unlike the follow budget above, which is shared precisely because
        # its limit appears to apply across concurrently-active agents.
        self._action_budgets = {
            (account_name, action): budget
            for account_name, limits in (action_rate_limits_by_account or {}).items()
            for action, budget in build_budgets(limits).items()
        }

        # Serializes transition evaluation. Both the tick thread and any
        # agent thread (via maybe_advance_phase) can evaluate a transition,
        # and two of them passing the exit-condition check concurrently
        # would otherwise advance the phase twice.
        self._transition_lock = threading.Lock()

        self._stop_event = threading.Event()
        self._thread = None

        # Treatment assignment belongs at the END of pre-treatment (paper
        # section 3.3: "At the conclusion of the pre-treatment period, the
        # Experiment Orchestrator assigns each sockpuppet to an experimental
        # condition"), so it is NOT done here -- see
        # _ensure_treatment_assignments. The exception is resuming an
        # experiment already past pre-treatment, where the assignment should
        # have happened before this process started.
        if self.state.get_phase() != PHASE_SEQUENCE[0]:
            self._ensure_treatment_assignments()

        # A persisted "running" intervention status can only mean a prior
        # process was killed mid-intervention -- reset it so it gets
        # retried, rather than staying stuck forever and permanently
        # blocking every future phase transition (see
        # _interventions_settled).
        self._recover_interrupted_interventions()

        # Covers a process restart landing mid-phase (or after a crash
        # right after a transition): any account whose intervention should
        # already be pending for the current phase gets marked/run now,
        # rather than waiting for the next natural phase transition (which
        # may never come again for that phase).
        self._sync_pending_interventions()

    @staticmethod
    def _parse_phase_exit_condition(value):
        """A `phase_durations` entry is either a bare duration (e.g. "5m",
        "7d", as before) or a dict `{max_duration: ..., max_action_count:
        ...}` so a phase can end on whichever comes first: wall-clock time,
        or a configured number of real (non-no_action) selected actions by
        any one participating account. At least one of the two must be set,
        or the phase would never transition at all.
        """
        if isinstance(value, dict):
            max_duration = value.get("max_duration")
            max_action_count = value.get("max_action_count")
        else:
            max_duration, max_action_count = value, None
        if max_duration is None and max_action_count is None:
            raise ValueError(
                f"phase_durations entry {value!r} must set at least one of "
                f"max_duration/max_action_count."
            )
        return {
            "max_duration": parse_duration(max_duration) if max_duration is not None else None,
            "max_action_count": int(max_action_count) if max_action_count is not None else None,
        }

    def _recover_interrupted_interventions(self):
        for account_name in self.state.get_participating_accounts():
            if self.state.get_intervention_status(account_name) == "running":
                self.state.reset_intervention_to_pending(account_name)

    def _ensure_treatment_assignments(self):
        """Assigns every participating account to an arm, once, and persists
        it (paper section 3.4 responsibility #3: "Assignment results are
        stored permanently within the experiment state"). Idempotent -- a
        restart reuses the committed assignment rather than re-drawing it,
        so a later seed or roster change can't silently reassign an
        already-running experiment.
        """
        accounts = self.state.get_participating_accounts()
        if self.state.has_all_assignments(accounts):
            return
        seed = self.state.get_randomization_seed()
        assignments = randomization.assign_treatment_arms(accounts, self.arms, self.ratios, seed)
        self.state.set_treatment_assignments(assignments)
        for account_name, arm in assignments.items():
            self.repro_log.log_treatment_assignment(
                account_name, arm, seed,
                agent_id=self.agent_id_by_account.get(account_name, account_name),
                method=self.randomization_method,
            )

    def start(self):
        """Starts the phase-transition tick thread. Must be called once,
        from main.py, before any account thread starts -- so a phase due
        to transition immediately (e.g. an already-elapsed pre_treatment
        on restart) is caught before agents start acting under a stale
        phase.
        """
        self._thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._thread.start()

    def _tick_loop(self):
        while not self._stop_event.is_set():
            try:
                self._maybe_transition_phase()
            except Exception as e:
                # A bad tick must never silently kill this thread -- that
                # would permanently freeze every future phase transition
                # (and anything waiting on one, like a still-pending
                # intervention) for the rest of the experiment.
                try:
                    self.repro_log.log_failure("_orchestrator", repr(e), self.state.get_phase())
                except Exception:
                    pass
            time.sleep(self.check_interval_seconds)

    def _maybe_transition_phase(self):
        """Evaluates (and applies) a phase transition. Serialized so two
        threads can't both advance the phase off the same condition.
        """
        with self._transition_lock:
            self._maybe_transition_phase_locked()

    def maybe_advance_phase(self):
        """Opportunistic, non-blocking transition check for an agent to call
        right after it records a real action, so a `max_action_count`
        threshold takes effect immediately instead of waiting up to
        `check_interval_seconds` for the next tick (live testing 2026-09-09
        overshot a threshold of 3 by ~17 actions for exactly that reason).

        Skips silently if another thread is already evaluating a transition
        -- that thread will apply it -- so an agent never blocks here, and
        never raises into its own decision loop.
        """
        if not self._transition_lock.acquire(blocking=False):
            return
        try:
            self._maybe_transition_phase_locked()
        except Exception as e:
            try:
                self.repro_log.log_failure("_orchestrator", repr(e), self.state.get_phase())
            except Exception:
                pass
        finally:
            self._transition_lock.release()

    def _maybe_transition_phase_locked(self):
        status = self.state.get_experiment_status()
        if status == "completed":
            # Either this tick thread just finished the experiment below, or
            # a fresh process started up and found it already completed
            # (persisted from a prior run) -- either way, there is nothing
            # left for this thread to ever do again.
            self._stop_event.set()
            return
        if status == "paused":
            # A paused experiment's clock keeps running (phase_entered_at is
            # wall-clock), but no transition, intervention, or agent action
            # fires until it's resumed -- see is_paused().
            return

        current = self.state.get_phase()
        if current not in PHASE_SEQUENCE:
            return

        # Guarantee: never leave a phase while an intervention it triggered
        # is still pending/running for any participating account, no matter
        # how long that takes -- a long target list, transient failures, or
        # rate limits must never cause a mute (or other intervention
        # action) to be skipped just because the time/action-count
        # condition below was also satisfied.
        if not self._interventions_settled():
            return

        exit_reason = self._phase_exit_reason(current)
        if exit_reason is None:
            return

        idx = PHASE_SEQUENCE.index(current)
        if idx + 1 < len(PHASE_SEQUENCE):
            new_phase = PHASE_SEQUENCE[idx + 1]
            old_phase = self.state.transition_phase(new_phase)
            self.repro_log.log_phase_transition(old_phase, new_phase, reason=exit_reason)
            # Randomization happens HERE, at the end of pre-treatment, not
            # at experiment start (paper section 3.3/3.4) -- so agents run
            # an identical, condition-blind pre-treatment baseline and
            # `treatment_arm` is genuinely unassigned until this moment.
            # Must precede the intervention sync below, which needs each
            # account's arm to decide who receives the intervention.
            if old_phase == PHASE_SEQUENCE[0]:
                self._ensure_treatment_assignments()
            self._sync_pending_interventions()
        else:
            # post_treatment is the last phase -- there's nothing to
            # transition INTO, so its own exit condition (time and/or
            # max_action_count, same as any other phase) marks the
            # EXPERIMENT itself completed instead. This does not stop the
            # account threads themselves (see run_agent_runtime/run()) --
            # that's a separate, larger decision; this only makes the exit
            # condition on post_treatment actually mean something, and frees
            # this now-useless tick thread.
            self.state.mark_experiment_completed()
            self.repro_log.log_termination("_experiment", f"{current} exit condition met ({exit_reason})")
            self._stop_event.set()

    def _interventions_settled(self) -> bool:
        return all(
            self.state.get_intervention_status(name) not in ("pending", "running")
            for name in self.state.get_participating_accounts()
        )

    def _phase_exit_reason(self, phase):
        """Returns which configured condition ended the phase --
        "max_duration" or "max_action_count (<account>)" -- or None if
        neither is met yet. Surfaced in the reproducibility log so it's
        possible to tell, after the fact, which condition actually fired
        rather than just that a transition happened.
        """
        condition = self.phase_exit_conditions.get(phase)
        if condition is None:
            return None

        max_duration = condition["max_duration"]
        if max_duration is not None:
            entered_at = self.state.get_phase_entered_at()
            if entered_at is not None:
                elapsed = (
                    datetime.datetime.now(datetime.timezone.utc)
                    - datetime.datetime.fromisoformat(entered_at)
                ).total_seconds()
                if elapsed >= max_duration:
                    return "max_duration"

        max_action_count = condition["max_action_count"]
        if max_action_count is not None:
            # "Whichever comes first" is evaluated per account, not summed
            # -- the first participating account to reach the threshold
            # ends the phase for everyone, matching how a duration-based
            # phase already ends for every account at once regardless of
            # each one's own activity level.
            for account_name in self.state.get_participating_accounts():
                if self._action_count_for_phase(account_name, phase) >= max_action_count:
                    return f"max_action_count ({account_name})"

        return None

    def _action_count_for_phase(self, account_name, phase) -> int:
        with self._scrapers_lock:
            scraper = self._scrapers.get(account_name)
        if scraper is None:
            return 0
        return scraper.agent_state.count_interactions(phase=phase, actions=REAL_ACTIONS)

    def _sync_pending_interventions(self):
        """Marks pending (and immediately attempts to run) the
        intervention for every participating account whose
        `intervention.trigger_phase` matches the current phase and whose
        treatment arm is in `applies_to_arms`. Safe to call repeatedly --
        `mark_intervention_pending`/`claim_intervention` are both
        idempotent, so a re-tick never re-triggers a completed
        intervention.
        """
        phase = self.state.get_phase()
        for account_name in self.state.get_participating_accounts():
            intervention_cfg = self.intervention_by_account.get(account_name)
            if not intervention_cfg or intervention_cfg.get("trigger_phase") != phase:
                continue
            if self.state.get_treatment_arm(account_name) not in (intervention_cfg.get("applies_to_arms") or []):
                continue
            self.state.mark_intervention_pending(account_name)
            self._maybe_run_now(account_name)

    def register_scraper(self, account_name, scraper):
        """Called by TwitterScraper.run() once its own state is set up, so
        the orchestrator has a live scraper to execute interventions
        through (reusing execute_action() -- no parallel action-execution
        path). Also covers the case where a phase transition already
        marked this account's intervention pending before it finished
        registering (e.g. still in cold-start).
        """
        with self._scrapers_lock:
            self._scrapers[account_name] = scraper
        self._maybe_run_now(account_name)

    def maybe_run_pending_intervention(self, account_name):
        """Cheap, defensive no-op check called once per activation from
        run_agent_runtime()'s loop -- belt-and-suspenders against any
        registration/tick-timing edge case that `register_scraper()` and
        the tick loop's own `_sync_pending_interventions()` don't already
        cover.
        """
        self._maybe_run_now(account_name)

    def _maybe_run_now(self, account_name):
        if self.state.get_intervention_status(account_name) != "pending":
            return
        with self._scrapers_lock:
            scraper = self._scrapers.get(account_name)
        if scraper is None:
            return
        # Claim BEFORE any network calls, so two near-simultaneous callers
        # (register_scraper() racing the tick loop) can never both run it.
        if not self.state.claim_intervention(account_name):
            return
        try:
            self._run_intervention(account_name, scraper)
        except Exception as e:
            # Never leave this account stuck at "running" -- since
            # _interventions_settled() treats "running" as unsettled, that
            # would permanently block every future phase transition. A
            # clear terminal failure is far better than a silent freeze.
            self.state.fail_intervention(account_name, repr(e))
            self.repro_log.log_failure(account_name, f"intervention failed: {e!r}", self.state.get_phase())

    def _run_intervention(self, account_name, scraper):
        intervention_cfg = self.intervention_by_account.get(account_name) or {}
        action = intervention_cfg["action"]
        list_name = intervention_cfg["target_accounts"]
        fraction = intervention_cfg["fraction"]

        path = scraper.account.get_account_list_path(list_name)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        user_ids = [entry["user_id"] for entry in data.get("users", []) if entry.get("user_id")]

        rng = randomization.seeded_rng(self.state.get_randomization_seed(), account_name, "intervention")
        sample_size = min(round(len(user_ids) * fraction), len(user_ids))
        sampled = rng.sample(user_ids, sample_size)

        results = []
        for user_id in sampled:
            # Retries transient failures (network exceptions, rate limits)
            # instead of recording one attempt as final -- a long target
            # list with real downtime between requests must not silently
            # drop targets just because a single attempt didn't land.
            result = self._execute_intervention_target(scraper, action, user_id)
            # execute_action() only updates following_list/muted_accounts
            # bookkeeping on success, not interaction_history (that's only
            # ever populated by run_agent_runtime()'s/run_cold_start()'s own
            # loops) -- recording it here too keeps has_acted_on() and this
            # account's own interaction history complete, so the persona
            # doesn't "re-decide" to act on an already-intervened-on target,
            # and the intervention shows up in that account's own data too.
            scraper.agent_state.record_interaction(
                action, user_id, phase="intervention",
                execution_status=result["execution_status"],
                system_response=result["system_response"],
            )
            results.append({"target": user_id, "execution_status": result["execution_status"]})

        self.state.complete_intervention(account_name, results)
        agent_id = self.agent_id_by_account.get(account_name, account_name)
        self.repro_log.log_intervention_execution(
            account_name, action, list_name, fraction, results, agent_id=agent_id
        )
        # Intervention History is one of the paper's own data streams
        # (section 4), written per-account alongside that account's
        # observations rather than only into the experiment-wide
        # reproducibility log.
        scraper.write_intervention_history({
            "experiment_id": f"{self.experiment_name}::{account_name}",
            "agent_id": agent_id,
            "phase": self.state.get_phase(),
            "treatment_arm": self.state.get_treatment_arm(account_name),
            "action": action,
            "target_accounts": list_name,
            "selection_fraction": fraction,
            "target_count": len(results),
            "results": results,
        })

    @staticmethod
    def _execute_intervention_target(scraper, action, target, max_attempts=4, base_backoff_seconds=5):
        """Guarantees a real attempt for every sampled target: a single
        target's exception (network timeout, etc.) can't abort the rest of
        the batch, and a failure (including a rate limit) gets retried with
        exponential backoff before being recorded as final. Not infinite --
        a permanently invalid target (e.g. a deleted account, a real 404
        seen in prior live testing) shouldn't retry forever, just enough to
        ride out real transient downtime.
        """
        result = {"execution_status": "failure", "system_response": {"reason": "not attempted"}}
        for attempt in range(max_attempts):
            try:
                result = scraper.execute_action(action, target)
            except Exception as e:
                result = {"execution_status": "failure", "system_response": {"reason": repr(e)}}
            if result["execution_status"] == "success":
                return result
            if attempt < max_attempts - 1:
                time.sleep(base_backoff_seconds * (2 ** attempt))
        return result

    def acquire_follow_slot(self, account_name):
        """Blocks until a slot is free in THIS account's sliding-window
        follow budget. Same self-throttle-ahead-of-429 approach as
        twitter.py's `_throttled_follow`, but owned by the orchestrator so
        it covers every follow path (cold-start, follow_all, and
        agent-runtime decisions) rather than only `_throttled_follow`'s
        callers.

        Per account, not pooled across accounts. X enforces the follow
        limit per authenticated account, and our own data only ever
        measured it on a single account, so a pooled window understated
        every account's real allowance. It also let one agent's follows
        block another's -- interference between experimental units, which
        the causal estimate assumes away.

        A restart resets the window, which is safe: it is a conservative
        self-throttle, not a correctness-critical value.
        """
        while True:
            with self._follow_lock:
                now = time.time()
                stamps = [
                    t for t in self._follow_timestamps.get(account_name, [])
                    if now - t < FOLLOW_RATE_WINDOW_SECONDS
                ]
                if len(stamps) < FOLLOW_RATE_LIMIT:
                    stamps.append(now)
                    self._follow_timestamps[account_name] = stamps
                    self.repro_log.log_scheduling_decision(account_name, "follow_slot", "granted")
                    return
                self._follow_timestamps[account_name] = stamps
                wait = FOLLOW_RATE_WINDOW_SECONDS - (now - stamps[0]) + 1
            # Never sleep while holding the lock -- other accounts' slot
            # checks must not block on this one's wait.
            self.repro_log.log_scheduling_decision(account_name, "follow_slot", "waited", wait_seconds=wait)
            time.sleep(wait)

    def should_stop(self) -> bool:
        """True once the experiment has completed (paper section 3.4
        responsibility #5: "the experiment terminates after completion of
        the post-treatment phase"). Every account loop checks this so agents
        stop acting and their threads exit, letting main() shut down cleanly
        instead of running forever past the end of the experiment.
        """
        return self.state.get_experiment_status() == "completed"

    def is_paused(self) -> bool:
        """True while `experiment_status` is "paused" -- agents idle without
        acting or observing, and phases don't advance, until resumed.
        """
        return self.state.get_experiment_status() == "paused"

    def pause(self) -> str:
        status = self.state.set_paused(True)
        self.repro_log.log_scheduling_decision("_experiment", "experiment_status", status)
        return status

    def resume(self) -> str:
        status = self.state.set_paused(False)
        self.repro_log.log_scheduling_decision("_experiment", "experiment_status", status)
        return status

    def get_agent_id(self, account_name) -> str:
        return self.agent_id_by_account.get(account_name, account_name)

    def record_activation_failure(self, account_name, error):
        """Failure recovery (paper section 3.4 responsibility #6): records
        the failure centrally and returns how long this agent should wait
        before its next activation, or None once it has failed too many
        times in a row to keep retrying. Isolated per agent -- one account
        backing off or giving up never touches another's schedule.

        `experiment_design.recovery_policy` tunes it:
        {max_consecutive_failures, backoff, max_backoff}.
        """
        policy = self.recovery_policy
        max_consecutive = int(policy.get("max_consecutive_failures", 5))
        backoff = parse_duration(policy.get("backoff", "1m"))
        max_backoff = parse_duration(policy.get("max_backoff", "30m"))

        with self._failures_lock:
            count = self._consecutive_failures.get(account_name, 0) + 1
            self._consecutive_failures[account_name] = count

        self.repro_log.log_failure(
            account_name, error, self.state.get_phase(),
            agent_id=self.get_agent_id(account_name), consecutive_failures=count,
        )

        if count > max_consecutive:
            self.repro_log.log_termination(
                account_name,
                f"gave up after {count} consecutive activation failures "
                f"(recovery_policy.max_consecutive_failures={max_consecutive})",
            )
            return None
        return min(backoff * (2 ** (count - 1)), max_backoff)

    def record_activation_success(self, account_name):
        """Clears an agent's consecutive-failure streak, so recovery backoff
        measures *consecutive* failures rather than lifetime ones.
        """
        with self._failures_lock:
            self._consecutive_failures.pop(account_name, None)

    def try_consume_action_slot(self, account_name, action) -> tuple:
        """Non-blocking per-account, per-action pacing check
        (`sockpuppet_config.action_rate_limits`). Returns
        (allowed, retry_after_seconds); an action with no configured budget
        is always allowed.

        Unlike `acquire_follow_slot`, this never waits: it's called from
        inside the per-post decision loop, where blocking would stall a
        whole activation. A refused slot becomes a logged `no_action`
        instead, so the agent keeps evaluating the rest of the feed and the
        log records why the action didn't happen.
        """
        budget = self._action_budgets.get((account_name, action))
        if budget is None:
            return True, 0.0
        allowed, retry_after = budget.try_consume()
        if not allowed:
            self.repro_log.log_scheduling_decision(
                account_name, f"{action}_rate_budget", "capped",
                retry_after_seconds=round(retry_after, 1),
                agent_id=self.get_agent_id(account_name),
            )
        return allowed, retry_after

    def get_phase(self, account_name=None) -> str:
        return self.state.get_phase()

    def get_treatment_arm(self, account_name):
        return self.state.get_treatment_arm(account_name)

    def build_experiment_context(self, account_name) -> dict:
        return {
            "experiment_id": f"{self.experiment_name}::{account_name}",
            "phase": self.get_phase(account_name),
            "treatment_arm": self.get_treatment_arm(account_name),
            "intervention_status": self.state.get_intervention_status(account_name),
            "current_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def log_failure(self, account_name, error):
        """Must never raise -- a broken logger must not replace or mask
        the real exception on its way to killing that one account's
        thread, which is the only thing actually stopping it.
        """
        try:
            self.repro_log.log_failure(account_name, error, self.get_phase())
        except Exception:
            pass
