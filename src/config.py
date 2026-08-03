import os
import yaml
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # Secrets: real environment variables only (.env), never config.yaml --
    # that file is meant to be safe to share/commit.
    TWITTER_AUTH_TOKEN = os.getenv("TWITTER_AUTH_TOKEN")
    TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
    TWITTER_CSRF_TOKEN = os.getenv("TWITTER_CSRF_TOKEN")

    # Deployment plumbing: paths tied to the Docker volume mount, which
    # differ per machine/deployment rather than per experiment. See
    # docker-compose.yml (HOST_OUTPUT_DIR/CONTAINER_OUTPUT_DIR feed the
    # volume mapping; OUTPUT_DIR is what the app itself reads).
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/app/data")
    CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")

    def __init__(self):
        self._config = self._load_config()
        # Behavioral/experiment settings: sourced from config.yaml, not
        # .env -- see config.yaml for what each of these does.
        self.SCROLL_DELAY = float(self._config.get("scroll_delay", 2))
        self.FETCH_MAX_RETRIES = int(self._config.get("fetch_max_retries", 5))
        self.FETCH_RETRY_BACKOFF = float(self._config.get("fetch_retry_backoff", 5))
        self.TIMEZONE = self._config.get("timezone", "America/New_York")
        self.PLATFORM = self._config.get("platform")
        data_collection = self._config.get("data_collection") or {}
        self.DATA_COLLECTION_TARGETS = data_collection.get("targets") or []
        self.SEARCH_QUERY = data_collection.get("search_query")
        self.ACTIONS = self._config.get("actions") or {}

    def _load_config(self):
        if not os.path.exists(self.CONFIG_PATH):
            return {}
        with open(self.CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def get_account_list_path(self, name: str) -> str:
        """Resolves a named entry under `account_lists` in config.yaml to a
        filesystem path (relative entries are resolved against the config
        file's own directory, so it works the same whether run in Docker or
        locally).
        """
        account_lists = self._config.get("account_lists", {})
        if name not in account_lists:
            raise KeyError(
                f"No account list named '{name}' in {self.CONFIG_PATH}. "
                f"Available: {list(account_lists)}"
            )
        path = account_lists[name]
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(self.CONFIG_PATH)), path)
        return path

    def validate(self):
        missing_env = [
            name for name in ("TWITTER_AUTH_TOKEN", "TWITTER_BEARER_TOKEN", "TWITTER_CSRF_TOKEN")
            if not getattr(self, name)
        ]
        missing_config = [
            key for key, name in (("platform", "PLATFORM"),)
            if not getattr(self, name)
        ]
        if not self.DATA_COLLECTION_TARGETS:
            missing_config.append("data_collection.targets")
        problems = []
        if missing_env:
            problems.append(f"missing .env variable(s): {', '.join(missing_env)}")
        if missing_config:
            problems.append(f"missing {self.CONFIG_PATH} key(s): {', '.join(missing_config)}")
        if problems:
            raise EnvironmentError("; ".join(problems))

settings = Settings()
