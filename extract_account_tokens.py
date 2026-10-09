"""Extract X/Twitter account credentials from copy-pasted cURL commands.

For each account you're logged into, open DevTools (F12) -> Network, click any
request to x.com, and "Copy -> Copy as cURL" (bash OR cmd both work -- this
reads the values, not the shell quoting). Paste one or more of those into this
script and it pulls out the four fields config/accounts.yaml needs
(name, auth_token, bearer_token, csrf_token) and prints them as JSON.

This only ever reads a request YOU copied for YOUR OWN account -- it is a
formatter for credentials you already have in hand, not a tool that reaches
into a browser and harvests sessions.

Naming each account (first match wins):
  1. a `name: <handle>` or `# name: <handle>` line right before its cURL;
  2. the file's name, when cURLs are passed as files (`primary.txt` -> primary);
  3. `--names a,b,c`, applied to the cURLs in order;
  4. `account1`, `account2`, ... as a fallback.

Usage:
  # one file per account, named after the account
  python extract_account_tokens.py primary.txt secondary.txt > accounts.tokens.json

  # paste several into stdin (end with Ctrl-Z Enter on Windows, Ctrl-D elsewhere)
  python extract_account_tokens.py --names primary,secondary < requests.txt

  # write an accounts.yaml-ready block instead of JSON
  python extract_account_tokens.py --yaml primary.txt secondary.txt

The output contains LIVE credentials. Do not commit it or paste it anywhere
shared -- accounts.yaml and these dumps are gitignored for that reason.
"""
import argparse
import json
import os
import re
import sys

# Match the VALUES, not the shell syntax, so a cURL copied as bash, cmd, or
# PowerShell all parse the same way -- none of these tokens contain quotes,
# spaces, or the separators used below, so the surrounding quoting is irrelevant.
# `^` is in every stop-class because a "Copy as cURL (cmd)" wraps quotes and
# line breaks in carets (`^"value^"`); carets never appear inside a real token.
_AUTH_TOKEN = re.compile(r"auth_token=([^;'\"&\s\\^]+)")
_CT0_COOKIE = re.compile(r"\bct0=([^;'\"&\s\\^]+)")
_CSRF_HEADER = re.compile(r"(?i)x-csrf-token:\s*([^;'\"\s\\^]+)")
_BEARER = re.compile(r"(?i)authorization:\s*(Bearer\s+[^\s'\"\\^]+)")


def _clean(value):
    """Undo cmd-shell escaping in a captured value: cmd doubles a literal
    `%` to `%%` (only the bearer token contains `%`), and may leave a caret.
    A bash/PowerShell copy has neither, so this is a no-op there.
    """
    if value is None:
        return None
    return value.replace("%%", "%").replace("^", "")
_NAME_MARKER = re.compile(r"(?i)^#?\s*name\s*[:=]\s*(\S+)\s*$")


def split_curl_blocks(text):
    """Splits raw text into (name_or_None, block_text) per cURL command,
    honoring an optional `name:`/`# name:` line immediately before a cURL.
    """
    blocks = []
    pending_name = None
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        marker = _NAME_MARKER.match(stripped)
        if marker and not re.match(r"(?i)curl\b", stripped):
            pending_name = marker.group(1)
            continue
        if re.match(r"(?i)curl\b", stripped):
            if current is not None:
                blocks.append(current)
            current = {"name": pending_name, "text": line + "\n"}
            pending_name = None
        elif current is not None:
            current["text"] += line + "\n"
    if current is not None:
        blocks.append(current)
    return blocks


def extract_one(block_text):
    """Pulls the four credential fields out of one cURL command's text.
    Returns (fields_dict, missing_list)."""
    auth_token = _AUTH_TOKEN.search(block_text)
    bearer = _BEARER.search(block_text)
    # Prefer the explicit csrf header; fall back to the ct0 cookie, which
    # carries the same value.
    csrf = _CSRF_HEADER.search(block_text) or _CT0_COOKIE.search(block_text)

    fields = {
        "auth_token": _clean(auth_token.group(1)) if auth_token else None,
        # Stored WITH the "Bearer " prefix, since config.py feeds this
        # straight into the Authorization header (see src/scraper/twitter.py).
        "bearer_token": _clean(bearer.group(1).strip()) if bearer else None,
        "csrf_token": _clean(csrf.group(1)) if csrf else None,
    }
    missing = [k for k, v in fields.items() if not v]
    return fields, missing


def gather_blocks(args):
    """Yields (default_name, name_from_marker, text) for every cURL across
    the given files (or stdin when none are given)."""
    out = []
    if args.files:
        for path in args.files:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            stem = os.path.splitext(os.path.basename(path))[0]
            found = split_curl_blocks(text)
            if not found:
                print(f"warning: no cURL command found in {path!r}", file=sys.stderr)
            for i, block in enumerate(found):
                # One cURL per file is the norm, so the filename is the default
                # name; extra cURLs in the same file get a suffix.
                default = stem if len(found) == 1 else f"{stem}{i + 1}"
                out.append((default, block["name"], block["text"]))
    else:
        text = sys.stdin.read()
        for block in split_curl_blocks(text):
            out.append((None, block["name"], block["text"]))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", help="files each holding a copied cURL command; omit to read stdin")
    parser.add_argument("--names", help="comma-separated account names, applied to the cURLs in order")
    parser.add_argument("--yaml", action="store_true", help="emit an accounts.yaml block instead of JSON")
    parser.add_argument("--out", help="write to this file instead of stdout")
    args = parser.parse_args()

    names = [n.strip() for n in args.names.split(",")] if args.names else []
    blocks = gather_blocks(args)
    if not blocks:
        parser.error("no cURL commands found. Pass files, or paste cURL command(s) on stdin.")

    accounts = []
    seen_names = set()
    seen_auth = {}
    had_error = False

    for i, (default_name, marker_name, text) in enumerate(blocks):
        fields, missing = extract_one(text)
        # Name precedence: in-block marker, then --names, then filename, then accountN.
        name = marker_name or (names[i] if i < len(names) else None) or default_name or f"account{i + 1}"

        if missing:
            print(f"error: account {name!r} is missing {', '.join(missing)} "
                  f"-- is this a cURL to x.com from a logged-in tab?", file=sys.stderr)
            had_error = True
            continue
        if name in seen_names:
            print(f"error: duplicate account name {name!r}; give each a distinct name "
                  f"(a `name:` line, --names, or filename).", file=sys.stderr)
            had_error = True
            continue
        if fields["auth_token"] in seen_auth:
            print(f"warning: {name!r} has the same auth_token as {seen_auth[fields['auth_token']]!r} "
                  f"-- same logged-in account copied twice?", file=sys.stderr)
        seen_names.add(name)
        seen_auth[fields["auth_token"]] = name
        accounts.append({"name": name, **fields})

    if had_error:
        print(f"\n{len(accounts)} of {len(blocks)} parsed; fix the errors above and re-run.", file=sys.stderr)
    if not accounts:
        sys.exit(1)

    if args.yaml:
        lines = ["accounts:"]
        for a in accounts:
            lines += [
                f"  - name: {a['name']}",
                f"    auth_token: {a['auth_token']}",
                f"    bearer_token: \"{a['bearer_token']}\"",
                f"    csrf_token: {a['csrf_token']}",
            ]
        rendered = "\n".join(lines) + "\n"
    else:
        rendered = json.dumps(accounts, indent=2) + "\n"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rendered)
        print(f"wrote {len(accounts)} account(s) to {args.out}. This file holds LIVE "
              f"credentials -- keep it local, don't commit it.", file=sys.stderr)
    else:
        sys.stdout.write(rendered)


if __name__ == "__main__":
    main()
