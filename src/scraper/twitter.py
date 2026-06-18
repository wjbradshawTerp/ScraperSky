import httpx
import time
import json
from scraper.base import BaseScraper
from scraper.x_client import fetch_and_init
from config import settings
from storage.file_manager import FileManager

FILE_PATH = "/app/follow_user_ids.json"

TWITTER_HOME_LATEST_TIMELINE_HASH = "KLMY6cZZUfQrLubs5DHHtQ"
TWITTER_HOME_TIMELINE_HASH = "L8Lb9oomccM012S7fQ-QKA"


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

        self.follow_all()

        if self.mode == "home":
            self.scrape_home()
        elif self.mode == "follows":
            self.scrape_follows()

    def scrape_home(self):
        print("Scraping Twitter home timeline.")

        cursor = None
        num_tweets = 0

        while True:
            data = self.fetch_home_latest(cursor)
            time.sleep(settings.SCROLL_DELAY)
            tweets, cursor = parse_home_timeline(data)
            num_tweets += len(tweets)
            print(f"{num_tweets} tweets collected")

            self.file_manager.save_data(tweets)

    def scrape_follows(self):
        print("Scraping Twitter for all tweets from followed accounts.")

        cursor = None
        num_tweets = 0

        while True:
            data = self.fetch_follow_latest(cursor)
            time.sleep(settings.SCROLL_DELAY)
            tweets, cursor = parse_follow_timeline(data)
            num_tweets += len(tweets)
            print(f"{num_tweets} tweets collected")

            self.file_manager.save_data(tweets)

    def fetch_follow_latest(self, cursor=None):
        url = f"https://x.com/i/api/graphql/{TWITTER_HOME_LATEST_TIMELINE_HASH}/HomeLatestTimeline"

        variables = {
            "count": 20,
            "cursor": cursor,
            "includePromotedContent": True,
            "latestControlAvailable": True,
            "withVoice": True,
        }

        params = {
            "variables": json.dumps(variables),
            "features": json.dumps({}),
        }

        r = self.client.get(url, params=params)

        try:
            data = r.json()
        except Exception:
            print("NON JSON RESPONSE:")
            print(r.text[:500])
            raise

        return data

    def fetch_home_latest(self, cursor=None):
        url = f"https://x.com/i/api/graphql/{TWITTER_HOME_TIMELINE_HASH}/HomeTimeline"

        variables = {
            "count": 20,
            "cursor": cursor,
            "includePromotedContent": True,
            "latestControlAvailable": True,
            "withVoice": True,
        }

        params = {
            "variables": json.dumps(variables),
            "features": json.dumps({}),
        }

        r = self.client.get(url, params=params)

        try:
            data = r.json()
        except Exception:
            print("NON JSON RESPONSE:")
            print(r.text[:500])
            raise

        return data

    def _txid(self, method: str, path: str) -> str:
        return self.ct.generate(method, path)

    def favorite_tweet(self, tweet_id: str):
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

    def mute_user(self, user_id: str):
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

    def follow_user(self, user_id: str) -> bool:
        """Returns True on success, False on rate-limit, raises on other errors."""
        path = "/i/api/1.1/friendships/create.json"
        body = (
            "include_profile_interstitial_type=1&include_blocking=1&include_blocked_by=1"
            "&include_followed_by=1&include_want_retweets=1&include_mute_edge=1"
            "&include_can_dm=1&include_can_media_tag=1&include_ext_is_blue_verified=1"
            "&include_ext_verified_type=1&include_ext_profile_image_shape=1"
            f"&skip_status=1&user_id={user_id}"
        )
        txid = self._txid("POST", path)
        r = self.client.post(
            f"https://x.com{path}",
            headers={
                "x-client-transaction-id": txid,
                "content-type": "application/x-www-form-urlencoded",
            },
            content=body,
        )
        print(r.status_code, r.reason_phrase)
        if r.status_code == 429:
            reset = r.headers.get("x-rate-limit-reset")
            limit = r.headers.get("x-rate-limit-limit")
            remaining = r.headers.get("x-rate-limit-remaining")
            print(f"  Rate limited — limit={limit}, remaining={remaining}, resets_at={reset}")
            if reset:
                import datetime
                reset_dt = datetime.datetime.fromtimestamp(int(reset), tz=datetime.timezone.utc)
                print(f"  Reset time (UTC): {reset_dt.strftime('%Y-%m-%d %H:%M:%S')}")
            return False
        if r.status_code != 200:
            print(f"  response: {r.text[:500]}")
        return r.status_code == 200

    def follow_all(self):
        with open(FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        for entry in data["users"]:
            user_id = entry["user_id"]
            print(f"Following user {user_id} ({entry.get('_comment', '')})...")
            if not self.follow_user(user_id):
                break
            time.sleep(5)


def parse_follow_timeline(data):

    tweets = []
    next_cursor = None

    instructions = data["data"]["home"]["home_timeline_urt"]["instructions"]

    for instruction in instructions:

        if "entries" not in instruction:
            continue

        for entry in instruction["entries"]:

            entry_id = entry["entryId"]

            # tweet
            if entry_id.startswith("tweet-"):
                try:
                    content = entry.get("content", {})

                    tweet_data = (
                        entry.get("content", {})
                        .get("itemContent", {})
                        .get("tweet_results", {})
                        .get("result")
                    )

                    if tweet_data:
                        tweets.append(build_tweet_object(tweet_data))

                except Exception as e:
                    print("Error parsing tweet entry:", e)
                    continue

            # Module with multiple tweets
            if entry_id.startswith("home-conversation-"):
                content = entry.get("content", {})

                for item in content["items"]:
                    try:
                        tweet_data = (
                            item.get("item", {})
                            .get("itemContent", {})
                            .get("tweet_results", {})
                            .get("result")
                        )

                        if tweet_data:
                            tweets.append(build_tweet_object(tweet_data))

                    except Exception as e:
                        print("Error parsing module tweet:", e)
                        continue

            # cursor
            if entry_id.startswith("cursor-bottom"):
                next_cursor = entry["content"]["value"]

    return tweets, next_cursor


def parse_home_timeline(data):

    tweets = []
    next_cursor = None

    instructions = data["data"]["home"]["home_timeline_urt"]["instructions"]

    for instruction in instructions:

        if "entries" not in instruction:
            continue

        for entry in instruction["entries"]:

            entry_id = entry["entryId"]

            # tweet
            if entry_id.startswith("tweet-"):
                try:
                    content = entry.get("content", {})

                    tweet_data = (
                        entry.get("content", {})
                        .get("itemContent", {})
                        .get("tweet_results", {})
                        .get("result")
                    )

                    if tweet_data:
                        tweets.append(build_tweet_object(tweet_data))

                except Exception as e:
                    print("Error parsing tweet entry:", e)
                    continue

            # Module with multiple tweets
            if entry_id.startswith("home-conversation-"):
                content = entry.get("content", {})

                for item in content["items"]:
                    try:
                        tweet_data = (
                            item.get("item", {})
                            .get("itemContent", {})
                            .get("tweet_results", {})
                            .get("result")
                        )

                        if tweet_data:
                            tweets.append(build_tweet_object(tweet_data))

                    except Exception as e:
                        print("Error parsing module tweet:", e)
                        continue

            # cursor
            if entry_id.startswith("cursor-bottom"):
                next_cursor = entry["content"]["value"]

    return tweets, next_cursor


def build_tweet_object(tweet):
    legacy = tweet.get("legacy", {})
    retweeted_status = legacy.get("retweeted_status_result", {}).get("result")
    user = tweet.get("core", {}).get("user_results", {}).get("result", {})
    user_legacy = user.get("legacy", {})

    return {
        "tweet_id": tweet.get("rest_id"),
        "created_at": legacy.get("created_at"),
        "text": (
            retweeted_status.get("legacy", {}).get("full_text")
            if retweeted_status
            else legacy.get("full_text")
        ),
        "language": legacy.get("lang"),
        "metrics": {
            "likes": (
                (retweeted_status.get("legacy", {}).get("favorite_count", 0))
                if retweeted_status
                else (legacy.get("favorite_count", 0))
            ),
            "retweets": (
                (retweeted_status.get("legacy", {}).get("retweet_count", 0))
                if retweeted_status
                else (legacy.get("retweet_count", 0))
            ),
            "replies": (
                (retweeted_status.get("legacy", {}).get("reply_count", 0))
                if retweeted_status
                else (legacy.get("reply_count", 0))
            ),
            "quotes": (
                (retweeted_status.get("legacy", {}).get("quote_count", 0))
                if retweeted_status
                else (legacy.get("quote_count", 0))
            ),
            "bookmarks": (
                (retweeted_status.get("legacy", {}).get("bookmark_count", 0))
                if retweeted_status
                else (legacy.get("bookmark_count", 0))
            ),
        },
        "author": {
            "user_id": user.get("rest_id"),
            "username": user.get("core", {}).get("screen_name"),
            "display_name": user.get("core", {}).get("name"),
            "followers": user_legacy.get("followers_count", 0),
        },
    }
