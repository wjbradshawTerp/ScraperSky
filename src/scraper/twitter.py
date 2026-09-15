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
from utils.rate_budget import build_budgets
from utils.seeding import seeded_rng

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

# Pages fetched per target per rotation when the old scripted path is
# configured with more than one target (see _run_scripted_observation).
# Small on purpose: the point is alternating coverage of both feeds, not
# deep pagination of either.
SCRIPTED_ROUND_ROBIN_PAGES = 3

# How long a paused agent waits before re-checking experiment_status, and
# the slice size long sleeps are broken into so a completed experiment is
# noticed promptly (see _sleep_interruptible).
PAUSED_POLL_SECONDS = 30
STOP_CHECK_SLICE_SECONDS = 5

# One bounded retry for an agent-selected action that fails transiently
# (see _execute_agent_action).
AGENT_ACTION_MAX_ATTEMPTS = 2
AGENT_ACTION_RETRY_SECONDS = 5

# Markers in a failure reason that mean the platform DECLINED the action on
# purpose (anti-automation, authorization, rate limiting) rather than
# failing transiently. These are never retried -- retrying a deliberate
# refusal would be working around an anti-abuse control, not recovering
# from an error.
PLATFORM_REFUSAL_MARKERS = ("automated", "authorization", "rate_limited", "not authorized")


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
        # Optional per-account data streams from the paper's Data Streams
        # list (section 4), enabled by naming them in
        # `data_collection.targets`: Engagement History and Intervention
        # History. Both are derived from actions this account takes, so they
        # are written here rather than by observe().
        self.engagement_file_manager = (
            FileManager(
                settings.OUTPUT_DIR, "twitter", "engagement_log", account.name,
                account.timezone, stream="engagement_log",
            )
            if "engagement_log" in account.data_streams else None
        )
        self.intervention_file_manager = (
            FileManager(
                settings.OUTPUT_DIR, "twitter", "intervention_history", account.name,
                account.timezone, stream="intervention_history",
            )
            if "intervention_history" in account.data_streams else None
        )

        # agent_state.persona_prompt/account_metadata (paper section 3.2's
        # Initialization State) -- the agent's own record of the behavioral
        # policy and identity it actually ran under, which the paper
        # requires stay fixed from initialization through post-treatment.
        self.agent_state.record_agent_identity(
            persona_prompt=account.get_persona_prompt_text() if account.agent_runtime_enabled else None,
            account_metadata={
                "agent_id": account.agent_id,
                "account_name": account.name,
                "platform": account.platform,
                "experiment_id": account.experiment_id,
                "observation_targets": list(account.targets),
            },
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

        if self.orchestrator is not None:
            # Gives the shared ExperimentOrchestrator (roadmap Phase 5) a
            # live scraper to execute this account's intervention through,
            # reusing execute_action() rather than a parallel action path.
            self.orchestrator.register_scraper(account.name, self)

        self.run_cold_start()

        if account.agent_runtime_enabled:
            # LLM-driven decision cycle (roadmap Phase 4) -- observes every
            # configured target each activation rather than one target
            # continuously.
            self.run_agent_runtime()
            return

        # Observation and actions are independent now (see config.yaml).
        self._run_scripted_observation()

    def _run_scripted_observation(self):
        """The old scripted (non-Agent-Runtime) observation path.

        A single configured target keeps its original behavior exactly: one
        continuous, indefinite scroll through that timeline. With several
        targets it round-robins a bounded number of pages per target, so an
        account without a persona can still collect the paper's two feeds
        (For You *and* the reverse-chronological Home timeline, section 4)
        instead of only ever scrolling one of them.
        """
        targets = self.account.targets
        if len(targets) == 1:
            handler_name = self.OBSERVATION_HANDLERS[targets[0]]
            getattr(self, handler_name)()
            return

        self._log(
            f"Round-robin scripted observation across {targets} "
            f"({SCRIPTED_ROUND_ROBIN_PAGES} page(s) per target per rotation)."
        )
        while not self._should_stop():
            if self._idle_while_paused():
                continue
            for target in targets:
                if self._should_stop():
                    return
                handler_name = self.OBSERVATION_HANDLERS[target]
                getattr(self, handler_name)(max_pages=SCRIPTED_ROUND_ROBIN_PAGES)

    def _should_stop(self) -> bool:
        """True once the orchestrator reports the experiment complete
        (roadmap Phase 5 termination) -- every long-running loop checks this
        so agents stop acting and their threads exit cleanly instead of
        outliving the experiment. Always False for a non-orchestrated
        account, which has no experiment to end.
        """
        return self.orchestrator is not None and self.orchestrator.should_stop()

    def _idle_while_paused(self) -> bool:
        """Sleeps a beat if the experiment is paused (paper section 3.4's
        `experiment_status: paused`), returning True if it was -- so callers
        can `continue` rather than acting. False when not paused, or when
        this account isn't orchestrated at all.
        """
        if self.orchestrator is None or not self.orchestrator.is_paused():
            return False
        self._log("[runtime] Experiment is paused; idling.")
        self._sleep_interruptible(PAUSED_POLL_SECONDS)
        return True

    def _sleep_interruptible(self, seconds):
        """Sleeps in short slices so a completed/paused experiment is
        noticed promptly rather than after a full multi-hour activation
        interval has elapsed.
        """
        remaining = seconds
        while remaining > 0:
            if self._should_stop():
                return
            time.sleep(min(STOP_CHECK_SLICE_SECONDS, remaining))
            remaining -= STOP_CHECK_SLICE_SECONDS

    def _log(self, message):
        print(f"[{self.account.name}] {message}")

    def fetch_home(self, max_pages=None):
        """Observes the "home" target: X's algorithmic default feed (the
        "Home" tab; GraphQL operation `HomeTimeline`, see `fetch_home_timeline()`).
        """
        self._log("Observing Home (X's algorithmic default feed, GraphQL HomeTimeline).")
        self._scrape_timeline(
            self.fetch_home_timeline, HOME_TIMELINE_PATH, "home",
            track_seen_ids=True, max_pages=max_pages,
        )

    def fetch_following(self, max_pages=None):
        """Observes the "following" target: X's reverse-chronological feed
        (the "Following" tab; GraphQL operation `HomeLatestTimeline`, see
        `fetch_home_latest_timeline()`).
        """
        self._log("Observing Following (X's chronological feed, GraphQL HomeLatestTimeline).")
        self._scrape_timeline(
            self.fetch_home_latest_timeline, HOME_TIMELINE_PATH, "following", max_pages=max_pages
        )

    def fetch_search_timeline(self, max_pages=None):
        query = self.account.search_query
        self._log(f"Observing Search results for query: {query!r}")
        self._scrape_timeline(
            self.fetch_search_latest, SEARCH_TIMELINE_PATH, "search",
            track_seen_ids=True, max_pages=max_pages,
        )

    def _scrape_timeline(self, fetch_fn, timeline_path, target_name, track_seen_ids=False, max_pages=None):
        """Continuously scrolls one timeline, saving each page.

        `max_pages` bounds how many pages this call fetches before
        returning, so _run_scripted_observation can rotate between several
        targets; None keeps the original indefinite behavior.
        """
        cursor = None
        num_tweets = 0
        consecutive_empty_batches = 0
        pages = 0

        if track_seen_ids:
            # Rolling: IDs from the page we just fetched, echoed back to
            # Twitter as `seenTweetIds` on the next request (see
            # _fetch_timeline) so the ranking backend doesn't re-serve them
            # instead of pulling fresh candidates. This mirrors what a real
            # browser session does -- it is NOT a growing full-run history.
            self.seen_tweet_ids = []
            # Separate, capped local cache of every tweet_id collected this
            # run, used only to keep exact duplicates out of the JSONL
            # output; never sent over the wire. Kept PER TARGET and across
            # calls, so round-robin rotations don't reset the cache (or let
            # one target's ids suppress another's).
            if not hasattr(self, "_collected_tweet_ids_by_target"):
                self._collected_tweet_ids_by_target = {}
            self._collected_tweet_ids = self._collected_tweet_ids_by_target.setdefault(
                target_name, OrderedDict()
            )

        while True:
            if self._should_stop():
                self._log("Experiment complete; stopping observation.")
                return
            if self._idle_while_paused():
                continue

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
                # Full Observation Schema (paper section 4) -- see
                # _observation_metadata. Every batch carries the experiment/
                # agent/phase/arm header, so collected feed data is
                # self-describing for analysis.
                record = self._observation_metadata(str(uuid.uuid4()))
                record["target"] = target_name
                record["tweets"] = tweets
                self.file_manager.save_data(record)

            pages += 1
            if max_pages is not None and pages >= max_pages:
                return

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

        if action == "follow" and self.orchestrator is not None:
            # Orchestrated accounts (roadmap Phase 5) share ONE
            # cross-account follow-rate budget instead of each account's
            # own FOLLOW_RATE_LIMIT window -- closes the roadmap's flagged
            # gap ("must also respect the 15/15min follow rate limit
            # across concurrently-active agents"). Covers every follow
            # path (cold-start, follow_all, and agent-runtime decisions),
            # not just _throttled_follow's callers.
            self.orchestrator.acquire_follow_slot(self.account.name)

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
            elif action == "like":
                self.agent_state.mark_liked(target)
            elif action == "retweet":
                self.agent_state.mark_retweeted(target)
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

        structured = {
            "execution_status": "success" if success else "failure",
            "system_response": {k: v for k, v in result.items() if k != "success"},
        }
        self._record_engagement(action, target, structured)
        return structured

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

        # Cold-start engagement runs the SAME decision cycle as the later
        # phases, restricted to follow/like/repost (paper section 3.2: "At
        # each activation, the agent executes the same runtime decision
        # cycle used during later experimental phases. During
        # initialization, however, available actions are restricted"). That
        # makes each agent's initialization reflect its own persona --
        # which is the whole point, since this phase is what shapes the
        # baseline feed the experiment then measures.
        cold_start_actions = self._allowed_actions_for_phase("initialization")
        persona_prompt = None
        engine = None
        if account.agent_runtime_enabled:
            persona_prompt = account.get_persona_prompt_text()
            engine = self._get_decision_engine()
        else:
            self._log(
                "[cold-start] No sockpuppet_config.persona_prompt configured, so engagement "
                "choices fall back to a random like/repost -- configure a persona for "
                "persona-driven initialization."
            )

        def follows_done():
            return len(self.agent_state.following_list)

        def engagements_done():
            return self.agent_state.count_interactions(phase="initialization", actions=("like", "retweet"))

        while follows_done() < min_follows or engagements_done() < min_engagements:
            if self._should_stop():
                self._log("[cold-start] Experiment complete; stopping initialization.")
                return
            if self._idle_while_paused():
                continue
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

                if engine is not None:
                    # Persona-driven: the decision cycle records the
                    # interaction and the runtime_log row itself, using
                    # phase="initialization" so engagements_done() counts
                    # them and _allowed_actions_for_phase keeps mute out.
                    self._log(
                        f"[cold-start] Evaluating tweet {tweet_id} "
                        f"({engagements_done()}/{min_engagements} engagements)"
                    )
                    self._decide_and_execute(
                        persona_prompt, engine, tweet, "home",
                        "initialization", cold_start_actions, str(uuid.uuid4()),
                    )
                else:
                    action = random.choice(["like", "retweet"])
                    self._log(
                        f"[cold-start] {action}-ing tweet {tweet_id} "
                        f"({engagements_done()}/{min_engagements})"
                    )
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

    def _cold_start_seed(self):
        """Base seed for the cold-start follow draw, in precedence order:
        an explicit `initialization_params.follow_seed`, then the
        experiment's own randomization seed (read from PERSISTED
        orchestrator state where orchestrated, so a later config edit can't
        change an already-run draw), then the config value, then 0.

        Always resolves to something, so the draw is always reproducible
        and the seed actually used is always logged.
        """
        params = self.account.initialization_params or {}
        if params.get("follow_seed") is not None:
            return params["follow_seed"]
        if self.orchestrator is not None:
            persisted = self.orchestrator.state.get_randomization_seed()
            if persisted is not None:
                return persisted
        configured = (self.account.experiment_design.get("randomization") or {}).get("seed")
        if configured is not None:
            return configured
        return 0

    def _load_cold_start_candidates(self):
        """Seeded random mix of user_ids drawn from the account lists named
        in `sockpuppet_config.account_lists` (e.g. an untrustworthy-source
        list and a trustworthy-source list).

        Two modes, both reproducible from the seed:
          - `initialization_params.follow_mix` set: draw exactly that many
            from each named list, so the composition of the follow graph is
            a controlled design parameter rather than a side effect of how
            long each file happens to be.
          - otherwise: draw from the pooled union, so the mix lands roughly
            proportional to list sizes.
        `initialization_params.max_follows` caps the total either way.

        The draw is namespaced per account, so every sockpuppet gets a
        DIFFERENT mix while the whole thing stays reproducible from the one
        recorded seed -- the same approach the intervention's per-sockpuppet
        sampling uses.
        """
        params = self.account.initialization_params or {}
        by_list = OrderedDict()
        seen = set()
        for list_name in self.account.cold_start_account_lists:
            path = self.account.get_account_list_path(list_name)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            ids = []
            for entry in data.get("users", []):
                user_id = entry.get("user_id")
                if user_id and user_id not in seen:
                    seen.add(user_id)
                    ids.append(user_id)
            by_list[list_name] = ids

        seed = self._cold_start_seed()
        rng = seeded_rng(seed, self.account.name, "cold_start_follows")
        max_follows = params.get("max_follows")
        mix = params.get("follow_mix") or {}

        origin = {}
        if mix:
            selected = []
            for list_name, wanted in mix.items():
                pool = by_list.get(list_name)
                if pool is None:
                    raise ValueError(
                        f"Account '{self.account.name}': initialization_params.follow_mix names "
                        f"{list_name!r}, which is not in sockpuppet_config.account_lists "
                        f"({list(by_list)})."
                    )
                take = min(int(wanted), len(pool))
                if take < int(wanted):
                    self._log(
                        f"[cold-start] follow_mix wants {wanted} from {list_name!r} but it only "
                        f"has {len(pool)}; taking {take}."
                    )
                drawn = rng.sample(pool, take)
                for user_id in drawn:
                    origin[user_id] = list_name
                selected.extend(drawn)
        else:
            pool = [user_id for ids in by_list.values() for user_id in ids]
            for list_name, ids in by_list.items():
                for user_id in ids:
                    origin[user_id] = list_name
            take = len(pool) if max_follows is None else min(int(max_follows), len(pool))
            selected = rng.sample(pool, take)

        # Shuffle the combined selection so the lists aren't followed in
        # blocks -- a run of untrustworthy follows back to back is both
        # unrealistic and a needlessly strong automation signal.
        rng.shuffle(selected)
        if max_follows is not None:
            selected = selected[: int(max_follows)]

        composition = OrderedDict(
            (name, sum(1 for user_id in selected if origin.get(user_id) == name))
            for name in by_list
        )
        summary = ", ".join(f"{name}={count}" for name, count in composition.items())
        self._log(
            f"[cold-start] follow candidates: {len(selected)} ({summary}) "
            f"seed={seed} mode={'follow_mix' if mix else 'pooled'}"
        )
        # Persisted so the realized composition is recoverable from the
        # account's own state, not only from a console line.
        self.agent_state.remember("cold_start_follow_draw", {
            "seed": seed,
            "mode": "follow_mix" if mix else "pooled",
            "max_follows": max_follows,
            "selected_count": len(selected),
            "composition": dict(composition),
        })
        return selected

    def _throttled_follow(self, user_id) -> dict:
        """Self-throttles to stay under FOLLOW_RATE_LIMIT per
        FOLLOW_RATE_WINDOW_SECONDS, sleeping ahead of a 429 instead of
        reacting to one. Returns execute_action()'s structured result.

        Orchestrated accounts (roadmap Phase 5) delegate straight to
        execute_action(), which already acquires a slot from the shared,
        cross-account budget -- also going through this instance's own
        self._follow_timestamps would double-throttle against a budget
        this account no longer owns alone.
        """
        if self.orchestrator is not None:
            return self.execute_action("follow", user_id)

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

    def _build_experiment_context(self, allowed_actions=None) -> dict:
        """experiment_context (paper section 3.3): experiment_id/phase/
        treatment_arm/intervention_status/current_time/runtime_constraints.
        Orchestrated accounts (`experiment_design.treatment_arms`
        configured, roadmap Phase 5) get the experiment fields from the
        shared ExperimentOrchestrator's real state; everything else keeps
        reading the static per-account config stand-in.
        """
        account = self.account
        if self.orchestrator is not None:
            context = self.orchestrator.build_experiment_context(account.name)
        else:
            context = {
                "experiment_id": account.experiment_id,
                "phase": account.experiment_phase,
                "treatment_arm": account.treatment_arm,
                "intervention_status": "none",
                "current_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        # `runtime_constraints` is the last experiment_context field in the
        # paper's section 3.3 listing. Rendered as a flat string because the
        # whole context dict is dumped key: value into the prompt -- a
        # nested dict would reach the model as a Python repr.
        constraints = [f"allowed_actions={','.join(allowed_actions)}"] if allowed_actions else []
        constraints.append(f"follow_rate_limit={FOLLOW_RATE_LIMIT}/{FOLLOW_RATE_WINDOW_SECONDS // 60}min")
        context["runtime_constraints"] = "; ".join(constraints)
        return context

    def _observation_metadata(self, observation_id) -> dict:
        """The paper's Observation Schema header (section 4): observation_id,
        timestamp_utc, experiment_id, agent_id, phase, treatment_arm. Every
        saved platform observation carries this, so collected feed data can
        be split by phase and arm during analysis without cross-referencing
        the orchestrator's own logs.
        """
        account = self.account
        orchestrated_phase = self._orchestrated_phase()
        return {
            "observation_id": observation_id,
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "experiment_id": account.experiment_id,
            "agent_id": account.agent_id,
            "phase": orchestrated_phase if orchestrated_phase is not None else account.experiment_phase,
            "treatment_arm": (
                self.orchestrator.get_treatment_arm(account.name)
                if self.orchestrator is not None else account.treatment_arm
            ),
        }

    def _record_engagement(self, action, target, result):
        """Engagement History stream (paper section 4: "All engagement
        behaviors performed by the sockpuppet"). Written for every real
        action this account takes, wherever it originated -- cold-start,
        follow_all, an Agent Runtime decision, or an orchestrator-triggered
        intervention -- so the engagement log is a complete record rather
        than only what the LLM chose.
        """
        file_manager = getattr(self, "engagement_file_manager", None)
        if file_manager is None:
            return
        entry = self._observation_metadata(str(uuid.uuid4()))
        entry.pop("observation_id")
        entry.update({
            "action": action,
            "target_object": target,
            "execution_status": result.get("execution_status"),
            "system_response": result.get("system_response"),
        })
        file_manager.save_data(entry)

    def write_intervention_history(self, record: dict):
        """Intervention History stream (paper section 4: "Records describing
        when experimental interventions were applied and which accounts were
        affected"). Called by the ExperimentOrchestrator after it runs this
        account's intervention; a no-op unless the account enabled the
        stream via `data_collection.targets`.
        """
        file_manager = getattr(self, "intervention_file_manager", None)
        if file_manager is None:
            return
        file_manager.save_data({
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            **record,
        })

    def _try_consume_action_slot(self, action) -> tuple:
        """Per-action pacing check (`sockpuppet_config.action_rate_limits`),
        returning (allowed, retry_after_seconds).

        Routes to the orchestrator for an orchestrated account so the
        budget is visible in the reproducibility log; a non-orchestrated
        account keeps its own equivalent budgets, so pacing doesn't depend
        on being part of a formal experiment.
        """
        if self.orchestrator is not None:
            return self.orchestrator.try_consume_action_slot(self.account.name, action)

        if not hasattr(self, "_own_action_budgets"):
            self._own_action_budgets = build_budgets(self.account.action_rate_limits)
        budget = self._own_action_budgets.get(action)
        if budget is None:
            return True, 0.0
        return budget.try_consume()

    def _current_phase(self) -> str:
        """The phase to record an action under RIGHT NOW -- the
        orchestrator's live phase for an orchestrated account, else the
        static per-account config value. Must be re-read per action rather
        than cached per activation: an activation spans many minutes, and a
        phase transition partway through has to apply to the rest of it.
        """
        if self.orchestrator is not None:
            return self.orchestrator.get_phase(self.account.name)
        return self.account.experiment_phase

    def _orchestrated_phase(self):
        """Current orchestrator phase for this account, or None if it isn't
        orchestrated (roadmap Phase 5) -- used to tag raw observation
        records (as opposed to `_build_experiment_context()`'s full
        decision-prompt context) so they can be split by experiment phase
        during analysis without cross-referencing the orchestrator's own
        phase-transition log. None (not a static-config fallback) for a
        non-orchestrated account, so its saved records stay unchanged.
        """
        if self.orchestrator is None:
            return None
        return self.orchestrator.get_phase(self.account.name)

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
        - "repeat": downgraded because it would repeat something already
          done -- an (action, target) pair that already succeeded, or a
          follow/mute of an account already followed/muted.
        - "failed": downgraded because the decision itself couldn't be
          honored -- either the action isn't allowed this phase, or this
          post's own data is missing the id the chosen action needs.
        - "rate_capped": downgraded because this account's configured
          pacing budget for that action (`action_rate_limits`) has no slot
          free right now.
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

        # The interaction-history check above only sees what THIS decision
        # cycle recorded. agent_state's own platform-state lists outlive it
        # -- follow_all()'s startup follows and the orchestrator's
        # intervention mutes never touch interaction_history at all, and a
        # trimmed or reset history loses the rest. Both were observed live
        # on 2026-09-09: 2 redundant follows on follow_list accounts, and a
        # re-like that came back `139 has already favorited`. These are
        # platform no-ops, but they still consume a rate slot, still count
        # toward a phase's max_action_count, and land in the engagement log
        # as failures that aren't really failures.
        already = {
            "follow": self.agent_state.is_following,
            "mute": self.agent_state.is_muted,
            "like": self.agent_state.is_liked,
            "retweet": self.agent_state.is_retweeted,
        }.get(action)
        if already is not None and already(target):
            self._log(
                f"[runtime] LLM chose {action} on target_object={target!r}, which this account "
                f"has already done; rejecting as no_action."
            )
            return "no_action", None, "repeat"

        # Pacing gate, checked LAST so a capped action is only counted
        # against the budget once every other check has passed -- a
        # decision that was going to be rejected anyway must not burn a
        # slot. Consumed per DECISION, so a retried attempt (see
        # _execute_agent_action) doesn't take a second slot: worst-case
        # attempt rate is 2x the configured cap.
        allowed, retry_after = self._try_consume_action_slot(action)
        if not allowed:
            self._log(
                f"[runtime] {action} on target_object={target!r} is within this account's "
                f"pacing budget limit ({retry_after:.0f}s until the next slot); "
                f"rejecting as no_action."
            )
            return "no_action", None, "rate_capped"

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
        if distribution == "poisson":
            # A Poisson process has exponentially distributed inter-arrival
            # times (paper section 3.4 lists Poisson processes among the
            # supported scheduling policies). The config schema gives a
            # window rather than a rate, so the mean is taken as the
            # window's midpoint and draws are clamped into [min, max] --
            # the same order of approximation as truncated_powerlaw above.
            mean = (min_interval + max_interval) / 2
            return min(max(random.expovariate(1.0 / mean), min_interval), max_interval)
        return random.uniform(min_interval, max_interval)

    def _log_runtime_cycle(
        self, observation_id, phase, prompt, decision, action, target,
        observed_tweet_id, result, target_name=None, no_action_reason=None,
        platform_observation=None,
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

        `no_action_reason` (see `_validate_decision`) distinguishes the ways
        a row can end up `no_action` -- "chose" (the model genuinely decided
        not to act), "repeat" (downgraded, already done), "failed"
        (downgraded, disallowed action or missing target data),
        "rate_capped" (downgraded, pacing budget exhausted), and
        "nothing_observed" (no post to decide on at all) -- always present
        when `action == "no_action"`, always None otherwise.
        """
        entry = {
            # `log_id` uniquely identifies this runtime_log row (paper
            # section 4's Runtime Log Schema); `observation_id` still groups
            # every row produced by the same activation.
            "log_id": str(uuid.uuid4()),
            "observation_id": observation_id,
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "experiment_id": self.account.experiment_id,
            "agent_id": self.account.agent_id,
            "phase": phase,
            "observed_tweet_id": observed_tweet_id,
            "target_name": target_name,
            "selected_action": action,
            "target_object": target,
            "no_action_reason": no_action_reason,
            "execution_result": result,
            "execution_status": result["execution_status"],
            "system_response": result["system_response"],
        }
        if self.account.logging_level != "minimal":
            entry["constructed_prompt"] = prompt
            entry["model_output"] = decision.model_dump() if decision is not None else None
            # `platform_observation` (paper section 3.3 stage 5): the actual
            # post this decision concerned. Only under `logging: full` --
            # it's the single largest field per row.
            entry["platform_observation"] = platform_observation
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
        engine = self._get_decision_engine()

        while True:
            if self._should_stop():
                self._log("[runtime] Experiment complete; stopping the decision loop.")
                return
            if self._idle_while_paused():
                continue

            try:
                self._run_activation(persona_prompt, engine)
            except Exception as e:
                # Failure recovery (paper section 3.4 responsibility #6):
                # an orchestrated agent records the failure centrally and
                # reschedules per the recovery policy instead of letting one
                # bad activation kill this account's thread for good. A
                # non-orchestrated account keeps the old behavior (the
                # exception propagates and stops that account).
                if self.orchestrator is None:
                    raise
                wait = self.orchestrator.record_activation_failure(account.name, repr(e))
                if wait is None:
                    self._log(
                        f"[runtime] Too many consecutive activation failures; stopping this "
                        f"account. Last error: {e!r}"
                    )
                    return
                self._log(f"[runtime] Activation failed ({e!r}); retrying in {wait:.0f}s.")
                self._sleep_interruptible(wait)
                continue

            if self.orchestrator is not None:
                self.orchestrator.record_activation_success(account.name)

            # Re-check before announcing/scheduling the next activation --
            # an activation that returned early because the experiment just
            # completed must not log a next-activation time it will never
            # honor.
            if self._should_stop():
                self._log("[runtime] Experiment complete; stopping the decision loop.")
                return

            interval = self._sample_activation_interval()
            self._log(f"[runtime] Next activation in {interval:.0f}s.")
            self._sleep_interruptible(interval)

    def _get_decision_engine(self):
        """One DecisionEngine per scraper, shared by the Agent Runtime and
        persona-driven cold-start (they run the same decision cycle).
        """
        if getattr(self, "_decision_engine", None) is None:
            self._decision_engine = DecisionEngine()
        return self._decision_engine

    def _run_activation(self, persona_prompt, engine):
        """One activation: Observation -> per-post (Prompt Construction ->
        Decision -> Execution -> Logging). Split out of run_agent_runtime()
        so the loop above can wrap a whole activation in the orchestrator's
        failure-recovery policy.
        """
        account = self.account
        phase = self._current_phase()

        if self.orchestrator is not None:
            # Cheap, defensive check -- belt-and-suspenders against any
            # registration/tick-timing edge case (roadmap Phase 5).
            self.orchestrator.maybe_run_pending_intervention(account.name)

        platform_state = self.observe()
        observation_id = str(uuid.uuid4())
        observation_record = self._observation_metadata(observation_id)
        observation_record["platform_state"] = platform_state
        self.file_manager.save_data(observation_record)

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
            return

        for target_name, tweets in platform_state.items():
            for tweet in tweets:
                if self._should_stop():
                    self._log("[runtime] Experiment completed mid-activation; stopping.")
                    return
                # Phase (and therefore the allowed action set) is re-read
                # PER POST, not once per activation. An activation over a
                # full feed page runs for many minutes, so a phase that
                # transitions partway through must apply to the rest of
                # this activation -- otherwise actions get recorded under
                # the phase that was current when the activation started,
                # which is exactly the attribution the phase machine exists
                # to get right.
                current_phase = self._current_phase()
                self._decide_and_execute(
                    persona_prompt, engine, tweet, target_name,
                    current_phase, self._allowed_actions_for_phase(current_phase), observation_id,
                )

    def _decide_and_execute(
        self, persona_prompt, engine, tweet, target_name, phase, allowed_actions, observation_id,
    ):
        """Prompt Construction -> Decision -> Execution -> Logging for ONE
        observed post (paper section 3.3 stages 2-5). Shared by the Agent
        Runtime and persona-driven cold-start, which the paper specifies run
        the same cycle and differ only in the allowed action set.
        """
        experiment_context = self._build_experiment_context(allowed_actions)
        # Cold-start passes phase="initialization" explicitly, which the
        # orchestrator's own phase (pre_treatment) doesn't know about --
        # keep the prompt's stated phase consistent with what gets logged.
        experiment_context["phase"] = phase
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
        result = self._execute_agent_action(action, target)

        self.agent_state.record_interaction(
            action, target, phase=phase,
            execution_status=result["execution_status"],
            system_response=result["system_response"],
        )
        self._log_runtime_cycle(
            observation_id, phase, prompt, decision, action, target,
            tweet.get("tweet_id"), result, target_name=target_name,
            no_action_reason=no_action_reason, platform_observation=tweet,
        )

        # A phase whose exit condition is max_action_count should end on the
        # action that meets it, not up to a full tick interval later -- so
        # check as soon as this action is on the record.
        if action != "no_action" and self.orchestrator is not None:
            self.orchestrator.maybe_advance_phase()

        return action, target, result

    def _execute_agent_action(self, action, target) -> dict:
        """execute_action() plus ONE bounded retry for a transient failure.

        Live testing (2026-09-09) saw the same tweet_id fail `retweet` with
        a 404 and then succeed ~12 minutes later, so some failures here are
        genuinely transient and a single attempt under-reports what the
        agent actually managed to do.

        A platform REFUSAL is never retried (see
        PLATFORM_REFUSAL_MARKERS) -- when the platform says the request
        looked automated, or refuses on authorization/rate-limit grounds,
        that's a deliberate decline and retrying it would be circumventing
        an anti-abuse control rather than recovering from an error. Each
        attempt is logged to the engagement stream on its own, so the
        record shows what was actually attempted.
        """
        result = self.execute_action(action, target)
        for attempt in range(2, AGENT_ACTION_MAX_ATTEMPTS + 1):
            if result["execution_status"] != "failure":
                return result
            reason = str((result.get("system_response") or {}).get("reason", "")).lower()
            if any(marker in reason for marker in PLATFORM_REFUSAL_MARKERS):
                self._log(
                    f"[runtime] {action} on {target!r} was declined by the platform; not retrying."
                )
                return result
            self._log(
                f"[runtime] {action} on {target!r} failed transiently; "
                f"retrying once in {AGENT_ACTION_RETRY_SECONDS}s."
            )
            self._sleep_interruptible(AGENT_ACTION_RETRY_SECONDS)
            result = self.execute_action(action, target)
        return result


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
