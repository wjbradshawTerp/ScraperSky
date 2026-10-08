"""Build untrustworthy_sources.json (the intervention's target list) from a CSV.

Keeps the first N_TOTAL rows of the CSV and writes them, in order, as an
account list. The order matters: with `selection_rule: stratified70` the
orchestrator splits this list into consecutive blocks of 10 and mutes 70%
of every block, so the file's order is the stratification key.

Usage:
    python build_untrustworthy_sources.py untrustworthy.csv
    python build_untrustworthy_sources.py untrustworthy.csv --order engagement
"""
import argparse
import csv
import json

N_TOTAL = 434  # Cutoff: first 434 accounts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path")
    parser.add_argument("--out", default="untrustworthy_sources.json")
    parser.add_argument("--n", type=int, default=N_TOTAL)
    parser.add_argument(
        "--order", choices=["original", "engagement"], default="original",
        help="original: keep the CSV's order within the cutoff (default). "
             "engagement: re-sort the kept rows by total_engagement, followed_by, "
             "exposure, followers (all descending) before writing.",
    )
    args = parser.parse_args()

    with open(args.csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Keep the first N rows of the original CSV
    reduced = rows[:args.n]

    if args.order == "engagement":
        keys = ["total_engagement", "followed_by", "exposure", "followers"]
        reduced = sorted(reduced, key=lambda r: tuple(int(r[k]) for k in keys), reverse=True)

    users = [
        {
            "user_id": r["target_user_id"],
            "_comment": r.get("name_with_handle", ""),
            "followed_by": int(r["followed_by"]),
            "total_engagement": int(r["total_engagement"]),
        }
        for r in reduced
    ]
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"_comment": f"First {len(users)} rows of {args.csv_path}, order={args.order}",
                   "users": users}, f, indent=4, ensure_ascii=False)
    print(f"Wrote {len(users)} accounts to {args.out} (order={args.order})")


if __name__ == "__main__":
    main()
