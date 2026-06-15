import httpx
import http.client
import time
import json
from scraper.base import BaseScraper
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
        
        self.favorite_tweet("2063797483629629691")
        self.mute_user("13298072")

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

    # Clean this and mute up, they fucking disgusting bro
    def favorite_tweet(self, tweet_id: str):
        conn = http.client.HTTPSConnection("x.com")
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "content-type": "application/json",
            "origin": "https://x.com",
            "priority": "u=1, i",
            "referer": "https://x.com/home",
            "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "x-csrf-token": "6d0554423a95c3ff492556b40af5d8c252425a12fbb8267290379fd24c99898586ed6016e5cf19217574dc21be55c488fcc50556f4144be1cebd3ffcf18d939b2a331bddb9d988cbfed5577a7e9ee5e2",
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "cookie": 'night_mode=2; guest_id=v1%3A176765025551579069; guest_id_marketing=v1%3A176765025551579069; guest_id_ads=v1%3A176765025551579069; __cuid=e73544982ab9463a902eea7a266132cc; kdt=Wp1rqsJgBz48Pv70RDpKgiRaLFFdSQunr6WDZoIN; _ga_KEWZ1G5MB3=GS2.2.s1767902968$o1$g1$t1767903177$j60$l0$h0; _ga=GA1.1.1582729253.1767902597; _ga_RJGMY4G45L=GS2.1.s1767902597$o1$g1$t1767903887$j60$l0$h0; personalization_id="v1_M/JjNXndIUT6LBdPANLzDQ=="; g_state={"i_l":0,"i_ll":1769620525840}; auth_token=1e5f32f9cd453a287e69cf5944f0bf58eb68deb0; ct0=6d0554423a95c3ff492556b40af5d8c252425a12fbb8267290379fd24c99898586ed6016e5cf19217574dc21be55c488fcc50556f4144be1cebd3ffcf18d939b2a331bddb9d988cbfed5577a7e9ee5e2; twid=u%3D2009357161542090752; _twpid=tw.1776900248126.192889039345785615; lang=en; __cuid=e73544982ab9463a902eea7a266132cc; __cf_bm=qRojvqQlOZP83CMc_MR8B8dw2gZ1K.Mwh6BroBZsE_k-1780959913.7579544-1.0.1.1-Sz8x.PEgGbI0K1KV9CigWpd20Lig_ts2EtDTC9UgIF39g0SgZbb6gKWzGVX152H3HMiyO6HxsNHE_uGeUwcWULYwQXqj00rSu3moMZnb.gBTK5Nx6TrHD5HNF7TuwBrS; external_referer=padhuUp37zjgzgv1mFWxJ12Ozwit7owX|0|8e8t2xd8A2w%3D; __gads=ID=911e5c8f2a98b584:T=1780959918:RT=1780959918:S=ALNI_MY01w-U9S7SGl55gUJmtgwor_N4KQ; __gpi=UID=000013c218398b1c:T=1780959918:RT=1780959918:S=ALNI_MYtXfiuDWH5O6Ub6pdVOkIfaRrnOw; __eoi=ID=ffcf43431e50ec40:T=1780959918:RT=1780959918:S=AA-AfjY5o5ouju0dEJzRDjEMK8oG',
        }
        json_data = {
            "variables": {
                "tweet_id": f'{tweet_id}',
            },
            "queryId": "lI07N6Otwv1PhnEgXILM7A",
        }
        conn.request(
            "POST",
            "/i/api/graphql/lI07N6Otwv1PhnEgXILM7A/FavoriteTweet",
            json.dumps(json_data),
            # '{"variables":{"tweet_id":"2063797483629629691"},"queryId":"lI07N6Otwv1PhnEgXILM7A"}',
            headers,
        )
        response = conn.getresponse()
        print(response.status, response.reason)
        
    def mute_user(self, user_id: str):
        conn = http.client.HTTPSConnection('x.com')
        headers = {
            'accept': '*/*',
            'accept-language': 'en-US,en;q=0.9',
            'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
            'content-type': 'application/x-www-form-urlencoded',
            'origin': 'https://x.com',
            'priority': 'u=1, i',
            'referer': 'https://x.com/home',
            'sec-ch-ua': '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-origin',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
            'x-csrf-token': '6d0554423a95c3ff492556b40af5d8c252425a12fbb8267290379fd24c99898586ed6016e5cf19217574dc21be55c488fcc50556f4144be1cebd3ffcf18d939b2a331bddb9d988cbfed5577a7e9ee5e2',
            'x-twitter-active-user': 'yes',
            'x-twitter-auth-type': 'OAuth2Session',
            'x-twitter-client-language': 'en',
            'cookie': 'night_mode=2; guest_id=v1%3A176765025551579069; guest_id_marketing=v1%3A176765025551579069; guest_id_ads=v1%3A176765025551579069; __cuid=e73544982ab9463a902eea7a266132cc; kdt=Wp1rqsJgBz48Pv70RDpKgiRaLFFdSQunr6WDZoIN; _ga_KEWZ1G5MB3=GS2.2.s1767902968$o1$g1$t1767903177$j60$l0$h0; _ga=GA1.1.1582729253.1767902597; _ga_RJGMY4G45L=GS2.1.s1767902597$o1$g1$t1767903887$j60$l0$h0; personalization_id="v1_M/JjNXndIUT6LBdPANLzDQ=="; g_state={"i_l":0,"i_ll":1769620525840}; auth_token=1e5f32f9cd453a287e69cf5944f0bf58eb68deb0; ct0=6d0554423a95c3ff492556b40af5d8c252425a12fbb8267290379fd24c99898586ed6016e5cf19217574dc21be55c488fcc50556f4144be1cebd3ffcf18d939b2a331bddb9d988cbfed5577a7e9ee5e2; twid=u%3D2009357161542090752; _twpid=tw.1776900248126.192889039345785615; lang=en; __cuid=e73544982ab9463a902eea7a266132cc; external_referer=padhuUp37zjgzgv1mFWxJ12Ozwit7owX|0|8e8t2xd8A2w%3D; __cf_bm=BMxzUMRa9aow8mi9ZEw3Et.cJHxK4QNieKxv7V0U1Js-1780960813.4330463-1.0.1.1-kGvRuBsfqNBphqJQWykl0oD.H7BMe2ppfECbwdB5Xgxq54Y0hbqkaA_jU1gNNaQ58fpua.8W1kdJ0QkNb3U0KYWnUWFv14BtX7C84b273vdh3aXkAFWkG6jO3P7gRFzC; __gads=ID=911e5c8f2a98b584:T=1780959918:RT=1780961480:S=ALNI_MY01w-U9S7SGl55gUJmtgwor_N4KQ; __gpi=UID=000013c218398b1c:T=1780959918:RT=1780961480:S=ALNI_MYtXfiuDWH5O6Ub6pdVOkIfaRrnOw; __eoi=ID=ffcf43431e50ec40:T=1780959918:RT=1780961480:S=AA-AfjY5o5ouju0dEJzRDjEMK8oG',
        }
        conn.request(
            'POST',
            '/i/api/1.1/mutes/users/create.json',
            f'user_id={user_id}',
            headers
        )
        response = conn.getresponse()
        print(response.status, response.reason)

    # This also throws 403, need x-client-transaction-id :(
    def follow_user(self, user_id: str):
        conn = http.client.HTTPSConnection("x.com")
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://x.com",
            "priority": "u=1, i",
            "referer": "https://x.com/NASA",
            "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "x-client-transaction-id": "DWURcUaFanJue6h533LznXR+cjpMiY6Fa9pp+0n/cLr+COlJRU4vn6VsJdlfaZKxOf/VxAj3Jd8nh8lw1Pe/5+9eI+gQDg",
            "x-csrf-token": "6d0554423a95c3ff492556b40af5d8c252425a12fbb8267290379fd24c99898586ed6016e5cf19217574dc21be55c488fcc50556f4144be1cebd3ffcf18d939b2a331bddb9d988cbfed5577a7e9ee5e2",
            "x-twitter-active-user": "yes",
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-client-language": "en",
            "cookie": 'night_mode=2; guest_id=v1%3A176765025551579069; guest_id_marketing=v1%3A176765025551579069; guest_id_ads=v1%3A176765025551579069; __cuid=e73544982ab9463a902eea7a266132cc; kdt=Wp1rqsJgBz48Pv70RDpKgiRaLFFdSQunr6WDZoIN; _ga_KEWZ1G5MB3=GS2.2.s1767902968$o1$g1$t1767903177$j60$l0$h0; _ga=GA1.1.1582729253.1767902597; _ga_RJGMY4G45L=GS2.1.s1767902597$o1$g1$t1767903887$j60$l0$h0; personalization_id="v1_M/JjNXndIUT6LBdPANLzDQ=="; g_state={"i_l":0,"i_ll":1769620525840}; auth_token=1e5f32f9cd453a287e69cf5944f0bf58eb68deb0; ct0=6d0554423a95c3ff492556b40af5d8c252425a12fbb8267290379fd24c99898586ed6016e5cf19217574dc21be55c488fcc50556f4144be1cebd3ffcf18d939b2a331bddb9d988cbfed5577a7e9ee5e2; twid=u%3D2009357161542090752; _twpid=tw.1776900248126.192889039345785615; lang=en; __cuid=e73544982ab9463a902eea7a266132cc; __cf_bm=cASzBSq7OirP1FBg2Zb8v5.AWBuB8jDALnFYQfawI9M-1780038758.1754198-1.0.1.1-yPfsFJmerO0G_iHPxGGrgY3IL4Vr88_Qbkh53kapTj5hnir21vOS4jkcdCGVS_b6rFqcEWrwvIvWpPdu0DNnPPewyZZ3ayylie8PbKLflR31POLa0rTMys7SfPRF.fel',
        }
        conn.request(
            "POST",
            "/i/api/1.1/friendships/create.json",
            f"include_profile_interstitial_type=1&include_blocking=1&include_blocked_by=1&include_followed_by=1&include_want_retweets=1&include_mute_edge=1&include_can_dm=1&include_can_media_tag=1&include_ext_is_blue_verified=1&include_ext_verified_type=1&include_ext_profile_image_shape=1&skip_status=1&user_id={user_id}",
            headers,
        )
        response = conn.getresponse()
        print(response.status, response.reason)

    # Currently throws 403 for multiple users, but worked on single user. How fix ::think::
    def follow_all(self):
        with open(FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        for user_id in data["users"]:
            print(f"Following user {user_id}...")
            self.follow_user(user_id)
            time.sleep(1)


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
