"""Quick health check for the accounts in accounts.yaml.

For each account it makes a few READ-ONLY requests -- the same kind the study
itself makes -- and reports one line per account:

  1. loads x.com/home with the account's session (a dead or logged-out
     session fails here, with a clear message),
  2. fetches one small page of each feed the study collects (`home` and
     `following`), using the study's own request code and query hashes, so
     "OK" means "the study's fetches work for this account",
  3. best effort: asks X which @handle the session belongs to, so a cURL
     copied from the wrong browser container shows up. If you pass --tracker
     (a CSV with `id` and `handle` columns, e.g. the signup tracker), a
     mismatch is flagged as WRONG ACCOUNT.

Statuses:
  OK              both feeds answered normally
  AUTH FAILED     session expired, logged out, or tokens wrong -- extract again
  LOCKED          X wants the account owner to complete a challenge
  SUSPENDED       account suspended
  FLAGGED         X called the request automated (226) -- back off, don't retry
  RATE LIMITED    throttled; the account itself is probably fine, retry later
  WRONG ACCOUNT   the session is a different @handle than --tracker says
  NETWORK ERROR   couldn't reach X from here
  UNEXPECTED      anything else -- the status and X's message are shown

What this CANNOT tell you: it only reads. An account can pass every check and
still be restricted from following/liking/reposting; the first real run's
engagement_log is the true test of that.

Run it sparingly. It talks to X from your IP exactly like the study does, so
don't put it in a loop; accounts are checked one at a time with a pause between
them (--delay).

Usage (from the repo root, in the project environment):
  poetry run python check_accounts.py
  poetry run python check_accounts.py --only p01,p09,p10
  poetry run python check_accounts.py --only p01 --tracker signup_tracker.csv
  poetry run python check_accounts.py --accounts other_accounts.yaml

Exit code is 0 only if every account checked is OK. Tokens are read from the
accounts file and are never printed.
"""
import argparse
import csv
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import httpx  # noqa: E402
import yaml  # noqa: E402

from scraper.x_client import fetch_and_init  # noqa: E402
from scraper.twitter import (  # noqa: E402
    TWITTER_HOME_TIMELINE_HASH, TWITTER_HOME_TIMELINE_FEATURES,
    TWITTER_HOME_LATEST_TIMELINE_HASH, HOME_TIMELINE_PATH, parse_timeline,
)

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
SETTINGS_URL = "https://x.com/i/api/1.1/account/settings.json"
REQUIRED = ("name", "auth_token", "bearer_token", "csrf_token")

# One entry per feed the study collects: (url, variables, features).
FEEDS = {
    "home": (
        f"https://x.com/i/api/graphql/{TWITTER_HOME_TIMELINE_HASH}/HomeTimeline",
        {"count": 5, "includePromotedContent": True, "withCommunity": True, "requestContext": "launch"},
        TWITTER_HOME_TIMELINE_FEATURES,
    ),
    "following": (
        f"https://x.com/i/api/graphql/{TWITTER_HOME_LATEST_TIMELINE_HASH}/HomeLatestTimeline",
        {"count": 5, "includePromotedContent": True, "latestControlAvailable": True, "withVoice": True},
        {},
    ),
}

# X error codes seen in this project's own logs and its roadmap notes.
CODE_STATUS = {
    32: "AUTH FAILED", 89: "AUTH FAILED", 215: "AUTH FAILED",
    326: "LOCKED", 64: "SUSPENDED", 63: "SUSPENDED",
    226: "FLAGGED", 88: "RATE LIMITED", 344: "RATE LIMITED",
}


def classify(status_code, body):
    """(verdict, detail) for one response. `body` is the parsed JSON, or None
    when the response wasn't JSON. Pure, so it can be tested without a network."""
    errors = body.get("errors") if isinstance(body, dict) else None
    first = errors[0] if errors and isinstance(errors, list) and isinstance(errors[0], dict) else {}
    code, message = first.get("code"), str(first.get("message", "")).strip()
    lowered = message.lower()

    if status_code == 429:
        return "RATE LIMITED", "HTTP 429"
    if code in CODE_STATUS:
        return CODE_STATUS[code], f"{message} (code {code})" if message else f"code {code}"
    if "automated" in lowered:
        return "FLAGGED", message
    if "suspended" in lowered:
        return "SUSPENDED", message
    if "locked" in lowered:
        return "LOCKED", message
    if status_code == 401:
        return "AUTH FAILED", message or "HTTP 401"
    if body is None:
        return "UNEXPECTED", f"HTTP {status_code}, non-JSON response (blocked before reaching the API?)"
    if errors:
        return "UNEXPECTED", f"HTTP {status_code}: {message or errors}"
    if status_code != 200:
        return "UNEXPECTED", f"HTTP {status_code}"
    if "data" not in body:
        return "UNEXPECTED", "HTTP 200 but no `data` in the response"
    return "OK", ""


def make_client(acct):
    """An httpx client configured the way the study's scraper configures its own."""
    return httpx.Client(
        headers={
            "authorization": acct["bearer_token"],
            "x-csrf-token": acct["csrf_token"],
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "x-twitter-auth-type": "OAuth2Session",
            "cookie": f"auth_token={acct['auth_token']}; ct0={acct['csrf_token']}",
            "user-agent": USER_AGENT,
            "origin": "https://x.com",
            "referer": "https://x.com/home",
        },
        timeout=30,
    )


def _get(client, ct, url, params=None):
    path = url.removeprefix("https://x.com")
    r = client.get(url, params=params, headers={
        "x-client-transaction-id": ct.generate("GET", path),
        "content-type": "application/json",
    })
    try:
        body = r.json()
    except Exception:
        body = None
    return r.status_code, body


def check_account(acct, feeds, expected_handle=None, *, bootstrap=None, client_factory=None, pause=None):
    """Returns {"verdict", "handle", "detail"} for one account. Never raises."""
    bootstrap = bootstrap or fetch_and_init
    client_factory = client_factory or make_client
    pause = pause or (lambda: time.sleep(random.uniform(1.0, 2.0)))
    result = {"verdict": "UNEXPECTED", "handle": "", "detail": ""}

    try:
        ct = bootstrap(cookies={"auth_token": acct["auth_token"], "ct0": acct["csrf_token"]})
    except RuntimeError as e:
        text = str(e)
        if "logged-out" in text or "expired" in text:
            result.update(verdict="AUTH FAILED", detail="x.com served a logged-out page: session expired, logged out, or tokens invalid")
        else:
            result.update(detail=text[:200])
        return result
    except httpx.HTTPError as e:
        result.update(verdict="NETWORK ERROR", detail=type(e).__name__)
        return result
    except Exception as e:  # the bootstrap parses x.com's HTML; any failure here is worth reporting, not crashing on
        result.update(detail=f"{type(e).__name__}: {str(e)[:160]}")
        return result

    details = []
    try:
        with client_factory(acct) as client:
            for feed in feeds:
                url, variables, features = FEEDS[feed]
                pause()
                status, body = _get(client, ct, url, {"variables": json.dumps(variables), "features": json.dumps(features)})
                verdict, detail = classify(status, body)
                if verdict != "OK":
                    result.update(verdict=verdict, detail=f"{feed}: {detail}")
                    return result
                try:
                    posts = len(parse_timeline(body, HOME_TIMELINE_PATH)[0])
                except Exception:
                    posts = "?"
                details.append(f"{feed} {posts}")

            # Best effort: which @handle is this session? Never changes the verdict
            # unless a --tracker handle is given and clearly disagrees.
            pause()
            status, body = _get(client, ct, SETTINGS_URL)
            if status == 200 and isinstance(body, dict) and body.get("screen_name"):
                result["handle"] = str(body["screen_name"])
    except httpx.HTTPError as e:
        result.update(verdict="NETWORK ERROR", detail=type(e).__name__)
        return result
    except Exception as e:
        result.update(detail=f"{type(e).__name__}: {str(e)[:160]}")
        return result

    result.update(verdict="OK", detail=", ".join(details))
    if expected_handle and result["handle"] and result["handle"].lower() != expected_handle.lstrip("@").lower():
        result.update(verdict="WRONG ACCOUNT",
                      detail=f"session is @{result['handle']}, tracker says @{expected_handle.lstrip('@')}")
    elif expected_handle and not result["handle"]:
        result["detail"] += " (couldn't confirm the @handle)"
    return result


def load_accounts(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    return doc.get("accounts") or []


def load_tracker(path):
    with open(path, newline="", encoding="utf-8") as f:
        return {r["id"].strip(): (r.get("handle") or "").strip() for r in csv.DictReader(f) if r.get("id")}


def main(argv=None, *, check=check_account):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", default="accounts.yaml", help="accounts file (default: accounts.yaml)")
    p.add_argument("--only", help="comma-separated account names to check (default: all)")
    p.add_argument("--feeds", default="home,following", help="feeds to fetch (default: home,following)")
    p.add_argument("--tracker", help="CSV with `id` and `handle` columns; flags a session whose @handle differs")
    p.add_argument("--delay", type=float, default=3.0, help="seconds to pause between accounts (default 3)")
    args = p.parse_args(argv)

    feeds = [f.strip() for f in args.feeds.split(",") if f.strip()]
    unknown = [f for f in feeds if f not in FEEDS]
    if unknown:
        sys.exit(f"error: unknown feed(s) {unknown}; choose from {list(FEEDS)}")
    if not os.path.exists(args.accounts):
        sys.exit(f"error: {args.accounts} not found")

    accounts = load_accounts(args.accounts)
    if args.only:
        wanted = [n.strip() for n in args.only.split(",") if n.strip()]
        missing = [n for n in wanted if n not in {a.get("name") for a in accounts}]
        if missing:
            sys.exit(f"error: not in {args.accounts}: {', '.join(missing)}")
        accounts = [a for a in accounts if a.get("name") in wanted]
    if not accounts:
        sys.exit(f"error: no accounts to check in {args.accounts}")
    tracker = load_tracker(args.tracker) if args.tracker else {}

    rows, bad = [], 0
    for i, acct in enumerate(accounts):
        name = str(acct.get("name"))
        gaps = [k for k in REQUIRED if not acct.get(k)]
        if gaps:
            res = {"verdict": "UNEXPECTED", "handle": "", "detail": f"entry is missing {', '.join(gaps)}"}
        else:
            if i:
                time.sleep(random.uniform(args.delay, args.delay * 2))
            res = check(acct, feeds, tracker.get(name) or None)
        rows.append((name, res))
        if res["verdict"] != "OK":
            bad += 1

    w = max(len("ACCOUNT"), *(len(n) for n, _ in rows))
    sw = max(len("STATUS"), *(len(r["verdict"]) for _, r in rows))
    print(f"{'ACCOUNT'.ljust(w)}  {'STATUS'.ljust(sw)}  HANDLE            DETAIL")
    for name, r in rows:
        handle = ("@" + r["handle"]) if r["handle"] else "-"
        print(f"{name.ljust(w)}  {r['verdict'].ljust(sw)}  {handle.ljust(16)}  {r['detail']}")
    print(f"\n{len(rows) - bad} of {len(rows)} OK.")
    if bad:
        print("Anything not OK needs a fresh cURL for that account (AUTH FAILED/LOCKED/SUSPENDED), "
              "or a wait (RATE LIMITED/FLAGGED).", file=sys.stderr)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
