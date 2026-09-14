import os
import yaml
from dotenv import load_dotenv

load_dotenv()

# `data_collection.targets` entries that are derived output streams rather
# than platform feeds to observe (paper section 4's Data Streams list:
# "Engagement History", "Intervention History"). See Account.__init__.
DATA_STREAM_TARGETS = ("engagement_log", "intervention_history")


class Account:
    """One sockpuppet account's credentials plus its resolved behavioral
    config -- every key in config.yaml acts as a default, shallow-merged
    with any per-account override of the same name in accounts.yaml (an
    account can override just `targets` and still inherit `search_query`,
    for example; same idea for the scalar keys like `scroll_delay`).
    """

    def __init__(
        self, name, auth_token, bearer_token, csrf_token, platform,
        scroll_delay, fetch_max_retries, fetch_retry_backoff,
        empty_batch_backoff_base, empty_batch_backoff_max, empty_batch_backoff_jitter, timezone,
        data_collection, actions, account_lists, sockpuppet_config,
        experiment_design, intervention, config_dir,
    ):
        self.name = name
        self.auth_token = auth_token
        self.bearer_token = bearer_token
        self.csrf_token = csrf_token
        self.platform = platform
        self.scroll_delay = scroll_delay
        self.fetch_max_retries = fetch_max_retries
        self.fetch_retry_backoff = fetch_retry_backoff
        self.empty_batch_backoff_base = empty_batch_backoff_base
        self.empty_batch_backoff_max = empty_batch_backoff_max
        self.empty_batch_backoff_jitter = empty_batch_backoff_jitter
        self.timezone = timezone
        # `data_collection.targets` mixes two different kinds of thing in
        # the paper's schema (section 3.1): platform feeds that get
        # OBSERVED each activation (for_you_feed/home_timeline -> our
        # home/following, plus search), and derived data STREAMS the
        # framework writes out (engagement_log, intervention_history).
        # Only the former are fetched by observe(); splitting them here
        # keeps that distinction out of every consumer.
        configured_targets = data_collection.get("targets") or []
        self.targets = [t for t in configured_targets if t not in DATA_STREAM_TARGETS]
        self.data_streams = [t for t in configured_targets if t in DATA_STREAM_TARGETS]
        self.search_query = data_collection.get("search_query")
        self.logging_level = data_collection.get("logging", "full")
        self.observation_frequency = data_collection.get("observation_frequency", "every_visit")
        self.actions = actions
        self.sockpuppet_config = sockpuppet_config
        self.experiment_design = experiment_design
        self.intervention = intervention
        self._account_lists = account_lists
        self._config_dir = config_dir

    @property
    def initialization_params(self):
        """Cold-start stopping criteria (roadmap Phase 4a), or None if this
        account has no `sockpuppet_config.initialization_params` configured.
        """
        return self.sockpuppet_config.get("initialization_params")

    @property
    def cold_start_account_lists(self):
        """Names (from `account_lists`) to draw cold-start follow/engagement
        candidates from.
        """
        return self.sockpuppet_config.get("account_lists") or []

    @property
    def persona_prompt_path(self):
        return self.sockpuppet_config.get("persona_prompt")

    @property
    def agent_runtime_enabled(self) -> bool:
        """Whether this account runs the LLM-driven Agent Runtime decision
        cycle (roadmap Phase 4) instead of the old scripted single-target
        observation loop -- gated on a persona prompt being configured,
        since the runtime has nothing to construct a decision prompt from
        without one.
        """
        return bool(self.persona_prompt_path)

    @property
    def experiment_id(self) -> str:
        name = self.experiment_design.get("experiment_name") or "unnamed_experiment"
        return f"{name}::{self.name}"

    @property
    def experiment_phase(self) -> str:
        """Current experiment phase for this account, when NOT opted into
        the Experiment Orchestrator (roadmap Phase 5) -- i.e. `treatment_arms`
        below is empty. An account with `treatment_arms` set gets its phase
        from the shared ExperimentOrchestrator instead (real state machine,
        not this static config read) -- see TwitterScraper._build_experiment_context.
        """
        return self.sockpuppet_config.get("phase", "pre_treatment")

    @property
    def treatment_arm(self):
        """Static treatment-arm override, when NOT opted into the
        Experiment Orchestrator (roadmap Phase 5) -- see `experiment_phase`
        above for the same distinction. An opted-in account's real,
        randomized assignment comes from the orchestrator instead.
        """
        return self.sockpuppet_config.get("treatment_arm")

    @property
    def agent_id(self) -> str:
        """Stable identifier for this sockpuppet within an experiment (paper
        section 3.4, account provisioning: each account is assigned an
        `agent_id`/`persona_prompt`/`experiment_id`/`initialization_params`).
        Defaults to the account name -- which is already unique per
        accounts.yaml -- so an experiment only needs to set this explicitly
        when its analysis expects some other identifier scheme.
        """
        return self.sockpuppet_config.get("agent_id") or self.name

    @property
    def action_rate_limits(self) -> dict:
        """Per-action pacing caps, e.g.
        `{"retweet": {"max_per_hour": 4, "min_interval": "8m"}}`.

        Sized from live observation, not a documented platform limit -- see
        ROADMAP.md's retweet-reliability entry. An action with no entry here
        is uncapped (the platform's own limits still apply).
        """
        return self.sockpuppet_config.get("action_rate_limits") or {}

    @property
    def randomization_method(self) -> str:
        return (self.experiment_design.get("randomization") or {}).get("method")

    @property
    def recovery_policy(self) -> dict:
        """Failure-recovery policy (paper section 3.4 responsibility #6:
        "the orchestrator records the failure and reschedules subsequent
        activations according to the configured recovery policy"). Empty
        means the built-in defaults apply -- see
        ExperimentOrchestrator.record_activation_failure.
        """
        return self.experiment_design.get("recovery_policy") or {}

    @property
    def treatment_arms(self) -> list:
        """The experiment's configured arms (e.g. ["control", "treatment"]).
        Setting this is what opts an account INTO the shared Experiment
        Orchestrator (roadmap Phase 5) -- see main.py's opt-in gate. Empty
        (the default) means this account is entirely unaffected by any
        orchestrator that may exist for sibling accounts in the same
        process.
        """
        return self.experiment_design.get("treatment_arms") or []

    def get_persona_prompt_text(self) -> str:
        path = self.persona_prompt_path
        if not path:
            raise ValueError(f"Account '{self.name}' has no sockpuppet_config.persona_prompt configured.")
        if not os.path.isabs(path):
            path = os.path.join(self._config_dir, path)
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    def get_account_list_path(self, name: str) -> str:
        """Resolves a named entry under this account's `account_lists` to a
        filesystem path (relative entries are resolved against config.yaml's
        own directory, same as the global default did before multi-account).
        """
        if name not in self._account_lists:
            raise KeyError(
                f"No account list named '{name}' for account '{self.name}'. "
                f"Available: {list(self._account_lists)}"
            )
        path = self._account_lists[name]
        if not os.path.isabs(path):
            path = os.path.join(self._config_dir, path)
        return path


class Settings:
    # Deployment plumbing: paths tied to the Docker volume mount, which
    # differ per machine/deployment rather than per experiment. See
    # docker-compose.yml (HOST_OUTPUT_DIR/CONTAINER_OUTPUT_DIR feed the
    # volume mapping; OUTPUT_DIR is what the app itself reads).
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/app/data")
    CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")
    # Per-account credentials + overrides live here, not in .env -- see
    # accounts.yaml.example. Gitignored like .env, since it holds secrets.
    ACCOUNTS_PATH = os.getenv("ACCOUNTS_PATH", "accounts.yaml")

    def __init__(self):
        self._config = self._load_yaml(self.CONFIG_PATH)
        # Defaults for every per-account-overridable key (see Account
        # above) -- sourced from config.yaml, not .env.
        self._default_platform = self._config.get("platform")
        self._default_scroll_delay = float(self._config.get("scroll_delay", 2))
        self._default_fetch_max_retries = int(self._config.get("fetch_max_retries", 5))
        self._default_fetch_retry_backoff = float(self._config.get("fetch_retry_backoff", 5))
        self._default_empty_batch_backoff_base = float(self._config.get("empty_batch_backoff_base", 5))
        self._default_empty_batch_backoff_max = float(self._config.get("empty_batch_backoff_max", 300))
        self._default_empty_batch_backoff_jitter = float(self._config.get("empty_batch_backoff_jitter", 0.2))
        self._default_timezone = self._config.get("timezone", "America/New_York")
        self._default_data_collection = self._config.get("data_collection") or {}
        self._default_actions = self._config.get("actions") or {}
        self._default_account_lists = self._config.get("account_lists") or {}
        self._default_sockpuppet_config = self._config.get("sockpuppet_config") or {}
        self._default_experiment_design = self._config.get("experiment_design") or {}
        self._default_intervention = self._config.get("intervention") or {}

    def _load_yaml(self, path):
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def load_accounts(self) -> list[Account]:
        """Parses ACCOUNTS_PATH into one Account per entry, merging each
        entry's overrides on top of config.yaml's defaults.
        """
        raw = self._load_yaml(self.ACCOUNTS_PATH)
        entries = raw.get("accounts") or []
        if not entries:
            raise EnvironmentError(
                f"No accounts defined in {self.ACCOUNTS_PATH}. See accounts.yaml.example."
            )

        config_dir = os.path.dirname(os.path.abspath(self.CONFIG_PATH))
        accounts = []
        seen_names = set()

        for entry in entries:
            name = entry.get("name")
            if not name:
                raise ValueError(f"Every entry in {self.ACCOUNTS_PATH} needs a 'name'.")
            if name in seen_names:
                raise ValueError(f"Duplicate account name '{name}' in {self.ACCOUNTS_PATH}.")
            seen_names.add(name)

            missing = [
                key for key in ("auth_token", "bearer_token", "csrf_token")
                if not entry.get(key)
            ]
            if missing:
                raise ValueError(
                    f"Account '{name}' in {self.ACCOUNTS_PATH} is missing: {', '.join(missing)}"
                )

            accounts.append(Account(
                name=name,
                auth_token=entry["auth_token"],
                bearer_token=entry["bearer_token"],
                csrf_token=entry["csrf_token"],
                platform=entry.get("platform", self._default_platform),
                scroll_delay=float(entry.get("scroll_delay", self._default_scroll_delay)),
                fetch_max_retries=int(entry.get("fetch_max_retries", self._default_fetch_max_retries)),
                fetch_retry_backoff=float(entry.get("fetch_retry_backoff", self._default_fetch_retry_backoff)),
                empty_batch_backoff_base=float(
                    entry.get("empty_batch_backoff_base", self._default_empty_batch_backoff_base)
                ),
                empty_batch_backoff_max=float(
                    entry.get("empty_batch_backoff_max", self._default_empty_batch_backoff_max)
                ),
                empty_batch_backoff_jitter=float(
                    entry.get("empty_batch_backoff_jitter", self._default_empty_batch_backoff_jitter)
                ),
                timezone=entry.get("timezone", self._default_timezone),
                data_collection={**self._default_data_collection, **(entry.get("data_collection") or {})},
                actions={**self._default_actions, **(entry.get("actions") or {})},
                account_lists={**self._default_account_lists, **(entry.get("account_lists") or {})},
                sockpuppet_config={**self._default_sockpuppet_config, **(entry.get("sockpuppet_config") or {})},
                experiment_design={**self._default_experiment_design, **(entry.get("experiment_design") or {})},
                intervention={**self._default_intervention, **(entry.get("intervention") or {})},
                config_dir=config_dir,
            ))

        return accounts

    def validate(self):
        problems = []
        if not os.path.exists(self.ACCOUNTS_PATH):
            problems.append(
                f"missing accounts file: {self.ACCOUNTS_PATH} (copy accounts.yaml.example and fill in credentials)"
            )
        if problems:
            raise EnvironmentError("; ".join(problems))

settings = Settings()
