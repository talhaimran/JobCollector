import argparse
import json
import logging
import statistics
import sys

from . import config, db, folder, pipeline, settings
from .normalize import parse_dt


def cmd_settings(_):
    print(f"Profile: {config.PROFILE_PATH}")
    print(settings.describe(settings.load(config.PROFILE_PATH)))


def cmd_run(args):
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.LOGS_DIR / "collector.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    with db.run_lock(config.LOCK_PATH) as got:
        if not got:
            logging.warning("Another run is in progress (lock held); exiting")
            return
        if moved := folder.saved_elsewhere():  # settings loaded before a folder switch finished
            logging.warning("The data folder changed to %s while this check was starting; skipping it. "
                            "The next check uses the new folder.", moved)
            return
        pipeline.run(dry_run=args.dry_run)


def cmd_report(_):
    conn = db.connect(config.DB_PATH)
    print(f"{'run':>4}  {'started (UTC)':<20} {'mins':>5} {'new':>5} {'pass':>5} {'mail':>5} {'err':>4}  found by source")
    for r in db.last_runs(conn):
        mins = ((parse_dt(r["finished_at"]) - parse_dt(r["started_at"])).total_seconds() / 60
                if r["finished_at"] else float("nan"))
        found = json.loads(r["jobs_found"] or "{}")
        errs = sum(len(v) for v in json.loads(r["errors"] or "{}").values())
        print(f"{r['id']:>4}  {r['started_at'][:19]:<20} {mins:>5.1f} {r['new_jobs'] or 0:>5} "
              f"{r['passed_filters'] or 0:>5} {r['emailed'] or 0:>5} {errs:>4}  "
              + ", ".join(f"{k}={v}" for k, v in found.items()))

    gaps = {}
    for row in db.posted_found_pairs(conn):
        try:
            hrs = (parse_dt(row["found_at"]) - parse_dt(row["posted_at"])).total_seconds() / 3600
        except ValueError:
            continue
        date_only = len(row["posted_at"]) == 10
        gaps.setdefault(row["source"], ([], date_only))[0].append(hrs)
    print("\nMedian hours from posted to found (lower = faster source):")
    for src, (hrs, date_only) in sorted(gaps.items(), key=lambda kv: statistics.median(kv[1][0])):
        note = "  (date-only posting times: measured from midnight UTC)" if date_only else ""
        print(f"  {src:<16} {statistics.median(hrs):6.1f} h   n={len(hrs)}{note}")


def main(argv=None):
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("JobSpy", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    p = argparse.ArgumentParser(prog="collector")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch, filter, score, save, email")
    r.add_argument("--dry-run", action="store_true", help="print the email instead of sending it")
    r.set_defaults(func=cmd_run)
    sub.add_parser("settings", help="print the parsed Job Search Settings").set_defaults(func=cmd_settings)
    sub.add_parser("report", help="last 10 runs and source speed").set_defaults(func=cmd_report)
    sc = sub.add_parser("schedule", help="install/remove the every-N-hours run (launchd or Task Scheduler)")
    sc.add_argument("action", choices=["install", "uninstall", "status"])
    sc.set_defaults(func=lambda a: print(getattr(__import__("collector.schedule", fromlist=["x"]), a.action)()))
    u = sub.add_parser("ui", help="local audit + settings page on 127.0.0.1")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")
    u.set_defaults(func=lambda a: __import__("collector.webui", fromlist=["serve"]).serve(a.port, not a.no_browser))
    args = p.parse_args(argv)
    folder.migrate_legacy()
    args.func(args)
