"""LinkedIn's public (signed-out) job search, with a request budget.

One sweep at most every LINKEDIN_MIN_HOURS. Each sweep:
1. fetches job pages for jobs saved without a description last time (oldest first),
2. runs the searches whose last success is oldest, newest jobs first, stopping at the first known job,
3. fetches job pages only for new jobs that pass the filters a description can't change.
Every request counts against LINKEDIN_REQUEST_BUDGET. The first 429 ends the sweep; the next one comes after
the usual LINKEDIN_MIN_HOURS (a blocked sweep costs only a request or two, so there's no back-off).
"""
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from .. import config, db, filters, normalize
from .jobspy_src import search_titles

log = logging.getLogger(__name__)
BASE = "https://www.linkedin.com"
SEARCH = f"{BASE}/jobs-guest/jobs/api/seeMoreJobPostings/search"
PER_PAGE = 10
HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Safari/537.36",
}
CURRENCIES = {"$": "USD", "CA$": "CAD", "£": "GBP", "€": "EUR"}
INTERVALS = {"/hr": "hourly", "/mo": "monthly", "/yr": "yearly"}


class Blocked(Exception):
    pass


class OutOfBudget(Exception):
    pass


class Client:
    def __init__(self, budget, get=None, sleep=time.sleep):
        self.left, self.used = budget, 0
        self._get = get or requests.Session().get
        self._sleep = sleep

    def get(self, url, params=None):
        if self.left <= 0:
            raise OutOfBudget
        if self.used:
            self._sleep(random.uniform(*config.LINKEDIN_DELAY_SECONDS))
        self.left -= 1
        self.used += 1
        r = self._get(url, params=params, headers=HEADERS, timeout=config.HTTP_TIMEOUT)
        if r.status_code in (429, 999):  # 999 is LinkedIn's other "too many requests"
            raise Blocked(f"status {r.status_code}")
        r.raise_for_status()
        return r


# --- parsing ------------------------------------------------------------------
def parse_cards(html: str, remote: bool) -> list[dict]:
    jobs = []
    for card in BeautifulSoup(html, "html.parser").select(".base-search-card"):
        job_id = str(card.get("data-entity-urn", "")).rsplit(":", 1)[-1]
        title, company = card.select_one(".base-search-card__title"), card.select_one(".base-search-card__subtitle")
        if not job_id.isdigit() or title is None:
            continue
        loc, when = card.select_one(".job-search-card__location"), card.find("time")
        title, loc = title.get_text(strip=True), loc.get_text(strip=True) if loc else None
        jobs.append(normalize._job(
            source="linkedin", source_job_id=f"li-{job_id}", url=f"{BASE}/jobs/view/{job_id}",
            title=title, company=company.get_text(strip=True) if company else None, location=loc,
            work_model=normalize.detect_work_model(title, loc, is_remote=remote),
            posted_at=when.get("datetime") if when else None))
    return jobs


def _criteria(soup, heading):
    for h in soup.select(".description__job-criteria-subheader"):
        if heading.lower() in h.get_text(strip=True).lower():
            span = h.find_next_sibling(class_="description__job-criteria-text")
            return span.get_text(strip=True) if span else None
    return None


def parse_salary(text: str | None) -> dict:
    """'CA$80,000.00/yr - CA$100,000.00/yr' -> min, max, currency, interval."""
    m = re.fullmatch(r"([^\d\s]+) ?([\d,]+(?:\.\d\d)?)(/\w+) ?- ?[^\d\s]+ ?([\d,]+(?:\.\d\d)?)/\w+", " ".join((text or "").split()))
    if not m:
        return {}
    cur, lo, interval, hi = m.groups()
    return {"salary_min": float(lo.replace(",", "")), "salary_max": float(hi.replace(",", "")),
            "salary_currency": CURRENCIES.get(cur, cur), "salary_interval": INTERVALS.get(interval)}


def parse_detail(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(".show-more-less-html__markup")
    desc = normalize.html_to_text(body.decode_contents()) if body else None
    salary = soup.select_one(".compensation__salary")
    return {"description": desc, "employment_type": normalize.norm_employment(_criteria(soup, "Employment type")),
            **parse_salary(salary.get_text() if salary else None)}


# --- queries and state ----------------------------------------------------------
def build_queries(s) -> list[dict]:
    qs = [{"title": t, "location": l, "remote": False, "distance": s.radius()}
          for t in search_titles(s.target_titles) for l in s.search_where()]
    qs += [{"title": t, "location": c, "remote": True} for t in search_titles(s.target_titles) for c in s.remote_in]
    for q in qs:
        q["key"] = f"{q['title']}|{q['location']}|{'remote' if q['remote'] else 'local'}"
    return qs


def _params(q, since_seconds, start):
    p = {"keywords": q["title"], "location": q["location"], "f_TPR": f"r{since_seconds}", "sortBy": "DD", "start": start}
    if q["remote"]:
        p["f_WT"] = "2"
    else:
        p["distance"] = q["distance"]
    return p


def due(conn, now) -> tuple[bool, datetime | None]:
    last = db.kv_get(conn, "linkedin_last_sweep")
    if not last:
        return True, None
    nxt = normalize.parse_dt(last) + timedelta(hours=config.LINKEDIN_MIN_HOURS)
    return now >= nxt, nxt


def pending_rows(conn) -> list:
    """Jobs saved without a description that are still worth a job-page fetch."""
    return conn.execute(
        "SELECT * FROM jobs WHERE source = 'linkedin' AND description IS NULL AND filter_passed = 1 "
        "AND duplicate_of IS NULL AND emailed_at IS NULL AND description_tries < ? ORDER BY id",
        (config.LINKEDIN_DESCRIPTION_TRIES,)).fetchall()


# --- the sweep -------------------------------------------------------------------
def sweep(conn, s, now=None, get=None, sleep=time.sleep) -> dict:
    """Returns {"jobs": new jobs, "refreshed": {job id: fields from its job page}, "found", "errors", "skipped"}."""
    now = now or datetime.now(timezone.utc)
    out = {"jobs": [], "refreshed": {}, "found": 0, "errors": [], "skipped": None}
    ok, nxt = due(conn, now)
    if not ok:
        out["skipped"] = f"next LinkedIn sweep after {nxt.astimezone():%H:%M}"
        log.info("linkedin: %s", out["skipped"])
        return out
    client = Client(config.LINKEDIN_REQUEST_BUDGET, get, sleep)
    blocked = None
    try:
        # 1. descriptions still missing from earlier sweeps
        for row in pending_rows(conn):
            out["refreshed"][row["id"]] = _detail(client, row["url"], row["title"], row["location"],
                                                  row["work_model"] == "remote")
            conn.execute("UPDATE jobs SET description_tries = description_tries + 1 WHERE id = ?", (row["id"],))
        # 2. searches, least recently successful first
        last = {r["key"]: r["last_success"] for r in conn.execute("SELECT * FROM linkedin_queries")}
        queries = sorted(build_queries(s), key=lambda q: last.get(q["key"]) or "")
        need_detail = []
        for q in queries:
            since = normalize.parse_dt(last[q["key"]]) if last.get(q["key"]) else now - timedelta(hours=config.HOURS_OLD)
            seconds = int((now - since).total_seconds()) + 3600  # one hour of overlap
            new = _search(conn, client, q, min(seconds, 30 * 86400), out["jobs"])
            out["found"] += len(new)
            log.info("linkedin: %s @ %s%s -> %d new", q["title"], q["location"], " (remote)" if q["remote"] else "", len(new))
            conn.execute("INSERT INTO linkedin_queries(key, last_success) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET last_success = excluded.last_success", (q["key"], now.isoformat()))
            out["jobs"] += new
            # 3. job pages only for jobs a description could still let through
            need_detail += [j for j in new if filters.check(j, s)[0]]
        for j in need_detail:
            j.update(_detail(client, j["url"], j["title"], j["location"], j["work_model"] == "remote"))
    except OutOfBudget:
        log.info("linkedin: request budget of %d used; the rest waits for the next sweep", config.LINKEDIN_REQUEST_BUDGET)
    except Blocked as e:
        blocked = str(e)
    except requests.RequestException as e:
        out["errors"].append(f"linkedin: {type(e).__name__}: {str(e)[:200]}")
    if blocked:
        out["errors"].append(f"linkedin: blocked ({blocked}) after {client.used} requests; "
                             f"next sweep in {config.LINKEDIN_MIN_HOURS:g} h")
        log.warning(out["errors"][-1])
    db.kv_set(conn, "linkedin_last_sweep", now.isoformat())
    conn.commit()
    log.info("linkedin: %d requests used, %d new jobs, %d job pages refreshed",
             client.used, out["found"], sum(1 for v in out["refreshed"].values() if v))
    return out


def _search(conn, client, q, seconds, already) -> list[dict]:
    """New jobs for one query: pages newest first, until a page brings nothing new or runs short.
    (Not 'stop at the first known job': promoted posts aren't in date order.)"""
    known = {j["source_job_id"] for j in already}
    new = []
    for page in range(config.LINKEDIN_MAX_PAGES):
        r = client.get(SEARCH, _params(q, seconds, page * PER_PAGE))
        cards = parse_cards(r.text, q["remote"])
        fresh = [j for j in cards if j["source_job_id"] not in known
                 and not db.seen(conn, j["source"], j["source_job_id"], j["url"])]
        known.update(j["source_job_id"] for j in fresh)
        new += fresh
        if not fresh or len(cards) < PER_PAGE:
            break
    return new


def _detail(client, url, title, location, remote) -> dict:
    try:
        r = client.get(url)
    except requests.RequestException as e:
        log.warning("linkedin: job page %s: %s", url, e)
        return {}
    if not r.url.split("?")[0].rstrip("/").startswith(f"{BASE}/jobs/view"):
        log.warning("linkedin: job page %s redirected to %s", url, r.url)
        return {}
    d = parse_detail(r.text)
    if d.get("description"):
        d["work_model"] = normalize.detect_work_model(title, location, d["description"], is_remote=remote)
    return {k: v for k, v in d.items() if v is not None}
