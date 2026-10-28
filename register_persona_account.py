"""Add one newly created persona account to accounts.yaml.

Run this right after creating an account (the spec's step 2): it takes the
account's id from signup_sheet.csv (see generate_personas.py), reads that
account's credentials from a cURL command you copied while logged into it,
and appends a ready-to-run entry -- login tokens, persona file, and the
sheet's follow_mix -- to accounts.yaml.

Credential extraction is exactly extract_account_tokens.py's (this imports
it), so the same rules apply: it only reads a request YOU copied for YOUR OWN
account, and nothing is harvested from a browser. The tokens are written to
accounts.yaml and never printed.

The entry it appends looks like (tokens elided):

  - name: p01
    auth_token: ...
    bearer_token: ...
    csrf_token: ...
    sockpuppet_config:
      persona_prompt: personas/p01.txt
      initialization_params:       # replaces the shared block wholesale, so it
        min_follows: 4             # restates every key (see the config notes)
        max_follows: 4
        min_engagements: 1
        max_initialization_days: 1
        follow_mix:
          local_news: 2
          national_dem: 2

Everything else (data_collection targets, activation schedule, the account
lists themselves) comes from the shared config -- see config.conjoint.yaml.

Usage:
  # cURL copied from the logged-in account's x.com tab, saved under curl_dumps/
  # (gitignored -- every cURL holds live credentials)
  python register_persona_account.py p01 curl_dumps/p01.txt

  # or paste it on stdin (end with Ctrl-Z Enter on Windows, Ctrl-D elsewhere)
  python register_persona_account.py p01 < curl_dumps/p01.txt

  # preview the entry (tokens masked) without touching accounts.yaml
  python register_persona_account.py p01 curl_dumps/p01.txt --dry-run

Safe to re-run: an id already in accounts.yaml is refused, never duplicated.
accounts.yaml holds LIVE credentials and is gitignored -- keep it that way.
"""
import argparse
import csv
import os
import re
import sys

try:
    from extract_account_tokens import extract_one, split_curl_blocks
except ImportError:
    sys.exit("error: extract_account_tokens.py must sit in the same directory as this script.")


def parse_follow_mix(text):
    """`local_news=2; national_dem=2` -> {"local_news": 2, "national_dem": 2}."""
    mix = {}
    for part in re.split(r"[;,]", text):
        part = part.strip()
        if not part:
            continue
        name, _, count = part.partition("=")
        if not count.strip().isdigit():
            raise ValueError(f"cannot read follow_mix entry {part!r} (expected list_name=count)")
        mix[name.strip()] = int(count)
    if not mix:
        raise ValueError("follow_mix is empty")
    return mix


def load_row(sheet_path, account_id):
    with open(sheet_path, newline="", encoding="utf-8") as f:
        rows = {r["id"]: r for r in csv.DictReader(f)}
    if account_id not in rows:
        known = ", ".join(list(rows)[:6]) + (" ..." if len(rows) > 6 else "")
        sys.exit(f"error: {account_id!r} is not in {sheet_path} (ids there: {known})")
    return rows[account_id]


def read_curl_block(path):
    text = open(path, encoding="utf-8").read() if path else sys.stdin.read()
    blocks = split_curl_blocks(text)
    if not blocks:
        sys.exit("error: no cURL command found. Copy a request to x.com as cURL from the logged-in account's tab.")
    if len(blocks) > 1:
        sys.exit(f"error: found {len(blocks)} cURL commands but this registers ONE account. "
                 f"Pass a file holding just that account's request.")
    return blocks[0]["text"]


def render_entry(account_id, creds, row, mix, min_engagements, max_init_days, mask=False):
    def tok(value):
        return "<masked>" if mask else value
    follows = sum(mix.values())
    lines = [
        f"  - name: {account_id}",
        f"    auth_token: {tok(creds['auth_token'])}",
        f"    bearer_token: {tok(creds['bearer_token'])}",
        f"    csrf_token: {tok(creds['csrf_token'])}",
        "    sockpuppet_config:",
        f"      persona_prompt: {row['persona_file']}",
        "      initialization_params:",
        f"        min_follows: {follows}",
        f"        max_follows: {follows}",
        f"        min_engagements: {min_engagements}",
        f"        max_initialization_days: {max_init_days}",
        "        follow_mix:",
    ]
    lines += [f"          {name}: {count}" for name, count in mix.items()]
    return "\n".join(lines) + "\n"


def append_entry(accounts_path, account_id, entry):
    """Appends to the `accounts:` list as text, so existing entries and
    comments are left exactly as they are (re-dumping the YAML would lose
    them). Only safe when `accounts:` is the file's last top-level key, which
    is checked first."""
    if not os.path.exists(accounts_path):
        text = "accounts:\n"
    else:
        text = open(accounts_path, encoding="utf-8").read()
        if re.search(rf"(?m)^\s*-\s*name:\s*{re.escape(account_id)}\s*$", text):
            sys.exit(f"error: {account_id!r} is already in {accounts_path}; not adding a duplicate. "
                     f"Edit that entry by hand if it needs changing.")
        top_level = re.findall(r"(?m)^([A-Za-z_][\w-]*):", text)
        if top_level != ["accounts"]:
            sys.exit(f"error: expected {accounts_path} to contain only a top-level `accounts:` list, "
                     f"found top-level keys {top_level or '[none]'}. Add this entry by hand "
                     f"(re-run with --dry-run to see it).")
        if not text.endswith("\n"):
            text += "\n"
    with open(accounts_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + entry)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("account_id", help="the account's id in the signup sheet, e.g. p01")
    p.add_argument("curl_file", nargs="?", help="file holding the account's copied cURL; omit to read stdin")
    p.add_argument("--sheet", default="signup_sheet.csv", help="signup sheet (default: signup_sheet.csv)")
    p.add_argument("--accounts", default="accounts.yaml", help="accounts file to append to (default: accounts.yaml)")
    p.add_argument("--min-engagements", type=int, default=1,
                   help="cold-start engagements required before it ends (default 1)")
    p.add_argument("--max-init-days", type=int, default=1,
                   help="cold-start gives up after this many days (default 1)")
    p.add_argument("--dry-run", action="store_true", help="show the entry with tokens masked; write nothing")
    args = p.parse_args()

    row = load_row(args.sheet, args.account_id)
    persona_path = row["persona_file"]
    if not os.path.exists(persona_path):
        sys.exit(f"error: {persona_path} is missing -- run generate_personas.py first.")
    try:
        mix = parse_follow_mix(row["follow_mix"])
    except ValueError as e:
        sys.exit(f"error: {args.sheet} row {args.account_id}: {e}")

    creds, missing = extract_one(read_curl_block(args.curl_file))
    if missing:
        sys.exit(f"error: that cURL is missing {', '.join(missing)} -- is it a request to x.com "
                 f"from the logged-in {args.account_id} tab?")

    if args.dry_run:
        sys.stdout.write(render_entry(args.account_id, creds, row, mix, args.min_engagements,
                                      args.max_init_days, mask=True))
        print(f"(dry run: nothing written to {args.accounts})", file=sys.stderr)
        return

    entry = render_entry(args.account_id, creds, row, mix, args.min_engagements, args.max_init_days)
    append_entry(args.accounts, args.account_id, entry)
    print(f"added {args.account_id} to {args.accounts}: persona {persona_path}, follow_mix "
          f"{row['follow_mix']}. The file holds LIVE credentials -- keep it local, don't commit it.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
