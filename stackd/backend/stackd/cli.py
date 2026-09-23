"""Command line for manual refreshes and reports: `python -m stackd.cli <command>`."""

import argparse
import json
import sys

from stackd.config import get_settings
from stackd.data.nflverse import DATASETS, NflverseCache
from stackd.odds.base import BudgetError, ConfirmationRequired, TooSoonError
from stackd.odds.service import OddsService


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="stackd")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh-nflverse", help="download nflverse data (free)")
    r.add_argument("--dataset", choices=list(DATASETS))
    r.add_argument("--stale-only", action="store_true")
    sub.add_parser("status", help="nflverse cache status")

    e = sub.add_parser("events", help="games for a week, with cached odds counts (no API call)")
    e.add_argument("--week", type=int)

    sw = sub.add_parser("sgo-week", help="PAID: one SGO request for the week's games (~1 object per game)")
    sw.add_argument("--week", type=int)
    sw.add_argument("--yes", action="store_true", help="actually spend; without it you get an estimate")
    se = sub.add_parser("sgo-event", help="PAID: one SGO event (1 object)")
    se.add_argument("event_id")
    sub.add_parser("sgo-account", help="SGO /account/usage (their numbers)")

    of = sub.add_parser("op-fixtures", help="PAID: 1 OddsPapi request (+1 first time for tournament id)")
    of.add_argument("--week", type=int)
    oo = sub.add_parser("op-odds", help="PAID: 1 OddsPapi request (+1 if market catalog is stale)")
    oo.add_argument("fixture_id")
    oc = sub.add_parser("op-closing", help="PAID: 1 OddsPapi historical request (max 3 books)")
    oc.add_argument("fixture_id")
    oc.add_argument("--books", default="pinnacle,draftkings,fanduel")

    sub.add_parser("usage", help="month-to-date spend from our log")
    m = sub.add_parser("match-report", help="player-name matching results")
    m.add_argument("--all", action="store_true", help="include matched names")
    pr = sub.add_parser("props", help="print cached props (no API call)")
    pr.add_argument("--week", type=int)
    pr.add_argument("--market")
    pr.add_argument("--limit", type=int, default=40)

    args = p.parse_args(argv)
    settings = get_settings()
    nfl = NflverseCache(settings)
    svc = OddsService(settings, nfl)
    week = getattr(args, "week", None)

    try:
        if args.cmd == "refresh-nflverse":
            _print([nfl.refresh(args.dataset)] if args.dataset else nfl.refresh_stale(force=not args.stale_only))
        elif args.cmd == "status":
            _print(nfl.status())
        elif args.cmd == "events":
            _print(svc.events(week or nfl.current_week()))
        elif args.cmd == "sgo-week":
            _print(svc.refresh_sgo_week(week or nfl.current_week(), confirm=args.yes))
        elif args.cmd == "sgo-event":
            _print(svc.refresh_sgo_event(args.event_id))
        elif args.cmd == "sgo-account":
            _print(svc.sgo_account_usage())
        elif args.cmd == "op-fixtures":
            _print(svc.op_fixtures(week or nfl.current_week()))
        elif args.cmd == "op-odds":
            _print(svc.op_odds(args.fixture_id))
        elif args.cmd == "op-closing":
            _print(svc.op_closing(args.fixture_id, args.books.split(",")))
        elif args.cmd == "usage":
            _print(svc.usage_summary())
        elif args.cmd == "match-report":
            rows = svc.match_report()
            _print(rows if args.all else [r for r in rows if r["method"] in ("unmatched", "ambiguous")])
        elif args.cmd == "props":
            df = svc.odds("props", week, market=args.market)
            print(df.select(["game_id", "player_name", "market", "book", "side", "line", "price_american",
                             "gsis_id", "match_method"]).head(args.limit))
    except ConfirmationRequired as exc:
        print("Not spent. Estimated cost:")
        _print(exc.estimate)
        print("Re-run with --yes to spend it.")
        return 2
    except TooSoonError as exc:
        print(f"Skipped: {exc}")
        return 3
    except BudgetError as exc:
        print(f"Refused: {exc}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
