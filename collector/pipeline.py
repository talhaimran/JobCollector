"""One collector run: fetch -> dedupe -> filter -> score -> save -> email."""
import logging
import sqlite3

from . import config, db, emailer, filters, prescore, settings as settings_mod
from .dedupe import fingerprint
from .normalize import now_iso
from .sources import feeds, jobspy_src

log = logging.getLogger(__name__)


def collect(s, fetch_jobspy, fetch_feed):
    jobs, found, errors = [], {}, {}
    if s.target_titles:
        try:
            js, f, e = fetch_jobspy(s)
            jobs += js
            found.update(f)
            errors.update(e)
        except Exception as e:
            log.exception("JobSpy failed")
            errors["jobspy"] = [f"{type(e).__name__}: {e}"]
    for c in s.target_companies:
        key = f"{c.system}:{c.board}"
        try:
            got = fetch_feed(c)
            jobs += got
            found[key] = len(got)
            log.info("%s -> %d recent", key, len(got))
        except Exception as e:
            log.warning("%s failed: %s", key, e)
            errors[key] = [f"{key}: {type(e).__name__}: {str(e)[:200]}"]
    return jobs, found, errors


def process(conn, s, jobs, run_id):
    """Save every unseen job. Returns (new_count, passed_count, rows_to_email)."""
    new = passed = 0
    to_email = []
    for j in jobs:
        if db.seen(conn, j["source"], j["source_job_id"], j["url"]):
            continue
        fp = fingerprint(j["company"], j["title"], j["location"])
        # duplicate if any earlier row (any source, incl. same-source reposts) has this fingerprint
        dup = db.fingerprint_owner(conn, fp)
        ok, reason = filters.check(j, s)
        row = {**j, **(prescore.score(j, s) if ok else {}), "fingerprint": fp, "duplicate_of": dup,
               "filter_passed": int(ok), "filter_reason": reason, "run_id": run_id}
        try:
            row["id"] = db.insert_job(conn, row)
        except sqlite3.IntegrityError:
            continue  # same job twice in one batch with a NULL id but same url etc.
        new += 1
        passed += ok
        if ok and dup is None and (s.email_cutoff is None or row["prescore_total"] >= s.email_cutoff):
            to_email.append(row)
    conn.commit()
    to_email.sort(key=lambda r: r["prescore_total"], reverse=True)
    return new, passed, to_email


def run(dry_run=False, s=None, conn=None, fetch_jobspy=jobspy_src.fetch, fetch_feed=feeds.fetch,
        send=emailer.send, out=print) -> dict:
    s = s or settings_mod.load(config.PROFILE_PATH)
    log.info("Job Search Settings:\n%s", settings_mod.describe(s))
    conn = conn or db.connect(config.DB_PATH)
    filters_off = s.filters_off()
    run_id = db.start_run(conn, now_iso(), filters_off)
    log.info("Run %d started%s", run_id, " (dry run)" if dry_run else "")

    jobs, found, errors = collect(s, fetch_jobspy, fetch_feed)
    new, passed, to_email = process(conn, s, jobs, run_id)
    stats = {"found": len(jobs), "new": new, "passed": passed}

    emailed = 0
    if to_email or config.SEND_WHEN_EMPTY:
        subject, html_body, text_body = emailer.build(to_email, stats, filters_off, errors)
        if dry_run:
            out(text_body)
        else:
            try:
                send(subject, html_body, text_body)
                emailed = len(to_email)
                db.mark_emailed(conn, [r["id"] for r in to_email], now_iso())
            except Exception as e:
                log.error("Email failed: %s", e)
                errors["email"] = [f"{type(e).__name__}: {e}"]
    else:
        log.info("No new matches; no email sent")

    db.finish_run(conn, run_id, finished_at=now_iso(), jobs_found=found, new_jobs=new,
                  passed_filters=passed, emailed=emailed, errors=errors)
    summary = {"run_id": run_id, **stats, "to_email": len(to_email), "emailed": emailed,
               "found_by_source": found, "errors": errors}
    log.info("Run %d done: %s", run_id, {k: v for k, v in summary.items() if k != "errors"})
    if errors:
        log.warning("Source errors: %s", sum(len(v) for v in errors.values()))
    return summary
