import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    TWITTER_AUTH_TOKEN = os.getenv("TWITTER_AUTH_TOKEN")
    TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN")
    TWITTER_CSRF_TOKEN = os.getenv("TWITTER_CSRF_TOKEN")
    SCROLL_DELAY = float(os.getenv("SCROLL_DELAY", 2))
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/app/data")
    TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
    PLATFORM = os.getenv("PLATFORM")
    MODE = os.getenv("MODE")
    FOLLOW_LIST_PATH = os.getenv("FOLLOW_LIST_PATH", "/app/follow_user_ids.json")

    def validate(self):
        missing = [
            name for name in ("TWITTER_AUTH_TOKEN", "TWITTER_BEARER_TOKEN", "TWITTER_CSRF_TOKEN", "PLATFORM", "MODE")
            if not getattr(self, name)
        ]
        if missing:
            raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")

settings = Settings()
