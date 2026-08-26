import datetime
import httpx
import os
import random
import time
import json
import uuid
from typing import Optional
from collections import OrderedDict
from scraper.base import BaseScraper
from scraper.x_client import fetch_and_init
from config import settings
from storage.file_manager import FileManager
from storage.agent_state import AgentState
from runtime.decision import DecisionEngine
from runtime.prompt import construct_prompt
from utils.duration import parse_duration

# Naming key, since these two GraphQL operation names are easy to conflate:
#   - `HomeTimeline`       -- X's algorithmic default feed, i.e. the "Home"
#                             tab. Our config/target name for this is "home".
#   - `HomeLatestTimeline` -- X's reverse-chronological feed, i.e. the
#                             "Following" tab. Our config/target name for
#                             this is "following".
# Every method/constant below that touches one of these two endpoints is
# named after exactly which one it is, specifically to avoid the mixup.
TWITTER_HOME_LATEST_TIMELINE_HASH = "KLMY6cZZUfQrLubs5DHHtQ"  # HomeLatestTimeline -> "following"
TWITTER_HOME_TIMELINE_HASH = "3b9_7tltt0hJRef-xm_3sw"  # HomeTimeline -> "home"
TWITTER_SEARCH_TIMELINE_HASH = "BGd0T_j7oVwlW5U79tO_0A"

# Response paths (under data[...]) each endpoint nests its `instructions`
# list under -- see parse_timeline(). HOME_TIMELINE_PATH is shared by BOTH
# HomeTimeline ("home") and HomeLatestTimeline ("following") responses --
# X nests both under the same `data.home.home_timeline_urt` envelope, so
# this name reflects X's shared response shape, not either target
# specifically; don't read it as meaning "the home_timeline target" (that
# target name no longer exists -- see the naming key above).
HOME_TIMELINE_PATH = ("home", "home_timeline_urt")
SEARCH_TIMELINE_PATH = ("search_by_raw_query", "search_timeline", "timeline")

# Feature flags copied verbatim from a real browser HomeTimeline request.
# X rotates these (and the hash above) periodically, the same way the
# x-client-transaction-id generator in x_client.py needs occasional care --
# if collection degrades again, re-capture a fresh HomeTimeline request from
# the browser devtools and diff it against this constant.
TWITTER_HOME_TIMELINE_FEATURES = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

# SearchTimeline happens to request the identical feature-flag set as
# HomeTimeline as of this writing (captured from a real browser request) --
# kept as its own name rather than reusing TWITTER_HOME_TIMELINE_FEATURES
# directly so the two can drift independently if X changes one but not the
# other.
TWITTER_SEARCH_TIMELINE_FEATURES = dict(TWITTER_HOME_TIMELINE_FEATURES)

# Local output-integrity safety net for observation methods that track seen
# ids (fetch_home, fetch_search_timeline); not sent over the wire.
_DEDUP_CACHE_SIZE = 5000

# Documented live follow rate limit (see ROADMAP.md's rate-limit note) --
# cold-start self-throttles follows to stay under this instead of relying on
# reactive 429 handling, since a burst of 429s makes little progress and
# looks more automated than pacing does.
FOLLOW_RATE_LIMIT = 15
FOLLOW_RATE_WINDOW_SECONDS = 15 * 60

# How many of the account's own past interactions to show the LLM each
# decision cycle, so it can avoid re-liking/re-following something it
# already acted on (roadmap Phase 4e follow-up -- see run_agent_runtime).
RECENT_INTERACTIONS_WINDOW = 10


class FetchFailedError(Exception):
    """Raised when a timeline fetch doesn't return usable JSON (bad body, rate limit, etc.)."""


class TwitterScraper(BaseScraper):
    # Maps a generic action name (as used by execute_action, and eventually
    # by the LLM Agent Runtime's decision output) to the concrete method
    # that performs it.
    ACTION_HANDLERS = {
        "like": "favorite_tweet",
        "retweet": "retweet",
        "follow": "follow_user",
        "mute": "mute_user",
    }

    # Maps a data_collection target name to the observation method that
    # serves it. "home" = X's algorithmic default feed (GraphQL
    # `HomeTimeline`); "following" = X's reverse-chronological feed (GraphQL
    # `HomeLatestTimeline`) -- see the naming key near the top of this file.
    OBSERVATION_HANDLERS = {
        "home": "fetch_home",
        "following": "fetch_following",
        "search": "fetch_search_timeline",
    }

    def run(self):
        account = self.account
        self._log("Running Twitter Scraper with the following parameters:")
        self._log(f"Targets: {account.targets}")
        self._log(f"Actions: {account.actions}")

        label = account.targets[0] if account.targets else "twitter"
        self.file_manager = FileManager(settings.OUTPUT_DIR, "twitter", label, account.name, account.timezone)
        self.agent_state = AgentState(
            os.path.join(settings.OUTPUT_DIR, "state", account.name, "twitter_agent_state.json")
        )
        if account.agent_runtime_enabled:
            self.runtime_file_manager = FileManager(
                settings.OUTPUT_DIR, "twitter", "runtime_log", account.name, account.timezone, stream="runtime_log"
            )

        self._log("Initialising x-client-transaction-id generator...")
        self.ct = fetch_and_init()
        self._log("Transaction generator ready.")

        self.client = httpx.Client(
            headers={
                "authorization": account.bearer_token,
                "x-csrf-token": account.csrf_token,
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "en",
                "x-twitter-auth-type": "OAuth2Session",
                "cookie": f"auth_token={account.auth_token}; ct0={account.csrf_token}",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "origin": "https://x.com",
                "referer": "https://x.com/home",
            },
            timeout=30,
        )

        if account.actions.get("follow_all"):
            self.follow_all()

        self.run_cold_start()

        if account.agent_runtime_enabled:
            # LLM-driven decision cycle (roadmap Phase 4) -- observes every
            # configured target each activation rather than one target
            # continuously.
            self.run_agent_runtime()
            return

        # Observation and actions are independent now (see config.yaml).
        # main.validate_targets() already guaranteed exactly one valid
        # target before this scraper was even constructed, when not in
        # Agent Runtime mode.
        handler_name = self.OBSERVATION_HANDLERS[account.targets[0]]
        getattr(self, handler_name)()

    def _log(self, message):
        print(f"[{self.account.name}] {message}")

    def fetch_home(self):
        """Observes the "home" target: X's algorithmic default feed (the
        "Home" tab; GraphQL operation `HomeTimeline`, see `fetch_home_timeline()`).
        """
        self._log("Observing Home (X's algorithmic default feed, GraphQL HomeTimeline).")
        self._scrape_timeline(
            self.fetch_home_timeline, HOME_TIMELINE_PATH, track_seen_ids=True
        )

    def fetch_following(self):
        """Observes the "following" target: X's reverse-chronological feed
        (the "Following" tab; GraphQL operation `HomeLatestTimeline`, see
        `fetch_home_latest_timeline()`).
        """
        self._log("Observing Following (X's chronological feed, GraphQL HomeLatestTimeline).")
        self._scrape_timeline(
            self.fetch_home_latest_timeline, HOME_TIMELINE_PATH
        )

    def fetch_search_timeline(self):
        query = self.account.search_query
        self._log(f"Observing Search results for query: {query!r}")
        self._scrape_timeline(
            self.fetch_search_latest, SEARCH_TIMELINE_PATH, track_seen_ids=True
        )

    def _scrape_timeline(self, fetch_fn, timeline_path, track_seen_ids=False):
        cursor = None
        num_tweets = 0
        consecutive_empty_batches = 0

        if track_seen_ids:
            # Rolling: IDs from the page we just fetched, echoed back to
            # Twitter as `seenTweetIds` on the next request (see
            # _fetch_timeline) so the ranking backend doesn't re-serve them
            # instead of pulling fresh candidates. This mirrors what a real
            # browser session does -- it is NOT a growing full-run history.
            self.seen_tweet_ids = []
            # Separate, capped local cache of every tweet_id collected this
            # run, used only to keep exact duplicates out of the JSONL
            # output; never sent over the wire.
            self._collected_tweet_ids = OrderedDict()

        while True:
            result = self._fetch_and_parse_with_retry(fetch_fn, cursor, timeline_path)

            if result is None:
                self._log(
                    "Giving up on this cursor after repeated failures; "
                    "restarting timeline from the top."
                )
                cursor = None
                continue

            tweets, next_cursor = result

            if track_seen_ids:
                new_tweets = []
                for tweet in tweets:
                    tweet_id = tweet.get("tweet_id")
                    if tweet_id and tweet_id in self._collected_tweet_ids:
                        continue
                    new_tweets.append(tweet)
                    if tweet_id:
                        self._collected_tweet_ids[tweet_id] = True
                        if len(self._collected_tweet_ids) > _DEDUP_CACHE_SIZE:
                            self._collected_tweet_ids.popitem(last=False)

                skipped = len(tweets) - len(new_tweets)
                if skipped:
                    self._log(f"Filtered {skipped} duplicate tweet(s) already collected this run.")

                self.seen_tweet_ids = [
                    tweet.get("tweet_id") for tweet in tweets if tweet.get("tweet_id")
                ]
                tweets = new_tweets

                if not tweets:
                    consecutive_empty_batches += 1
                    doubled = self.account.empty_batch_backoff_base * (2 ** (consecutive_empty_batches - 1))
                    jitter = self.account.empty_batch_backoff_jitter
                    # Jittered around the doubled value rather than doubling
                    # exactly every time -- a perfectly clean 1x/2x/4x/8x...
                    # cadence is a mechanical tell; real usage doesn't wait in
                    # neat powers of two.
                    jittered = doubled * random.uniform(1 - jitter, 1 + jitter)
                    wait = max(0.0, min(jittered, self.account.empty_batch_backoff_max))
                    self._log(
                        f"No new tweets in this batch ({consecutive_empty_batches} in a row); "
                        f"backing off for {wait:.0f}s before the next fetch."
                    )
                    time.sleep(wait)
                else:
                    consecutive_empty_batches = 0

            num_tweets += len(tweets)
            self._log(f"{num_tweets} tweets collected")
            if tweets:
                self.file_manager.save_data(tweets)

            if not next_cursor or next_cursor == cursor:
                self._log(
                    "Bottom of pagination reached (no new cursor); "
                    "restarting timeline from the top to keep running indefinitely."
                )
                cursor = None
                continue

            cursor = next_cursor

    def _fetch_and_parse_with_retry(self, fetch_fn, cursor, timeline_path):
        """Fetches and parses the given cursor, retrying the SAME cursor with
        backoff on bad/rate-limited/malformed responses.

        A malformed response (e.g. an empty `{"data": {"home": {}}}` body) is
        transient noise, not a signal that pagination has genuinely ended, so
        it must not be treated the same as a real "no next cursor" result.

        Returns (tweets, next_cursor), or None if all retries are exhausted.
        """
        account = self.account
        for attempt in range(1, account.fetch_max_retries + 1):
            try:
                data = fetch_fn(cursor)
                time.sleep(account.scroll_delay)
                return parse_timeline(data, timeline_path)
            except FetchFailedError as e:
                wait = account.fetch_retry_backoff * attempt
                self._log(
                    f"Fetch failed (attempt {attempt}/{account.fetch_max_retries}): "
                    f"{e}. Retrying same cursor in {wait}s..."
                )
                time.sleep(wait)
        return None

    def _fetch_timeline(self, url, cursor, variables, features):
        variables = dict(variables)
        if cursor:
            variables["cursor"] = cursor

        seen_ids = getattr(self, "seen_tweet_ids", None)
        if seen_ids:
            variables["seenTweetIds"] = seen_ids

        params = {
            "variables": json.dumps(variables),
            "features": json.dumps(features),
        }

        # X.com's own frontend sends x-client-transaction-id (and, per a
        # captured browser request, content-type: application/json) on
        # every /i/api request, not just actions with a body. Home/Following
        # have tolerated their absence so far, but there's no reason to
        # keep relying on that leniency, and Search's stricter bot checks
        # don't.
        path = url.removeprefix("https://x.com")
        r = self.client.get(
            url,
            params=params,
            headers={
                "x-client-transaction-id": self._txid("GET", path),
                "content-type": "application/json",
            },
        )

        if self._handle_rate_limit(r):
            reset = r.headers.get("x-rate-limit-reset")
            if reset:
                wait = max(int(reset) - int(time.time()), 1)
                self._log(f"Sleeping {wait}s until rate limit resets...")
                time.sleep(wait)
            raise FetchFailedError(f"rate limited (status={r.status_code})")

        try:
            return r.json()
        except Exception:
            self._log("NON JSON RESPONSE:")
            self._log(f"status={r.status_code}")
            # An empty/non-JSON body (as opposed to X's usual
            # {"errors": [...]} JSON payload) usually means the request was
            # rejected before reaching the GraphQL resolver at all (WAF/edge
            # layer) rather than a real API-level error -- response headers
            # often reveal which layer answered.
            self._log(f"response headers={dict(r.headers)}")
            self._log(f"body={r.text[:500]!r}")
            raise FetchFailedError(f"non-JSON response (status={r.status_code})")

    def fetch_home_latest_timeline(self, cursor=None):
        """Calls GraphQL operation `HomeLatestTimeline` -- X's reverse-
        chronological feed, i.e. the "Following" tab. Backs the "following"
        target (see `fetch_following()`). Named after the GraphQL operation
        itself, not the target, so it's never ambiguous which one this is.
        """
        url = f"https://x.com/i/api/graphql/{TWITTER_HOME_LATEST_TIMELINE_HASH}/HomeLatestTimeline"
        variables = {
            "count": 20,
            "includePromotedContent": True,
            "latestControlAvailable": True,
            "withVoice": True,
        }
        return self._fetch_timeline(url, cursor, variables, {})

    def fetch_home_timeline(self, cursor=None):
        """Calls GraphQL operation `HomeTimeline` -- X's algorithmic default
        feed, i.e. the "Home" tab. Backs the "home" target (see
        `fetch_home()`). Named after the GraphQL operation itself, not the
        target, so it's never ambiguous which one this is.
        """
        url = f"https://x.com/i/api/graphql/{TWITTER_HOME_TIMELINE_HASH}/HomeTimeline"
        variables = {
            "count": 20,
            "includePromotedContent": True,
            "withCommunity": True,
        }
        if cursor is None:
            variables["requestContext"] = "launch"
        return self._fetch_timeline(url, cursor, variables, TWITTER_HOME_TIMELINE_FEATURES)

    def fetch_search_latest(self, cursor=None):
        url = f"https://x.com/i/api/graphql/{TWITTER_SEARCH_TIMELINE_HASH}/SearchTimeline"
        variables = {
            "rawQuery": self.account.search_query,
            "count": 20,
            "querySource": "typed_query",
            "product": "Top",
            "withGrokTranslatedBio": True,
            "withQuickPromoteEligibilityTweetFields": False,
        }
        return self._fetch_timeline(url, cursor, variables, TWITTER_SEARCH_TIMELINE_FEATURES)

    def execute_action(self, action: str, target: str = None) -> dict:
        """Generic action dispatcher: `action` is one of "like"/"retweet"/
        "follow"/"mute"/"no_action", `target` is the relevant tweet_id or
        user_id (unused for "no_action").

        This is the single entry point the LLM-driven Agent Runtime
        (roadmap Phase 4) calls with its decision output -- its
        {action, target_object} maps directly onto (action, target) here.

        Returns {"execution_status": "success"|"failure"|"skipped",
        "system_response": {...} | None} -- the paper's Decision/runtime_log
        schema fields that only exist once an action has actually been
        attempted (roadmap Phase 4c), so callers get a structured result
        instead of a bare bool. Successful follow/mute actions also update
        local agent state, so callers don't need to do that bookkeeping
        themselves.
        """
        if action == "no_action":
            return {"execution_status": "skipped", "system_response": None}

        handler_name = self.ACTION_HANDLERS.get(action)
        if not handler_name:
            raise ValueError(
                f"Unknown action: {action!r}. Must be one of {list(self.ACTION_HANDLERS)} or 'no_action'."
            )

        result = getattr(self, handler_name)(target)
        success = result.get("success", False)

        if success:
            if action == "follow":
                self.agent_state.mark_followed(target)
            elif action == "mute":
                self.agent_state.mark_muted(target)
        else:
            # Surface failures loudly and with the actual reason (e.g. a
            # GraphQL "this request looks like it might be automated"
            # rejection, a 404, a rate limit) -- a bare status/reason line
            # from the handler's own request log is easy to miss among the
            # rest of the run's output, and callers must not assume an
            # attempted action actually happened.
            self._log(
                f"[action] {action} on {target!r} FAILED: {result.get('reason', 'unknown reason')} "
                f"(status_code={result.get('status_code')})"
            )

        return {
            "execution_status": "success" if success else "failure",
            "system_response": {k: v for k, v in result.items() if k != "success"},
        }

    def _txid(self, method: str, path: str) -> str:
        return self.ct.generate(method, path)

    def _handle_rate_limit(self, r) -> bool:
        """Prints rate limit info and returns True if rate limited."""
        if r.status_code != 429:
            return False
        reset = r.headers.get("x-rate-limit-reset")
        limit = r.headers.get("x-rate-limit-limit")
        remaining = r.headers.get("x-rate-limit-remaining")
        self._log(f"Rate limited — limit={limit}, remaining={remaining}, resets_at={reset}")
        if reset:
            reset_dt = datetime.datetime.fromtimestamp(
                int(reset), tz=datetime.timezone.utc
            )
            self._log(f"Reset time (UTC): {reset_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        return True

    def _post_graphql_mutation(self, path: str, query_id: str, variables: dict) -> dict:
        """POST a GraphQL mutation and determine success from the response
        *body*, not just the HTTP status code.

        X's GraphQL endpoints routinely return HTTP 200 with an `errors`
        array and null `data` when the mutation itself fails (stale query
        hash, already-performed action, tweet deleted, etc.) -- a 200
        status alone is not proof the action happened. Also surfaces the
        actual error message in the returned `reason` so a failure like
        this is diagnosable straight from `runtime_log.jsonl` instead of
        requiring a live repro.
        """
        r = self.client.post(
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": self._txid("POST", path),
                "content-type": "application/json",
            },
            content=json.dumps({"variables": variables, "queryId": query_id}),
        )
        self._log(f"{r.status_code} {r.reason_phrase}")
        if self._handle_rate_limit(r):
            return {"success": False, "status_code": r.status_code, "reason": "rate_limited"}
        if r.status_code != 200:
            self._log(f"response: {r.text[:500]}")
            return {"success": False, "status_code": r.status_code, "reason": r.reason_phrase}

        try:
            body = r.json()
        except ValueError:
            body = None
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            message = errors[0].get("message", "graphql_error")
            self._log(f"GraphQL mutation returned 200 but reported errors: {errors}")
            return {"success": False, "status_code": r.status_code, "reason": message}
        return {"success": True, "status_code": r.status_code, "reason": r.reason_phrase}

    def favorite_tweet(self, tweet_id: str) -> dict:
        return self._post_graphql_mutation(
            "/i/api/graphql/lI07N6Otwv1PhnEgXILM7A/FavoriteTweet",
            "lI07N6Otwv1PhnEgXILM7A",
            {"tweet_id": tweet_id},
        )

    def retweet(self, tweet_id: str) -> dict:
        return self._post_graphql_mutation(
            "/i/api/graphql/mbRO74GrOvSfRcJnlMapnQ/CreateRetweet",
            "mbRO74GrOvSfRcJnlMapnQ",
            {"tweet_id": tweet_id, "dark_request": False},
        )

    def mute_user(self, user_id: str) -> dict:
        path = "/i/api/1.1/mutes/users/create.json"
        r = self.client.post(
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": self._txid("POST", path),
                "content-type": "application/x-www-form-urlencoded",
            },
            content=f"user_id={user_id}",
        )
        self._log(f"{r.status_code} {r.reason_phrase}")
        if self._handle_rate_limit(r):
            return {"success": False, "status_code": r.status_code, "reason": "rate_limited"}
        if r.status_code != 200:
            self._log(f"response: {r.text[:500]}")
        return {"success": r.status_code == 200, "status_code": r.status_code, "reason": r.reason_phrase}

    def follow_user(self, user_id: str) -> dict:
        path = "/i/api/1.1/friendships/create.json"
        body = (
            "include_profile_interstitial_type=1&include_blocking=1&include_blocked_by=1"
            "&include_followed_by=1&include_want_retweets=1&include_mute_edge=1"
            "&include_can_dm=1&include_can_media_tag=1&include_ext_is_blue_verified=1"
            "&include_ext_verified_type=1&include_ext_profile_image_shape=1"
            f"&skip_status=1&user_id={user_id}"
        )
        r = self.client.post(
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": self._txid("POST", path),
                "content-type": "application/x-www-form-urlencoded",
            },
            content=body,
        )
        self._log(f"{r.status_code} {r.reason_phrase}")
        if self._handle_rate_limit(r):
            return {"success": False, "status_code": r.status_code, "reason": "rate_limited"}
        if r.status_code != 200:
            self._log(f"response: {r.text[:500]}")
            return {"success": False, "status_code": r.status_code, "reason": r.reason_phrase}
        return {"success": True, "status_code": r.status_code, "reason": r.reason_phrase}

    def follow_all(self):
        list_path = self.account.get_account_list_path("follow_list")
        with open(list_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for entry in data["users"]:
            user_id = entry["user_id"]
            if self.agent_state.is_following(user_id):
                self._log(f"Already following {user_id} ({entry.get('_comment', '')}); skipping.")
                continue
            self._log(f"Following user {user_id} ({entry.get('_comment', '')})...")
            result = self.execute_action("follow", user_id)
            if result["execution_status"] != "success":
                break
            time.sleep(5)

    def run_cold_start(self):
        """Cold-start initialization (roadmap Phase 4a): before entering
        normal observation, follow/engage with content sampled from the
        configured `sockpuppet_config.account_lists` until the
        `initialization_params` stopping criteria are met (or
        `max_initialization_days` elapses), so the platform's recommendation
        system has behavioral signal to personalize against.

        Runs the same restricted action set the paper specifies for this
        phase (follow/like/repost, no mute/unmute). Engagement candidates are
        drawn from this account's own Home feed (GraphQL HomeTimeline)
        rather than a dedicated per-account-list tweet fetch, since no such
        endpoint is implemented yet -- see ROADMAP.md Phase 4a note.

        No-op if this account has no `initialization_params` configured, or
        if a prior run already completed initialization (persisted in
        agent_state).
        """
        account = self.account
        params = account.initialization_params
        if not params:
            return
        if self.agent_state.is_initialization_complete():
            self._log("Cold-start initialization already complete; skipping.")
            return

        min_follows = params["min_follows"]
        min_engagements = params["min_engagements"]
        max_days = params["max_initialization_days"]
        self._log(
            f"Starting cold-start initialization: min_follows={min_follows}, "
            f"min_engagements={min_engagements}, max_initialization_days={max_days}"
        )

        deadline = time.time() + max_days * 86400
        candidates = self._load_cold_start_candidates()
        candidate_idx = 0
        engagement_queue = []

        def follows_done():
            return len(self.agent_state.following_list)

        def engagements_done():
            return self.agent_state.count_interactions(phase="initialization", actions=("like", "retweet"))

        while follows_done() < min_follows or engagements_done() < min_engagements:
            if time.time() >= deadline:
                self._log(
                    f"[cold-start] max_initialization_days ({max_days}) elapsed with "
                    f"follows={follows_done()}/{min_follows}, engagements={engagements_done()}/{min_engagements}; "
                    f"stopping without meeting all criteria."
                )
                break

            if follows_done() < min_follows and candidate_idx < len(candidates):
                user_id = candidates[candidate_idx]
                candidate_idx += 1
                if self.agent_state.is_following(user_id):
                    continue
                self._log(f"[cold-start] Following {user_id} ({follows_done()}/{min_follows})")
                result = self._throttled_follow(user_id)
                if result["execution_status"] == "success":
                    self.agent_state.record_interaction(
                        "follow", user_id, phase="initialization",
                        execution_status=result["execution_status"],
                        system_response=result["system_response"],
                    )
                continue

            if engagements_done() < min_engagements:
                if not engagement_queue:
                    engagement_queue = self._next_cold_start_engagement_candidates()
                    if not engagement_queue:
                        self._log("[cold-start] No engagement candidates available right now; waiting.")
                        time.sleep(account.scroll_delay)
                        continue
                tweet = engagement_queue.pop(0)
                tweet_id = tweet.get("tweet_id")
                if not tweet_id:
                    continue
                action = random.choice(["like", "retweet"])
                self._log(f"[cold-start] {action}-ing tweet {tweet_id} ({engagements_done()}/{min_engagements})")
                result = self.execute_action(action, tweet_id)
                if result["execution_status"] == "success":
                    self.agent_state.record_interaction(
                        action, tweet_id, phase="initialization",
                        execution_status=result["execution_status"],
                        system_response=result["system_response"],
                    )
                time.sleep(account.scroll_delay)
                continue

            # Engagements are satisfied but follows aren't, and we've run out
            # of follow candidates -- nothing left to do before the deadline.
            self._log(
                f"[cold-start] Ran out of follow candidates with follows={follows_done()}/{min_follows} "
                f"unmet; stopping without meeting all criteria."
            )
            break

        self.agent_state.mark_initialization_complete()
        self._log(
            f"[cold-start] Initialization complete: follows={follows_done()}/{min_follows}, "
            f"engagements={engagements_done()}/{min_engagements}"
        )

    def _load_cold_start_candidates(self):
        """Flattens and dedupes user_ids from every account list named in
        `sockpuppet_config.account_lists`, shuffled so candidates aren't
        always drawn in file order.
        """
        user_ids = []
        seen = set()
        for list_name in self.account.cold_start_account_lists:
            path = self.account.get_account_list_path(list_name)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.get("users", []):
                user_id = entry.get("user_id")
                if user_id and user_id not in seen:
                    seen.add(user_id)
                    user_ids.append(user_id)
        random.shuffle(user_ids)
        return user_ids

    def _throttled_follow(self, user_id) -> dict:
        """Self-throttles to stay under FOLLOW_RATE_LIMIT per
        FOLLOW_RATE_WINDOW_SECONDS, sleeping ahead of a 429 instead of
        reacting to one. Returns execute_action()'s structured result.
        """
        now = time.time()
        timestamps = [t for t in getattr(self, "_follow_timestamps", []) if now - t < FOLLOW_RATE_WINDOW_SECONDS]
        if len(timestamps) >= FOLLOW_RATE_LIMIT:
            wait = FOLLOW_RATE_WINDOW_SECONDS - (now - timestamps[0]) + 1
            self._log(f"[cold-start] Follow rate limit reached; sleeping {wait:.0f}s.")
            time.sleep(wait)
            now = time.time()
            timestamps = [t for t in timestamps if now - t < FOLLOW_RATE_WINDOW_SECONDS]

        result = self.execute_action("follow", user_id)
        timestamps.append(time.time())
        self._follow_timestamps = timestamps
        return result

    def _next_cold_start_engagement_candidates(self):
        """Pulls one page of the Home feed (GraphQL HomeTimeline) to use as
        engagement targets.

        Not scoped to the configured account_lists specifically (that would
        need a per-account tweet-fetch endpoint this codebase doesn't have
        captured yet) -- see ROADMAP.md Phase 4a note.
        """
        try:
            data = self.fetch_home_timeline(None)
            tweets, _ = parse_timeline(data, HOME_TIMELINE_PATH)
            return tweets
        except FetchFailedError as e:
            self._log(f"[cold-start] Failed to fetch engagement candidates: {e}")
            return []

    def observe(self) -> dict:
        """Observation stage (roadmap Phase 4, paper section 3.3 stage 1):
        fetches ONE page of every configured `data_collection.targets` right
        now, rather than the old continuous per-target scrape loop
        (_scrape_timeline) used outside Agent Runtime mode. This is what
        makes simultaneous multi-target observation possible -- each
        activation gets a snapshot of every target, not an indefinite scroll
        through one.

        Returns platform_state: {target_name: [tweet, ...]}.
        """
        raw_fetchers = {
            "home": (self.fetch_home_timeline, HOME_TIMELINE_PATH),
            "following": (self.fetch_home_latest_timeline, HOME_TIMELINE_PATH),
            "search": (self.fetch_search_latest, SEARCH_TIMELINE_PATH),
        }
        platform_state = {}
        for target in self.account.targets:
            fetch_fn, timeline_path = raw_fetchers[target]
            result = self._fetch_and_parse_with_retry(fetch_fn, None, timeline_path)
            platform_state[target] = result[0] if result else []
        return platform_state

    def _allowed_actions_for_phase(self, phase: str) -> list:
        """Restricted action set per experiment phase (paper section 3.2):
        cold-start/initialization excludes mute/unmute; pre/post-treatment
        allow the full implemented set. `no_action` is always available.
        """
        if phase == "initialization":
            return ["follow", "like", "retweet", "no_action"]
        return ["follow", "like", "retweet", "mute", "no_action"]

    def _build_experiment_context(self) -> dict:
        """experiment_context (paper section 3.3): experiment_id/phase/
        treatment_arm/intervention_status are stand-ins here -- Account
        exposes them from `sockpuppet_config`/`experiment_design` config for
        now, superseded by the Experiment Orchestrator's real per-agent
        state once it exists (roadmap Phase 5).
        """
        account = self.account
        return {
            "experiment_id": account.experiment_id,
            "phase": account.experiment_phase,
            "treatment_arm": account.treatment_arm,
            "intervention_status": "none",
            "current_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def _resolve_target(self, action: str, tweet: dict) -> Optional[str]:
        """Deterministically derives target_object from the single tweet a
        per-post decision concerns (roadmap Phase 4, per-post redesign) --
        the model is never asked to supply this itself, so there's no id for
        it to hallucinate or confuse (a real bug in the old whole-batch
        design, see ROADMAP.md's Phase 4 follow-ups).

        like/retweet act on the tweet itself; follow/mute act on its author.
        Can legitimately return None even for a non-"no_action" action if
        the tweet's own data is missing the needed id (e.g. a malformed
        upstream API response) -- that's a data-quality problem, not a
        hallucination, and the caller must treat it as such.
        """
        if action in ("like", "retweet"):
            return tweet.get("tweet_id")
        if action in ("follow", "mute"):
            return (tweet.get("author") or {}).get("user_id")
        return None

    def _validate_decision(self, decision, tweet: dict, allowed_actions: list):
        """Guards against three ways a structured LLM decision can still be
        unusable: an action outside what's allowed this phase, a target id
        that's missing from this tweet's own data (a data-quality problem,
        not a hallucination -- there's no LLM-supplied id to hallucinate
        anymore in the per-post design), or a repeat of an (action, target)
        pair this account already did successfully. Each case is downgraded
        to a logged no_action rather than passed to execute_action() -- the
        repeat check in particular exists because telling the model "don't
        repeat yourself" in the prompt (see YOUR RECENT ACTIONS) is not
        reliably honored by a small local model; this makes it a hard rule
        instead of a hint.

        Returns (action, target_object, no_action_reason). no_action_reason
        is None whenever action isn't "no_action" (there's nothing to
        explain), and otherwise one of:
        - "chose": the model was shown this post and genuinely decided
          no_action was the right call.
        - "repeat": downgraded because it's an exact repeat of an
          (action, target) pair that already succeeded before.
        - "failed": downgraded because the decision itself couldn't be
          honored -- either the action isn't allowed this phase, or this
          post's own data is missing the id the chosen action needs.
        """
        action = decision.action

        if action not in allowed_actions:
            self._log(
                f"[runtime] LLM chose disallowed action {action!r} for this phase "
                f"(allowed: {allowed_actions}); treating as no_action."
            )
            return "no_action", None, "failed"

        if action == "no_action":
            return "no_action", None, "chose"

        target = self._resolve_target(action, tweet)

        if target is None:
            self._log(
                f"[runtime] LLM chose {action} on tweet_id={tweet.get('tweet_id')!r}, but this "
                f"tweet's own data is missing the id {action} needs; rejecting as no_action "
                f"rather than acting on incomplete data."
            )
            return "no_action", None, "failed"

        if self.agent_state.has_acted_on(action, target):
            self._log(
                f"[runtime] LLM chose {action} on target_object={target!r} again, which it already "
                f"did successfully before; rejecting as no_action instead of repeating a no-op action."
            )
            return "no_action", None, "repeat"

        return action, target, None

    def _sample_activation_interval(self) -> float:
        """Samples the wait until the next decision cycle from
        `sockpuppet_config.activation_schedule` (paper section 3.3: Runtime
        Scheduling). A single-account stand-in for the Experiment
        Orchestrator's real per-agent scheduler (roadmap Phase 5) --
        `truncated_powerlaw` here is approximated as a log-uniform draw over
        [min_interval, max_interval] (no shape parameter is specified in the
        paper's config schema to do otherwise); `uniform` samples evenly.
        Revisit once Phase 5 formalizes real scheduling.
        """
        schedule = self.account.sockpuppet_config.get("activation_schedule") or {}
        min_interval = parse_duration(schedule.get("min_interval", "15m"))
        max_interval = parse_duration(schedule.get("max_interval", "6h"))
        distribution = schedule.get("distribution", "uniform")

        if max_interval <= min_interval:
            return min_interval
        if distribution == "truncated_powerlaw":
            return min_interval * (max_interval / min_interval) ** random.random()
        return random.uniform(min_interval, max_interval)

    def _log_runtime_cycle(
        self, observation_id, phase, prompt, decision, action, target,
        observed_tweet_id, result, target_name=None, no_action_reason=None,
    ):
        """Logging stage (roadmap Phase 4, paper section 3.3 stage 5 /
        section 4 runtime_log schema). `data_collection.logging: minimal`
        omits the constructed prompt and raw model output (larger, more
        sensitive) and keeps just the decision/execution trace.

        `observed_tweet_id` records which specific post prompted this row --
        needed since the per-post redesign (roadmap Phase 4) means
        `target_object` alone doesn't identify it: it's None for no_action,
        and it's the *author's* user_id (not the tweet's id) for
        follow/mute. `target_name` (which feed this post came from) is
        included when available for the same traceability reason.

        `no_action_reason` (see `_validate_decision`) distinguishes the three
        ways a row can end up `no_action` -- "chose" (the model genuinely
        decided not to act), "repeat" (downgraded, exact repeat of a prior
        success), "failed" (downgraded, disallowed action or missing target
        data) -- always present when `action == "no_action"`, always None
        otherwise.
        """
        entry = {
            "observation_id": observation_id,
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "phase": phase,
            "observed_tweet_id": observed_tweet_id,
            "target_name": target_name,
            "selected_action": action,
            "target_object": target,
            "no_action_reason": no_action_reason,
            "execution_status": result["execution_status"],
            "system_response": result["system_response"],
        }
        if self.account.logging_level != "minimal":
            entry["constructed_prompt"] = prompt
            entry["model_output"] = decision.model_dump() if decision is not None else None
        self.runtime_file_manager.save_data(entry)

    def run_agent_runtime(self):
        """The Agent Runtime's five-stage decision cycle (roadmap Phase 4,
        paper section 3.3): Observation -> Prompt Construction -> Decision
        -> Execution -> Logging, repeated at activation intervals sampled
        from `sockpuppet_config.activation_schedule`.

        Per-post redesign (roadmap Phase 4, 2026-08-18): one activation
        still means one `observe()` snapshot, but within that snapshot every
        individual post gets its OWN Prompt Construction -> Decision ->
        Execution -> Logging pass, rather than one prompt/decision for the
        whole batch. This matches the paper's own worked example ("for each
        X item in the feed, decide whether or not to engage") and avoids
        overwhelming the model with dozens of competing candidates in one
        prompt -- live testing found the old whole-batch design made the
        model default to `like` almost every cycle instead of reasoning
        through compound persona triggers. `activation_schedule` still
        governs the interval BETWEEN activations (platform visits), not
        between individual post evaluations within one activation.

        Runs forever (each account already gets its own thread -- see
        main.py). This is a single-account stand-in for the real per-agent
        activation scheduler the Experiment Orchestrator (roadmap Phase 5)
        will own, same as Phase 3's one-thread-per-account was for
        multi-account concurrency.
        """
        account = self.account
        self._log("Starting Agent Runtime decision loop.")
        persona_prompt = account.get_persona_prompt_text()
        engine = DecisionEngine()

        while True:
            phase = account.experiment_phase
            allowed_actions = self._allowed_actions_for_phase(phase)

            platform_state = self.observe()
            observation_id = str(uuid.uuid4())
            self.file_manager.save_data({"observation_id": observation_id, "platform_state": platform_state})

            total_posts = sum(len(tweets) for tweets in platform_state.values())
            if total_posts == 0:
                # Nothing observed this activation -- still write one row so
                # the runtime_log's audit trail (paper: Experiment
                # Orchestrator responsibility #7, reproducibility logging)
                # shows the activation happened rather than looking
                # indistinguishable from a skipped/crashed one.
                self._log("[runtime] Nothing observed this activation.")
                result = self.execute_action("no_action", None)
                self.agent_state.record_interaction(
                    "no_action", None, phase=phase,
                    execution_status=result["execution_status"],
                    system_response=result["system_response"],
                )
                self._log_runtime_cycle(
                    observation_id, phase, None, None, "no_action", None, None, result,
                    no_action_reason="nothing_observed",
                )
            else:
                for target_name, tweets in platform_state.items():
                    for tweet in tweets:
                        experiment_context = self._build_experiment_context()
                        recent_interactions = self.agent_state.recent_interactions(
                            limit=RECENT_INTERACTIONS_WINDOW, exclude_actions=("no_action",)
                        )
                        prompt = construct_prompt(
                            persona_prompt, tweet, target_name, experiment_context,
                            allowed_actions, recent_interactions,
                        )

                        decision = engine.decide(prompt)
                        action, target, no_action_reason = self._validate_decision(decision, tweet, allowed_actions)

                        self._log(
                            f"[runtime] Decision on tweet_id={tweet.get('tweet_id')}: "
                            f"action={action}, target_object={target}"
                            + (f", no_action_reason={no_action_reason}" if no_action_reason else "")
                        )
                        result = self.execute_action(action, target)

                        self.agent_state.record_interaction(
                            action, target, phase=phase,
                            execution_status=result["execution_status"],
                            system_response=result["system_response"],
                        )
                        self._log_runtime_cycle(
                            observation_id, phase, prompt, decision, action, target,
                            tweet.get("tweet_id"), result, target_name=target_name,
                            no_action_reason=no_action_reason,
                        )

            interval = self._sample_activation_interval()
            self._log(f"[runtime] Next activation in {interval:.0f}s.")
            time.sleep(interval)


def parse_timeline(data, timeline_path):
    """Parses a timeline response into (tweets, next_cursor).

    `timeline_path` is the sequence of keys under `data[...]` that leads to
    the `instructions` list -- e.g. HOME_TIMELINE_PATH for Home/Following
    (GraphQL HomeTimeline/HomeLatestTimeline both share this path -- see the
    naming key near the top of this file), SEARCH_TIMELINE_PATH for Search
    (see path constants near the top of this file). The entries within
    `instructions` follow the same tweet-*/
    cursor-bottom-* shape across all three endpoints, so only the path to
    reach them differs.
    """
    tweets = []

    if "errors" in data:
        codes = ", ".join(
            f"{err.get('code')}: {err.get('message')}" for err in data["errors"]
        )
        raise FetchFailedError(f"API returned errors: {codes}")

    try:
        node = data["data"]
        for key in timeline_path:
            node = node[key]
        instructions = node["instructions"]
    except (KeyError, TypeError):
        raise FetchFailedError(
            f"Unexpected response structure: {json.dumps(data)[:300]}"
        )

    next_cursor = None

    for instruction in instructions:
        if "entries" not in instruction:
            continue

        for entry in instruction["entries"]:
            entry_id = entry["entryId"]

            if entry_id.startswith("tweet-"):
                try:
                    tweet_data = _unwrap_tweet_result(
                        entry.get("content", {})
                        .get("itemContent", {})
                        .get("tweet_results", {})
                        .get("result")
                    )
                    if tweet_data:
                        tweets.append(build_tweet_object(tweet_data))
                except Exception as e:
                    print("Error parsing tweet entry:", e)

            elif entry_id.startswith("home-conversation-"):
                for item in entry.get("content", {}).get("items", []):
                    try:
                        tweet_data = _unwrap_tweet_result(
                            item.get("item", {})
                            .get("itemContent", {})
                            .get("tweet_results", {})
                            .get("result")
                        )
                        if tweet_data:
                            tweets.append(build_tweet_object(tweet_data))
                    except Exception as e:
                        print("Error parsing module tweet:", e)

            elif entry_id.startswith("cursor-bottom"):
                next_cursor = entry["content"]["value"]

    return tweets, next_cursor


def _unwrap_tweet_result(result):
    """Normalizes a tweet_results.result node to a plain Tweet dict (or None).

    `result` isn't always a `Tweet` — sensitive/age-restricted tweets come
    back as `TweetWithVisibilityResults` with the real tweet nested under
    `result["tweet"]`, and deleted/suspended/withheld tweets come back as
    `TweetTombstone` with no underlying tweet data at all.
    """
    if not result:
        return None

    typename = result.get("__typename")

    if typename == "TweetWithVisibilityResults":
        return result.get("tweet")

    if typename == "TweetTombstone" or "rest_id" not in result:
        return None

    return result


def build_tweet_object(tweet):
    legacy = tweet.get("legacy", {})
    retweeted_status = legacy.get("retweeted_status_result", {}).get("result")
    user = tweet.get("core", {}).get("user_results", {}).get("result", {})
    # X has moved some UserResults fields (name/screen_name) out of `legacy`
    # and into `core` over time, without a clean cutover -- check both so
    # this keeps working regardless of which shape a given response uses.
    user_legacy = user.get("legacy", {})
    user_core = user.get("core", {})

    src = retweeted_status.get("legacy", {}) if retweeted_status else legacy

    return {
        "tweet_id": tweet.get("rest_id"),
        "created_at": legacy.get("created_at"),
        "text": src.get("full_text"),
        "language": legacy.get("lang"),
        "metrics": {
            "likes": src.get("favorite_count", 0),
            "retweets": src.get("retweet_count", 0),
            "replies": src.get("reply_count", 0),
            "quotes": src.get("quote_count", 0),
            "bookmarks": src.get("bookmark_count", 0),
        },
        "author": {
            "user_id": user.get("rest_id"),
            "username": user_core.get("screen_name") or user_legacy.get("screen_name"),
            "display_name": user_core.get("name") or user_legacy.get("name"),
            "followers": user_legacy.get("followers_count")
            or user.get("relationship_counts", {}).get("followers", 0),
        },
    }
