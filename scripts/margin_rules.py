"""Manage per-company and per-product margin overrides.

    python scripts/margin_rules.py list
    python scripts/margin_rules.py set "ROHAN FAB SRT" 18               # whole company
    python scripts/margin_rules.py set "ROHAN FAB SRT" "MILK CAKE" 20   # one design
    python scripts/margin_rules.py remove "ROHAN FAB SRT" "MILK CAKE"
    python scripts/margin_rules.py import rules.csv                     # company,product,margin_pct

Without a rule, a line gets the 15/17% tier by bill rate (app/services/pricing.py).
The most specific rule wins: company + product, then company alone. Names
match ignoring case and extra spaces.

A rule affects scans from now on. Prices already saved in the book are never
rewritten — the same promise the tier makes.

Uses DATABASE_URL from .env, like the service.
"""

import argparse
import csv
import sys
from pathlib import Path

# Run as `python scripts/margin_rules.py`, so Python puts scripts/ on the
# path rather than the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402


def cmd_list(_: argparse.Namespace) -> None:
    rules = db.margin_rules()
    if not rules:
        print("No margin rules — every line uses the tier.")
        return
    for (company, product), pct in sorted(rules.items()):
        print(f"{pct:>6g}%  {company}  /  {product or '(every product)'}")


def cmd_set(args: argparse.Namespace) -> None:
    # `set COMPANY PCT` or `set COMPANY PRODUCT PCT`.
    if len(args.values) == 1:
        product, pct = None, args.values[0]
    elif len(args.values) == 2:
        product, pct = args.values
    else:
        sys.exit("usage: set COMPANY [PRODUCT] MARGIN_PCT")
    db.set_margin_rule(args.company, product, _pct(pct))
    print(f"set: {args.company} / {product or '(every product)'} = {_pct(pct):g}%")


def cmd_remove(args: argparse.Namespace) -> None:
    if db.remove_margin_rule(args.company, args.product):
        print(f"removed: {args.company} / {args.product or '(every product)'}")
    else:
        sys.exit(f"no rule for {args.company} / {args.product or '(every product)'}")


def cmd_import(args: argparse.Namespace) -> None:
    """Load rules from a CSV with columns company,product,margin_pct.

    An empty product means the whole company. Rows are upserts, so running
    the same file twice changes nothing.
    """
    with open(args.csv, newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    for number, row in enumerate(rows, start=2):  # line 1 is the header
        if not (row.get("company") or "").strip():
            sys.exit(f"{args.csv}:{number}: company is blank")
        db.set_margin_rule(row["company"], row.get("product"), _pct(row.get("margin_pct", "")))
    print(f"imported {len(rows)} rule(s) from {args.csv}")


def _pct(value: str) -> float:
    try:
        pct = float(value)
    except ValueError:
        sys.exit(f"not a percentage: {value!r}")
    if not 0 <= pct < 1000:
        sys.exit(f"margin out of range: {pct:g}")
    return pct


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list").set_defaults(run=cmd_list)

    set_parser = sub.add_parser("set")
    set_parser.add_argument("company")
    set_parser.add_argument("values", nargs="+", metavar="[PRODUCT] MARGIN_PCT")
    set_parser.set_defaults(run=cmd_set)

    remove_parser = sub.add_parser("remove")
    remove_parser.add_argument("company")
    remove_parser.add_argument("product", nargs="?")
    remove_parser.set_defaults(run=cmd_remove)

    import_parser = sub.add_parser("import")
    import_parser.add_argument("csv")
    import_parser.set_defaults(run=cmd_import)

    args = parser.parse_args()
    # Idempotent, and makes sure margin_rule exists even when the service
    # runs with DB_AUTO_INIT=false and hasn't created it.
    db.init_db()
    try:
        args.run(args)
    finally:
        db.close_pool()


if __name__ == "__main__":
    main()
