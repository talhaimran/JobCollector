"""Robert Half Canada's public job search (roberthalf.com/ca/en), signed out.

/ca/en/jobs/all/<title> is a server-side keyword search across Canada, newest first, 25 per page
(?pagenumber=2 ...). The page embeds its results as a JSON string (aemSettings.rh_job_search.initialResults)
with everything we need: full description, remote/hybrid/on-site, pay and posting date, so no job pages.
Each run searches once per title and stops paging at the first job dated before the cutoff day.
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

from .. import config, db, normalize
from . import PoliteClient
from .jobspy_src import search_titles

log = logging.getLogger(__name__)
BASE = "https://www.roberthalf.com/ca/en/jobs/all/"
PER_PAGE = 25
MARK = "initialResults = JSON.parse('"
_ESC = re.compile(r"\\(x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|.)", re.S)


def js_string(raw: str) -> str:
    """Undo a single-quoted JavaScript string literal: \\x22, \\u002D, \\/, \\\\ ..."""
    def one(m):
        e = m.group(1)
        if len(e) > 1:
            return chr(int(e[1:], 16))
        return {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(e, e)
    # \uD83D\uDE00 (emoji) decode to two halves: join them; a stray half becomes U+FFFD so SQLite can store it
    return _ESC.sub(one, raw).encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def parse_page(html: str) -> tuple[list[dict], int]:
    """(raw jobs, total found) from a search page."""
    i = html.find(MARK)
    if i < 0:
        raise ValueError("no initialResults in page (layout changed?)")
    i += len(MARK)
    data = json.loads(js_string(html[i:html.index("');", i)]))["data"]
    return data.get("jobs") or [], int(data.get("found") or 0)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def to_job(r: dict) -> dict:
    loc = ", ".join(x for x in (r.get("city"), r.get("stateprovince"), "Canada") if x)
    lo, hi = r.get("payrate_min"), r.get("payrate_max")
    return normalize._job(
        source="roberthalf", source_job_id=f"rh-{r['unique_job_number']}", url=r.get("job_detail_url"),
        title=r.get("jobtitle"), company="Robert Half (client not named)", location=loc,
        description=normalize.html_to_text(r.get("description")),
        work_model=normalize.detect_work_model(r.get("jobtitle"), loc, hint=r.get("remote")),
        employment_type=normalize.norm_employment((r.get("emptype") or "").replace(" to ", ", ")),  # "Contract to Permanent"
        salary_min=float(lo) if lo else None, salary_max=float(hi) if hi else None,
        salary_currency=(r.get("salary_currency") or None) if (lo or hi) else None,
        salary_interval=normalize.norm_interval(r.get("payrate_period")) if (lo or hi) else None,
        posted_at=(r.get("date_posted") or "")[:10] or None)


def fetch(conn, s, get=None, sleep=time.sleep, now=None) -> list[dict]:
    """New recent jobs for every search title. Raises on HTTP errors (the pipeline records them)."""
    now = now or datetime.now(timezone.utc)
    # whole days: postings carry a date, not a time, so a job from early yesterday still counts (known jobs are skipped)
    since = (now - timedelta(hours=config.HOURS_OLD)).date().isoformat()
    client, jobs, known = PoliteClient(config.RANDSTAD_DELAY_SECONDS, get, sleep), [], set()
    for title in search_titles(s.target_titles):
        for page in range(config.ROBERTHALF_MAX_PAGES):
            raw, total = parse_page(client.get(BASE + slug(title) + (f"?pagenumber={page + 1}" if page else "")).text)
            old = False
            for r in raw:
                try:
                    j = to_job(r)
                except (KeyError, ValueError, TypeError) as e:  # one malformed posting mustn't lose the rest
                    log.warning("roberthalf: skipped a malformed job (%s: %s)", type(e).__name__, e)
                    continue
                if j["posted_at"] and j["posted_at"] < since:
                    old = True  # newest first: the rest of the pages are older still
                    continue  # an undated job is kept and proves nothing about the rest
                if j["source_job_id"] in known or db.seen(conn, j["source"], j["source_job_id"], j["url"]):
                    continue
                known.add(j["source_job_id"])
                jobs.append(j)
            if old or not raw or (page + 1) * PER_PAGE >= total:
                break
    log.info("roberthalf: %d requests, %d new jobs", client.used, len(jobs))
    return jobs
