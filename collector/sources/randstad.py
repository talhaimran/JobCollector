"""Randstad Canada's public job search (randstad.ca), signed out.

Each search page embeds its results as JSON (window.__ROUTE_DATA__), 30 per page. robots.txt allows the plain
keyword search (/jobs/q-<title>/) but not the radius, sort or date filters, so each run searches Canada-wide per
title, pages through by relevance and keeps jobs created in the last HOURS_OLD. The accepted-cities and remote
filters do the rest. Search results carry only the start of the description, so new jobs that pass the filters
get their job page fetched for the full one. Any 403/429 or other HTTP error ends Randstad for this run.
"""
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from .. import config, db, filters, normalize
from .jobspy_src import search_titles
from .linkedin import HEADERS

log = logging.getLogger(__name__)
BASE = "https://www.randstad.ca"
PER_PAGE = 30
MARK = "window.__ROUTE_DATA__ = "


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def route_data(html: str) -> dict:
    i = html.find(MARK)
    if i < 0:
        raise ValueError("no __ROUTE_DATA__ in page (layout changed?)")
    return json.JSONDecoder().raw_decode(html, i + len(MARK))[0]


def _salary(sal: dict | None) -> dict:
    sal = sal or {}
    lo = None if sal.get("HideSalaryMin") == "true" else sal.get("SalaryMin")
    hi = None if sal.get("HideSalaryMax") == "true" else sal.get("SalaryMax")
    if not (lo or hi):
        return {}
    return {"salary_min": float(lo) if lo else None, "salary_max": float(hi) if hi else None,
            "salary_currency": "CAD", "salary_interval": normalize.norm_interval(sal.get("CompensationType"))}


def to_job(src: dict) -> dict:
    data, info, where = src.get("BlueXJobData", {}), src.get("JobInformation", {}), src.get("JobLocation", {})
    sane = src.get("BlueXSanitized", {})
    loc = ", ".join(x for x in (where.get("City"), where.get("Region"), "Canada") if x)
    title, snippet = info.get("Title") or data.get("Title"), info.get("Description")
    return normalize._job(
        source="randstad", source_job_id=f"rs-{src['JobId']}",
        url=f"{BASE}/jobs/{sane.get('Title')}_{sane.get('City')}_{data.get('JobId')}/",
        title=title, company=data.get("CompanyName") or "Randstad Canada", location=loc,
        work_model=normalize.detect_work_model(title, loc, snippet),  # the snippet is enough to spot "remote"/"hybrid"
        employment_type=normalize.norm_employment(info.get("JobType")),
        posted_at=(src.get("JobDates") or {}).get("DateCreated"), **_salary(src.get("Salary")))


def parse_search(html: str) -> tuple[list[dict], int]:
    """(raw job sources, total hits) from a search page."""
    hits = route_data(html)["searchResults"]["hits"]
    total = hits["total"]["value"] if isinstance(hits["total"], dict) else hits["total"]
    return [h["_source"] for h in hits["hits"]], total


def parse_detail(html: str) -> dict:
    hits = route_data(html)["jobData"]["hits"]["hits"]
    desc = normalize.html_to_text(hits[0]["_source"]["JobInformation"].get("Description")) if hits else None
    return {"description": desc} if desc else {}


class Client:
    def __init__(self, get=None, sleep=time.sleep):
        self._get, self._sleep, self.used = get or requests.Session().get, sleep, 0

    def get(self, url):
        if self.used:
            self._sleep(random.uniform(*config.RANDSTAD_DELAY_SECONDS))
        self.used += 1
        r = self._get(url, headers=HEADERS, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()  # 403/429 included: stop for this run, the next run tries again
        return r


def fetch(conn, s, get=None, sleep=time.sleep, now=None) -> list[dict]:
    """New recent jobs for every search title. Raises on HTTP errors; jobs found before an error are lost
    for this run only (they're still recent next run)."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=config.HOURS_OLD)).date().isoformat()  # dates only, so up to a day of overlap
    client, jobs, known = Client(get, sleep), [], set()
    for title in search_titles(s.target_titles):
        for page in range(config.RANDSTAD_MAX_PAGES):
            url = f"{BASE}/jobs/q-{slug(title)}/" + (f"page-{page + 1}/" if page else "")
            raw, total = parse_search(client.get(url).text)
            for src in raw:
                j = to_job(src)
                if (j["posted_at"] or "") < since or j["source_job_id"] in known \
                        or db.seen(conn, j["source"], j["source_job_id"], j["url"]):
                    continue
                known.add(j["source_job_id"])
                if filters.check(j, s)[0]:
                    j.update(_detail(client, j))
                jobs.append(j)
            if not raw or (page + 1) * PER_PAGE >= total:
                break
    log.info("randstad: %d requests, %d new jobs", client.used, len(jobs))
    return jobs


def _detail(client, j) -> dict:
    try:
        d = parse_detail(client.get(j["url"]).text)
    except (requests.RequestException, ValueError, KeyError) as e:
        if isinstance(e, requests.HTTPError):
            raise  # blocked: stop the whole source, don't keep hammering job pages
        log.warning("randstad: job page %s: %s", j["url"], e)
        return {}
    if d.get("description"):
        d["work_model"] = normalize.detect_work_model(j["title"], j["location"], d["description"])
    return d
