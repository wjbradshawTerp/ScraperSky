from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json


# This class manages file creation and data saving for scraped data, organizing files by date, platform, and account.
class FileManager:
    def __init__(self, base_output_dir, platform, mode, account_name, timezone, stream=None):
        self.base_output_dir = Path(base_output_dir)
        self.platform = platform
        self.account_name = account_name
        self.local_tz = ZoneInfo(timezone)
        self.run_id = datetime.now(self.local_tz).strftime("%Y%m%d-%H%M%S")
        # `stream` disambiguates the filename when an account has more than
        # one FileManager writing concurrently (e.g. platform observations
        # vs. Agent Runtime logs -- roadmap Phase 4/6). None preserves the
        # original filename exactly for existing single-stream callers.
        self.stream = stream
        self.metadata = {
            "run_id": self.run_id,
            "platform": platform,
            "mode": mode,
            "account": account_name,
        }
        self.current_date = None
        self.current_file = None

    def _get_today_str(self):
        return datetime.now(self.local_tz).strftime("%Y-%m-%d")

    def _ensure_file(self):
        today = self._get_today_str()

        # If its a first entry or the date has changed, generate a new file
        if self.current_date != today:
            self.current_date = today

            # Namespaced by account so concurrent accounts' JSONL output
            # never collides (see roadmap Phase 3 -- multi-account support).
            output_dir = self.base_output_dir / self.current_date / self.platform / self.account_name
            output_dir.mkdir(parents=True, exist_ok=True)

            suffix = f"_{self.stream}" if self.stream else ""
            filename = f"run_{self.run_id}{suffix}.jsonl"
            self.current_file = output_dir / filename

        return self.current_file

    def save_data(self, data):
        output_file = self._ensure_file()

        record = {
            "metadata": self.metadata,
            "timestamp": datetime.now(self.local_tz).isoformat(),
            "data": data,
        }

        with open(output_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return output_file
