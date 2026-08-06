import os
import yaml
from dotenv import load_dotenv

load_dotenv()


class Account:
    """One sockpuppet account's credentials plus its resolved behavioral
    config -- every key in config.yaml acts as a default, shallow-merged
    with any per-account override of the same name in accounts.yaml (an
    account can override just `targets` and still inherit `search_query`,
    for example; same idea for the scalar keys like `scroll_delay`).
    """

    def __init__(
        self, name, auth_token, bearer_token, csrf_token, platform,
        scroll_delay, fetch_max_retries, fetch_retry_backoff, timezone,
        data_collection, actions, account_lists, config_dir,
    ):
        self.name = name
        self.auth_token = auth_token
        self.bearer_token = bearer_token
        self.csrf_token = csrf_token
        self.platform = platform
        self.scroll_delay = scroll_delay
        self.fetch_max_retries = fetch_max_retries
        self.fetch_retry_backoff = fetch_retry_backoff
        self.timezone = timezone
        self.targets = data_collection.get("targets") or []
        self.search_query = data_collection.get("search_query")
        self.actions = actions
        self._account_lists = account_lists
        self._config_dir = config_dir

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
        self._default_timezone = self._config.get("timezone", "America/New_York")
        self._default_data_collection = self._config.get("data_collection") or {}
        self._default_actions = self._config.get("actions") or {}
        self._default_account_lists = self._config.get("account_lists") or {}

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
                timezone=entry.get("timezone", self._default_timezone),
                data_collection={**self._default_data_collection, **(entry.get("data_collection") or {})},
                actions={**self._default_actions, **(entry.get("actions") or {})},
                account_lists={**self._default_account_lists, **(entry.get("account_lists") or {})},
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
