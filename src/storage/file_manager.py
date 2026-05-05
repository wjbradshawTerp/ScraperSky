from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json
from config import settings

LOCAL_TZ = ZoneInfo(settings.TIMEZONE)


# This class manages file creation and data saving for scraped data, organizing files by date and platform.
class FileManager:
    def __init__(self, base_output_dir, platform, mode):
        self.base_output_dir = Path(base_output_dir)
        self.platform = platform
        self.run_id = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
        self.metadata = {
            "run_id": self.run_id,
            "platform": platform,
            "mode": mode,
        }
        self.current_date = None
        self.current_file = None

    def _get_today_str(self):
        return datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")

    def _ensure_file(self):
        today = self._get_today_str()

        # If its a first entry or the date has changed, generate a new file
        if self.current_date != today:
            self.current_date = today

            output_dir = self.base_output_dir / self.current_date / self.platform
            output_dir.mkdir(parents=True, exist_ok=True)

            filename = f"run_{self.run_id}.jsonl"
            self.current_file = output_dir / filename

        return self.current_file

    def save_data(self, data):
        output_file = self._ensure_file()

        record = {
            "metadata": self.metadata,
            "timestamp": datetime.now(LOCAL_TZ).isoformat(),
            "data": data,
        }

        with open(output_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return output_file
