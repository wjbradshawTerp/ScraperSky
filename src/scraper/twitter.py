import datetime
import httpx
import time
import json
from collections import OrderedDict
from scraper.base import BaseScraper
from scraper.x_client import fetch_and_init
from config import settings
from storage.file_manager import FileManager

TWITTER_HOME_LATEST_TIMELINE_HASH = "KLMY6cZZUfQrLubs5DHHtQ"
TWITTER_HOME_TIMELINE_HASH = "3b9_7tltt0hJRef-xm_3sw"

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

# Local output-integrity safety net for scrape_home only; not sent over the wire.
_DEDUP_CACHE_SIZE = 5000


class FetchFailedError(Exception):
    """Raised when a timeline fetch doesn't return usable JSON (bad body, rate limit, etc.)."""


class TwitterScraper(BaseScraper):
    def run(self):
        print("Running Twitter Scraper with the following parameters:")
        print("Mode:", self.mode)

        self.file_manager = FileManager(settings.OUTPUT_DIR, "twitter", self.mode)

        print("Initialising x-client-transaction-id generator...")
        self.ct = fetch_and_init()
        print("Transaction generator ready.")

        self.client = httpx.Client(
            headers={
                "authorization": settings.TWITTER_BEARER_TOKEN,
                "x-csrf-token": settings.TWITTER_CSRF_TOKEN,
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "en",
                "x-twitter-auth-type": "OAuth2Session",
                "cookie": f"auth_token={settings.TWITTER_AUTH_TOKEN}; ct0={settings.TWITTER_CSRF_TOKEN}",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "origin": "https://x.com",
                "referer": "https://x.com/home",
            },
            timeout=30,
        )

        if self.mode == "follows":
            self.retweet("2071232338168545370")
            self.follow_all()
            self.scrape_follows()
        elif self.mode == "home":
            self.scrape_home()

    def scrape_home(self):
        print("Scraping Twitter home timeline.")
        self._scrape_timeline(
            self.fetch_home_latest, TWITTER_HOME_TIMELINE_HASH, track_seen_ids=True
        )

    def scrape_follows(self):
        print("Scraping Twitter for all tweets from followed accounts.")
        self._scrape_timeline(
            self.fetch_follow_latest, TWITTER_HOME_LATEST_TIMELINE_HASH
        )

    def _scrape_timeline(self, fetch_fn, _hash, track_seen_ids=False):
        cursor = None
        num_tweets = 0

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
            result = self._fetch_and_parse_with_retry(fetch_fn, cursor)

            if result is None:
                print(
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
                    print(f"Filtered {skipped} duplicate tweet(s) already collected this run.")

                self.seen_tweet_ids = [
                    tweet.get("tweet_id") for tweet in tweets if tweet.get("tweet_id")
                ]
                tweets = new_tweets

            num_tweets += len(tweets)
            print(f"{num_tweets} tweets collected")
            if tweets:
                self.file_manager.save_data(tweets)

            if not next_cursor or next_cursor == cursor:
                print(
                    "Bottom of pagination reached (no new cursor); "
                    "restarting timeline from the top to keep running indefinitely."
                )
                cursor = None
                continue

            cursor = next_cursor

    def _fetch_and_parse_with_retry(self, fetch_fn, cursor):
        """Fetches and parses the given cursor, retrying the SAME cursor with
        backoff on bad/rate-limited/malformed responses.

        A malformed response (e.g. an empty `{"data": {"home": {}}}` body) is
        transient noise, not a signal that pagination has genuinely ended, so
        it must not be treated the same as a real "no next cursor" result.

        Returns (tweets, next_cursor), or None if all retries are exhausted.
        """
        for attempt in range(1, settings.FETCH_MAX_RETRIES + 1):
            try:
                data = fetch_fn(cursor)
                time.sleep(settings.SCROLL_DELAY)
                return parse_timeline(data)
            except FetchFailedError as e:
                wait = settings.FETCH_RETRY_BACKOFF * attempt
                print(
                    f"Fetch failed (attempt {attempt}/{settings.FETCH_MAX_RETRIES}): "
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

        r = self.client.get(url, params=params)

        if self._handle_rate_limit(r):
            reset = r.headers.get("x-rate-limit-reset")
            if reset:
                wait = max(int(reset) - int(time.time()), 1)
                print(f"Sleeping {wait}s until rate limit resets...")
                time.sleep(wait)
            raise FetchFailedError(f"rate limited (status={r.status_code})")

        try:
            return r.json()
        except Exception:
            print("NON JSON RESPONSE:")
            print(f"status={r.status_code}")
            print(r.text[:500])
            raise FetchFailedError(f"non-JSON response (status={r.status_code})")

    def fetch_follow_latest(self, cursor=None):
        url = f"https://x.com/i/api/graphql/{TWITTER_HOME_LATEST_TIMELINE_HASH}/HomeLatestTimeline"
        variables = {
            "count": 20,
            "includePromotedContent": True,
            "latestControlAvailable": True,
            "withVoice": True,
        }
        return self._fetch_timeline(url, cursor, variables, {})

    def fetch_home_latest(self, cursor=None):
        url = f"https://x.com/i/api/graphql/{TWITTER_HOME_TIMELINE_HASH}/HomeTimeline"
        variables = {
            "count": 20,
            "includePromotedContent": True,
            "withCommunity": True,
        }
        if cursor is None:
            variables["requestContext"] = "launch"
        return self._fetch_timeline(url, cursor, variables, TWITTER_HOME_TIMELINE_FEATURES)

    def _txid(self, method: str, path: str) -> str:
        return self.ct.generate(method, path)

    def _handle_rate_limit(self, r) -> bool:
        """Prints rate limit info and returns True if rate limited."""
        if r.status_code != 429:
            return False
        reset = r.headers.get("x-rate-limit-reset")
        limit = r.headers.get("x-rate-limit-limit")
        remaining = r.headers.get("x-rate-limit-remaining")
        print(f"Rate limited — limit={limit}, remaining={remaining}, resets_at={reset}")
        if reset:
            reset_dt = datetime.datetime.fromtimestamp(
                int(reset), tz=datetime.timezone.utc
            )
            print(f"Reset time (UTC): {reset_dt.strftime('%Y-%m-%d %H:%M:%S')}")
        return True

    def favorite_tweet(self, tweet_id: str) -> bool:
        path = "/i/api/graphql/lI07N6Otwv1PhnEgXILM7A/FavoriteTweet"
        r = self.client.post(
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": self._txid("POST", path),
                "content-type": "application/json",
            },
            content=json.dumps(
                {
                    "variables": {"tweet_id": tweet_id},
                    "queryId": "lI07N6Otwv1PhnEgXILM7A",
                }
            ),
        )
        print(r.status_code, r.reason_phrase)
        if self._handle_rate_limit(r):
            return False
        if r.status_code != 200:
            print(f"response: {r.text[:500]}")
        return r.status_code == 200

    def retweet(self, tweet_id: str) -> bool:
        path = "/i/api/graphql/mbRO74GrOvSfRcJnlMapnQ/CreateRetweet"
        r = self.client.post(
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": self._txid("POST", path),
                "content-type": "application/json",
            },
            content=json.dumps(
                {
                    "variables": {"tweet_id": tweet_id},
                    "queryId": "mbRO74GrOvSfRcJnlMapnQ",
                }
            ),
        )
        print(r.status_code, r.reason_phrase)
        if self._handle_rate_limit(r):
            return False
        if r.status_code != 200:
            print(f"response: {r.text[:500]}")
        return r.status_code == 200

    def mute_user(self, user_id: str) -> bool:
        path = "/i/api/1.1/mutes/users/create.json"
        r = self.client.post(
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": self._txid("POST", path),
                "content-type": "application/x-www-form-urlencoded",
            },
            content=f"user_id={user_id}",
        )
        print(r.status_code, r.reason_phrase)
        if self._handle_rate_limit(r):
            return False
        if r.status_code != 200:
            print(f"response: {r.text[:500]}")
        return r.status_code == 200

    def follow_user(self, user_id: str) -> bool:
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
        print(r.status_code, r.reason_phrase)
        if self._handle_rate_limit(r):
            return False
        if r.status_code != 200:
            print(f"response: {r.text[:500]}")
            return False
        return True

    def follow_all(self):
        with open(settings.FOLLOW_LIST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        for entry in data["users"]:
            user_id = entry["user_id"]
            print(f"Following user {user_id} ({entry.get('_comment', '')})...")
            if not self.follow_user(user_id):
                break
            time.sleep(5)


def parse_timeline(data):
    tweets = []

    if "errors" in data:
        codes = ", ".join(
            f"{err.get('code')}: {err.get('message')}" for err in data["errors"]
        )
        raise FetchFailedError(f"API returned errors: {codes}")

    try:
        instructions = data["data"]["home"]["home_timeline_urt"]["instructions"]
    except KeyError:
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
    user_legacy = user.get("legacy", {})

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
            "username": user_legacy.get("screen_name"),
            "display_name": user_legacy.get("name"),
            "followers": user_legacy.get("followers_count", 0),
        },
    }
