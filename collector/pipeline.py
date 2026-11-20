"""One collector run: fetch -> dedupe -> filter -> score -> save -> email."""
import logging
import sqlite3

from . import config, db, emailer, filters, prescore, settings as settings_mod
from .dedupe import fingerprint
from .normalize import now_iso
from .sources import feeds, jobspy_src, linkedin

log = logging.getLogger(__name__)


def collect(conn, s, fetch_jobspy, fetch_feed, fetch_linkedin):
    jobs, found, errors, refreshed = [], {}, {}, {}
    if s.target_titles:
        try:
            res = fetch_linkedin(conn, s)
            jobs += res["jobs"]
            refreshed = res["refreshed"]
            if not res["skipped"]:
                found["linkedin"] = res["found"]
            if res["errors"]:
                errors["linkedin"] = res["errors"]
        except Exception as e:
            log.exception("LinkedIn failed")
            errors["linkedin"] = [f"{type(e).__name__}: {e}"]
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
    return jobs, found, errors, refreshed


def emailable(row, s) -> bool:
    """Passed, not a duplicate, and either scored at/above the cutoff or (option) only partly scored."""
    if not row["filter_passed"] or row["duplicate_of"] is not None:
        return False
    if row["score_partial"]:
        return config.SEND_PARTIAL_SCORES  # no description: keywords and dealbreakers weren't checked
    return s.email_cutoff is None or row["prescore_total"] >= s.email_cutoff


def _scored(job, s) -> dict:
    ok, reason = filters.check(job, s)
    empty = {"prescore_total": None, "prescore_keywords": None, "prescore_title": None, "matched_keywords": None}
    return {**(prescore.score(job, s) if ok else empty), "filter_passed": int(ok), "filter_reason": reason,
            "score_partial": int(ok and not job.get("description"))}


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
        row = {**j, **_scored(j, s), "fingerprint": fp, "duplicate_of": dup, "run_id": run_id}
        try:
            row["id"] = db.insert_job(conn, row)
        except sqlite3.IntegrityError:
            continue  # same job twice in one batch with a NULL id but same url etc.
        new += 1
        passed += row["filter_passed"]
        if emailable(row, s):
            to_email.append(row)
    conn.commit()
    to_email.sort(key=lambda r: r["prescore_total"], reverse=True)
    return new, passed, to_email


def refresh(conn, s, refreshed: dict) -> list[dict]:
    """Re-filter and re-score saved jobs whose description arrived in a later LinkedIn sweep.
    Returns the ones to email now."""
    out = []
    for job_id, fields in refreshed.items():
        if not fields:
            continue
        row = {**dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()), **fields}
        row.update(_scored(row, s))
        db.update_job(conn, job_id, {k: row[k] for k in (*fields, "filter_passed", "filter_reason", "score_partial",
                                                           "prescore_total", "prescore_keywords", "prescore_title",
                                                           "matched_keywords")})
        if row["emailed_at"] is None and emailable(row, s):
            out.append(row)
    conn.commit()
    return out


def run(dry_run=False, s=None, conn=None, fetch_jobspy=jobspy_src.fetch, fetch_feed=feeds.fetch,
        send=emailer.send, out=print, fetch_linkedin=None) -> dict:
    fetch_linkedin = fetch_linkedin or linkedin.sweep
    s = s or settings_mod.load(config.PROFILE_PATH)
    log.info("Job Search Settings:\n%s", settings_mod.describe(s))
    conn = conn or db.connect(config.DB_PATH)
    filters_off = s.filters_off()
    run_id = db.start_run(conn, now_iso(), filters_off)
    log.info("Run %d started%s", run_id, " (dry run)" if dry_run else "")

    jobs, found, errors, refreshed = collect(conn, s, fetch_jobspy, fetch_feed, fetch_linkedin)
    new, passed, to_email = process(conn, s, jobs, run_id)
    to_email = sorted(to_email + refresh(conn, s, refreshed), key=lambda r: r["prescore_total"], reverse=True)
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
