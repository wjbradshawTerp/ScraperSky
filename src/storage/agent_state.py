import json
import os


class AgentState:
    """Persists lightweight per-account state (currently: who this account
    already follows/mutes) across runs.

    Without this, follow_all() has no memory between container restarts and
    will keep re-submitting follow requests for accounts it already
    follows — repeated redundant requests against the same accounts is
    exactly the kind of automated-looking pattern that gets accounts
    flagged.
    """

    def __init__(self, path):
        self.path = path
        self.following_list = []
        self.muted_accounts = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.following_list = data.get("following_list", [])
        self.muted_accounts = data.get("muted_accounts", [])

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "following_list": self.following_list,
                    "muted_accounts": self.muted_accounts,
                },
                f,
                indent=2,
            )

    def is_following(self, user_id) -> bool:
        return user_id in self.following_list

    def mark_followed(self, user_id):
        if user_id not in self.following_list:
            self.following_list.append(user_id)
            self._save()

    def is_muted(self, user_id) -> bool:
        return user_id in self.muted_accounts

    def mark_muted(self, user_id):
        if user_id not in self.muted_accounts:
            self.muted_accounts.append(user_id)
            self._save()
