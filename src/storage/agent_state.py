import datetime
import json
import os


class AgentState:
    """Persists lightweight per-account state (following/muting, interaction
    history, and cold-start initialization progress) across runs.

    Without this, follow_all()/cold-start would have no memory between
    container restarts and would keep re-submitting follow requests for
    accounts already followed, or re-run cold-start initialization from
    scratch — repeated redundant requests against the same accounts is
    exactly the kind of automated-looking pattern that gets accounts
    flagged.
    """

    def __init__(self, path):
        self.path = path
        self.following_list = []
        self.muted_accounts = []
        self.interaction_history = []
        self.initialization_progress = {"completed": False, "completed_at": None}
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            return
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.following_list = data.get("following_list", [])
        self.muted_accounts = data.get("muted_accounts", [])
        self.interaction_history = data.get("interaction_history", [])
        self.initialization_progress = data.get(
            "initialization_progress", {"completed": False, "completed_at": None}
        )

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "following_list": self.following_list,
                    "muted_accounts": self.muted_accounts,
                    "interaction_history": self.interaction_history,
                    "initialization_progress": self.initialization_progress,
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

    def record_interaction(self, action, target, phase, execution_status=None, system_response=None):
        entry = {
            "action": action,
            "target": target,
            "phase": phase,
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        if execution_status is not None:
            entry["execution_status"] = execution_status
        if system_response is not None:
            entry["system_response"] = system_response
        self.interaction_history.append(entry)
        self._save()

    def count_interactions(self, phase=None, actions=None) -> int:
        return sum(
            1 for entry in self.interaction_history
            if (phase is None or entry.get("phase") == phase)
            and (actions is None or entry.get("action") in actions)
        )

    def has_acted_on(self, action, target) -> bool:
        """True if this exact (action, target) pair already succeeded --
        e.g. this account already liked this specific tweet_id. Used to
        enforce "don't repeat an action" as a hard rule rather than a
        prompt hint the model can (and does) ignore.
        """
        return any(
            e.get("action") == action and e.get("target") == target and e.get("execution_status") == "success"
            for e in self.interaction_history
        )

    def recent_interactions(self, limit=10, exclude_actions=None) -> list:
        """The most recent `limit` interaction_history entries (oldest
        first), optionally dropping actions that add no useful "don't
        repeat this" context -- e.g. `exclude_actions=("no_action",)`,
        since there's nothing to avoid repeating about doing nothing.
        """
        entries = self.interaction_history
        if exclude_actions:
            entries = [e for e in entries if e.get("action") not in exclude_actions]
        return entries[-limit:]

    def is_initialization_complete(self) -> bool:
        return self.initialization_progress.get("completed", False)

    def mark_initialization_complete(self):
        self.initialization_progress = {
            "completed": True,
            "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self._save()
