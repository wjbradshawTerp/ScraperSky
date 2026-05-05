import httpx
import time
import json
from scraper.base import BaseScraper
from config import settings
from storage.file_manager import FileManager

TWITTER_HOME_LATEST_TIMELINE_HASH = "KLMY6cZZUfQrLubs5DHHtQ"
TWITTER_HOME_TIMELINE_HASH = "L8Lb9oomccM012S7fQ-QKA"


class TwitterScraper(BaseScraper):
    def run(self):
        print("Running Twitter Scraper with the following parameters:")
        print("Mode:", self.mode)

        self.file_manager = FileManager(settings.OUTPUT_DIR, "twitter", self.mode)

        self.client = httpx.Client(
            headers={
                "authorization": settings.TWITTER_BEARER_TOKEN,
                "x-csrf-token": settings.TWITTER_CSRF_TOKEN,
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "en",
                "cookie": f"auth_token={settings.TWITTER_AUTH_TOKEN}; ct0={settings.TWITTER_CSRF_TOKEN}",
                "user-agent": "Mozilla/5.0",
            },
            timeout=30,
        )

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
