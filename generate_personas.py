"""Generate the persona-conjoint study's persona files and signup sheet.

One run writes BOTH outputs, so the sheet and the persona text can never
drift apart (the sheet's age / gender / party / engage-rule columns are the
same values the persona files were rendered from):

  personas/p01.txt ... p36.txt   one persona prompt per account
  signup_sheet.csv               one row per account: what to enter at signup
  signup_sheet.meta.json         the seed + settings that produced them

Design (see the "Persona conjoint on X" spec): a full factorial of
  age band  (13-18, 18-29, 30+)  x  gender (male, female)
  x  party  (Democrat, Neither, Republican)            = 18 cells,
with k accounts per cell (default k=2 -> 36 accounts). Every account follows
the Maryland local-news list plus ONE national list chosen by its party, and
is told to engage with the n-th post it sees each time the feed is loaded.

Persona text (the spec's fixed template, three slots):
  You are 15-years old, female Democrat, living in Maryland. Engage with
  2nd post you see every time the feed is loaded.
For the no-partisan-marker level ("Neither") the party word is left out.

Everything is drawn from --seed, so the same command always reproduces the
same 36 personas. Different aspects use separate random streams, so changing
--engage-max, say, does not reshuffle ages or names.

Account ORDER is blocked-randomized: ids are dealt out k blocks of 18, each
block containing every cell exactly once in random order. Create accounts in
the sheet's order and the first 18 already cover every cell once -- if
signups stall or accounts get lost partway through, what you have is still a
complete (k=1) conjoint rather than a lopsided one.

Age bands: the spec labels them 13-18 / 18-29 / 30+, which overlap at 18 and
leave 30+ open-ended. Integers are drawn from 13-17 / 18-29 / 30-65 instead
(edit AGE_BANDS below to change this). Each birthdate is chosen so the age
printed in the persona is correct BOTH on signup day and through the end of
the data-collection window, so no account has a birthday mid-study.

Usage:
  python generate_personas.py                      # write everything, seed 42
  python generate_personas.py --seed 7 --dry-run   # preview the sheet, write nothing
  python generate_personas.py --k 1                # 18 accounts, one per cell
  python generate_personas.py --force              # overwrite differing files

Next step per account: create it by hand using its sheet row (birthdate and
display name are fixed at signup; set location to Maryland, leave the bio
empty), then run register_persona_account.py to add its accounts.yaml entry.

This script only writes local text files. It makes no network calls and has
nothing to do with creating or verifying accounts.
"""
import argparse
import csv
import datetime
import json
import os
import random
import sys

# (label as written in the spec, lowest age drawn, highest age drawn)
AGE_BANDS = [
    ("13-18", 13, 17),
    ("18-29", 18, 29),
    ("30+", 30, 65),
]
GENDERS = ["male", "female"]
PARTIES = ["Democrat", "Neither", "Republican"]

LOCAL_LIST = "local_news"
# The national list each party level follows, alongside the local list.
PARTY_LISTS = {
    "Democrat": "national_dem",
    "Neither": "national_non",
    "Republican": "national_rep",
}
FOLLOWS_PER_LIST = 2  # L1+L2, and the party's two national sources

# The spec's template. Kept verbatim -- including "15-years old" -- so the
# text matches what the researchers wrote; edit here to change it everywhere.
PERSONA_TEMPLATE = (
    "You are {age}-years old, {gender}{party}, living in Maryland. "
    "Engage with {nth} post you see every time the feed is loaded."
)

# First names by (age band, gender): drawn without replacement within a
# (band, gender) group, so no two accounts in a group share one.
FIRST_NAMES = {
    ("13-18", "female"): ["Olivia", "Emma", "Ava", "Sophia", "Isabella", "Mia", "Harper",
                          "Abigail", "Emily", "Ella", "Chloe", "Lily", "Grace", "Zoe", "Nora"],
    ("13-18", "male"): ["Liam", "Noah", "Mason", "Ethan", "Logan", "Lucas", "Jackson",
                        "Aiden", "Caleb", "Owen", "Elijah", "Carter", "Wyatt", "Gavin", "Dylan"],
    ("18-29", "female"): ["Madison", "Hannah", "Taylor", "Alexis", "Brianna", "Kayla", "Jasmine",
                          "Samantha", "Rachel", "Megan", "Jessica", "Lauren", "Ashley", "Natalie", "Morgan"],
    ("18-29", "male"): ["Tyler", "Jordan", "Austin", "Brandon", "Justin", "Cody", "Nathan",
                        "Zachary", "Kevin", "Jacob", "Hunter", "Marcus", "Trevor", "Devin", "Connor"],
    ("30+", "female"): ["Jennifer", "Michelle", "Karen", "Lisa", "Angela", "Melissa", "Stephanie",
                        "Heather", "Amy", "Nicole", "Tiffany", "Laura", "Denise", "Christine", "Alice"],
    ("30+", "male"): ["Michael", "David", "Jason", "Brian", "Jack", "Robert", "Eric",
                      "Scott", "Gregory", "Mark", "Daniel", "Steven", "Timothy", "Andrew", "Gary"],
}
SURNAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis", "Wilson", "Moore",
    "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin", "Thompson", "Garcia",
    "Martinez", "Robinson", "Clark", "Lewis", "Walker", "Hall", "Young", "Allen", "King", "Wright",
    "Scott", "Green", "Baker", "Adams", "Nelson", "Hill", "Campbell", "Mitchell", "Roberts",
    "Carter", "Phillips", "Evans", "Turner", "Parker", "Collins", "Edwards", "Stewart", "Morris",
    "Murphy", "Cook", "Rogers", "Morgan",
]

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]

SHEET_COLUMNS = [
    "id", "age_range", "age", "birthdate", "birth_month", "birth_day", "birth_year",
    "gender", "display_name", "party", "follow_mix", "engage_rule", "persona_file",
]


def sub_rng(seed, *parts):
    """An independent, reproducible random stream per purpose (and per
    account), so unrelated choices never share a draw sequence."""
    return random.Random("|".join([str(seed)] + [str(p) for p in parts]))


def ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def age_on(birth, on):
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


def pick_birthdate(rng, age, signup, window_end):
    """A birthdate making `age` correct on BOTH signup day and window_end.

    Candidate birthdays that straddle either date are skipped, so the age in
    the persona text holds for the whole study. Feb 29 is avoided (it would
    make "the same birthday next year" ambiguous).
    """
    for _ in range(2000):
        month = rng.randint(1, 12)
        day = rng.randint(1, 28 if month == 2 else [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
        year = window_end.year - age - rng.randint(0, 1)
        birth = datetime.date(year, month, day)
        if age_on(birth, signup) == age and age_on(birth, window_end) == age:
            return birth
    raise RuntimeError(f"no birthdate keeps age {age} constant from {signup} to {window_end}")


def deal_cells(seed, k):
    """k blocks, each holding every one of the 18 cells once, shuffled
    within the block. Returns [(age_band, gender, party), ...]."""
    cells = [(band, g, p) for band in AGE_BANDS for g in GENDERS for p in PARTIES]
    order = []
    for block in range(k):
        block_cells = list(cells)
        sub_rng(seed, "cell-order", block).shuffle(block_cells)
        order.extend(block_cells)
    return order


def balanced_engage_rules(seed, n, lo, hi):
    """n values in [lo, hi], each value used (nearly) equally often, in
    random order. 'Randomly assigned' per the spec, but with no chance of
    one value landing mostly on one party/age cell by luck."""
    span = list(range(lo, hi + 1))
    pool = (span * (n // len(span) + 1))[:n]
    sub_rng(seed, "engage").shuffle(pool)
    return pool


def build_accounts(seed, k, engage_min, engage_max, signup, window_end):
    cells = deal_cells(seed, k)
    n = len(cells)
    width = max(2, len(str(n)))
    engage = balanced_engage_rules(seed, n, engage_min, engage_max)

    # Names: drawn per (band, gender) group without replacement.
    name_queues = {}
    for (label, _lo, _hi) in AGE_BANDS:
        for g in GENDERS:
            names = list(FIRST_NAMES[(label, g)])
            sub_rng(seed, "names", label, g).shuffle(names)
            name_queues[(label, g)] = names

    used_full_names = set()
    accounts = []
    for i, ((label, lo, hi), gender, party) in enumerate(cells, start=1):
        acct_id = f"p{i:0{width}d}"
        age = sub_rng(seed, acct_id, "age").randint(lo, hi)
        birth = pick_birthdate(sub_rng(seed, acct_id, "birthdate"), age, signup, window_end)

        queue = name_queues[(label, gender)]
        if not queue:
            raise RuntimeError(f"ran out of first names for {label}/{gender}; add more to FIRST_NAMES")
        first = queue.pop()
        surname_rng = sub_rng(seed, acct_id, "surname")
        full = f"{first} {surname_rng.choice(SURNAMES)}"
        while full in used_full_names:
            full = f"{first} {surname_rng.choice(SURNAMES)}"
        used_full_names.add(full)

        mix = {LOCAL_LIST: FOLLOWS_PER_LIST, PARTY_LISTS[party]: FOLLOWS_PER_LIST}
        accounts.append({
            "id": acct_id,
            "age_range": label,
            "age": age,
            "birthdate": birth.isoformat(),
            "birth_month": MONTHS[birth.month - 1],
            "birth_day": birth.day,
            "birth_year": birth.year,
            "gender": gender,
            "display_name": full,
            "party": party,
            "follow_mix": "; ".join(f"{name}={count}" for name, count in mix.items()),
            "engage_rule": engage[i - 1],
            "persona_file": f"personas/{acct_id}.txt",
        })
    return accounts


def render_persona(account):
    party = "" if account["party"] == "Neither" else f" {account['party']}"
    return PERSONA_TEMPLATE.format(
        age=account["age"], gender=account["gender"], party=party,
        nth=ordinal(account["engage_rule"]),
    ) + "\n"


def check_design(accounts, k):
    """Fail loudly if the generated design isn't the balanced factorial it
    claims to be -- a silently lopsided design invalidates the conjoint."""
    from collections import Counter
    counts = Counter((a["age_range"], a["gender"], a["party"]) for a in accounts)
    expected = len(AGE_BANDS) * len(GENDERS) * len(PARTIES)
    bad = {cell: c for cell, c in counts.items() if c != k}
    if len(counts) != expected or bad:
        raise RuntimeError(f"design is not balanced at k={k}: {len(counts)}/{expected} cells, off-count: {bad}")
    if len({a["id"] for a in accounts}) != len(accounts):
        raise RuntimeError("duplicate account ids")


def print_table(accounts, file=sys.stdout):
    cols = ["id", "age_range", "age", "birthdate", "gender", "display_name", "party", "follow_mix", "engage_rule"]
    widths = {c: max(len(c), *(len(str(a[c])) for a in accounts)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols), file=file)
    for a in accounts:
        print("  ".join(str(a[c]).ljust(widths[c]) for c in cols), file=file)


def write_outputs(accounts, args, meta):
    os.makedirs(args.personas_dir, exist_ok=True)
    planned = [(os.path.join(args.personas_dir, f"{a['id']}.txt"), render_persona(a)) for a in accounts]

    # Refuse to clobber differing files unless asked: personas are the study's
    # fixed treatment, and silently changing one after accounts exist would
    # break the match between the sheet, the accounts, and the data.
    differing = [path for path, text in planned
                 if os.path.exists(path) and open(path, encoding="utf-8").read() != text]
    if os.path.exists(args.sheet):
        new_rows = [{c: str(a[c]) for c in SHEET_COLUMNS} for a in accounts]
        with open(args.sheet, newline="", encoding="utf-8") as f:
            if list(csv.DictReader(f)) != new_rows:
                differing.append(args.sheet)
    if differing and not args.force:
        shown = ", ".join(differing[:4]) + (f", ... ({len(differing)} total)" if len(differing) > 4 else "")
        sys.exit(f"error: would overwrite files that differ from what this run generates: {shown}\n"
                 f"The same --seed, --k and dates reproduce them exactly (birthdates depend on "
                 f"--signup-date/--window-end, so pin those; signup_sheet.meta.json records the "
                 f"command). If you changed settings on purpose, re-run with --force.")

    for path, text in planned:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    with open(args.sheet, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        writer.writeheader()
        writer.writerows(accounts)
    meta_path = os.path.splitext(args.sheet)[0] + ".meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")
    return meta_path


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, default=42, help="makes every draw reproducible (default 42)")
    p.add_argument("--k", type=int, default=2, help="accounts per cell (default 2 -> 36 accounts)")
    p.add_argument("--engage-min", type=int, default=1, help="lowest n for 'engage with the n-th post' (default 1)")
    p.add_argument("--engage-max", type=int, default=5, help="highest n (default 5)")
    p.add_argument("--signup-date", default=None,
                   help="YYYY-MM-DD accounts are created by (default: today). Ages are correct from this date on.")
    p.add_argument("--window-end", default="2026-11-06",
                   help="YYYY-MM-DD the data-collection window ends (default 2026-11-06: Election Day + 3 days). "
                        "Ages stay correct through this date.")
    p.add_argument("--personas-dir", default="personas", help="where pNN.txt files go (default: personas)")
    p.add_argument("--sheet", default="signup_sheet.csv", help="signup sheet path (default: signup_sheet.csv)")
    p.add_argument("--force", action="store_true", help="overwrite existing files that differ")
    p.add_argument("--dry-run", action="store_true", help="print the sheet and write nothing")
    return p.parse_args()


def main():
    args = parse_args()
    if args.k < 1:
        sys.exit("error: --k must be at least 1")
    if args.engage_min < 1 or args.engage_max < args.engage_min:
        sys.exit("error: need 1 <= --engage-min <= --engage-max")
    try:
        signup = datetime.date.fromisoformat(args.signup_date) if args.signup_date else datetime.date.today()
        window_end = datetime.date.fromisoformat(args.window_end)
    except ValueError as e:
        sys.exit(f"error: dates must be YYYY-MM-DD ({e})")
    if window_end < signup:
        sys.exit("error: --window-end is before the signup date")

    accounts = build_accounts(args.seed, args.k, args.engage_min, args.engage_max, signup, window_end)
    check_design(accounts, args.k)

    youngest = min(a["age"] for a in accounts)
    if youngest < 13:
        sys.exit(f"error: generated an age under 13 ({youngest}); X requires users to be 13+")

    meta = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "seed": args.seed, "k": args.k, "accounts": len(accounts),
        "engage_range": [args.engage_min, args.engage_max],
        "signup_date": signup.isoformat(), "window_end": window_end.isoformat(),
        "age_bands": [{"label": l, "drawn_from": [lo, hi]} for l, lo, hi in AGE_BANDS],
        "persona_template": PERSONA_TEMPLATE,
        "reproduce_with": (f"python generate_personas.py --seed {args.seed} --k {args.k} "
                           f"--engage-min {args.engage_min} --engage-max {args.engage_max} "
                           f"--signup-date {signup.isoformat()} --window-end {window_end.isoformat()}"),
    }

    if args.dry_run:
        print_table(accounts)
        print(f"\n(dry run: {len(accounts)} accounts, nothing written. First persona:)", file=sys.stderr)
        print(render_persona(accounts[0]).strip(), file=sys.stderr)
        return

    meta_path = write_outputs(accounts, args, meta)
    print(f"wrote {len(accounts)} personas to {args.personas_dir}/, the signup sheet to {args.sheet}, "
          f"and {meta_path}.", file=sys.stderr)
    print(f"design: {args.k} per cell across 18 cells, seed {args.seed}. Create accounts in sheet order "
          f"(location Maryland, empty bio), then register each with register_persona_account.py.",
          file=sys.stderr)
    print(f"reproduce: {meta['reproduce_with']}", file=sys.stderr)


if __name__ == "__main__":
    main()
