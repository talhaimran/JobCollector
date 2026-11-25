"""Randstad Canada: parsing the embedded JSON, paging, recency and job pages (no network)."""
import json
from datetime import datetime, timezone

import pytest
import requests

from collector import db, pipeline
from collector.settings import Settings
from collector.sources import randstad

NOW = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)


def src(n, city="Waterloo", created="2026-11-24", snippet="Lead delivery of a claims program.", salary=None):
    return {"JobId": f"REF{n}", "JobDates": {"DateCreated": created},
            "JobInformation": {"Title": "Senior Project Manager", "Description": snippet, "JobType": "Contract"},
            "JobLocation": {"City": city, "Region": "Ontario"},
            "BlueXJobData": {"JobId": str(n), "CompanyName": "Randstad Canada"},
            "BlueXSanitized": {"Title": "senior-project-manager", "City": city.lower()},
            "Salary": salary or {"SalaryMin": "51.37", "SalaryMax": "51.37", "CompensationType": "per hour",
                                 "HideSalaryMin": "false", "HideSalaryMax": "false"}}


def page(key, hits, total):
    # a string containing "};" checks the JSON isn't cut short by a naive regex
    data = {key: {"hits": {"total": total, "hits": [{"_source": h} for h in hits]}}, "x": "a};b"}
    return f"<html><script>window.__ROUTE_DATA__ = {json.dumps(data)};window.__SEO_DATA__ = {{}};</script></html>"


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSite:
    """Page 1: 30 hits (one old, one in Toronto), page 2: 5 hits. Job pages give a long hybrid description."""
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, **kw):
        self.calls.append(url)
        if self.status != 200:
            return Resp(status=self.status)
        if "/q-" not in url:
            return Resp(page("jobData", [src(0, snippet="Hybrid, 3 days a week in our Waterloo office. Jira, Agile.")], 1))
        if url.endswith("page-2/"):
            return Resp(page("searchResults", [src(100 + i) for i in range(5)], 35))
        hits = [src(i) for i in range(28)] + [src(28, created="2026-10-01"), src(29, city="Toronto")]
        return Resp(page("searchResults", hits, 35))


@pytest.fixture
def s():
    return Settings(target_titles=["Project Manager", "Senior Project Manager"], locations=["Waterloo ON"],
                    work_model=["Hybrid", "On-site"], remote_in=["Canada"])


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_to_job_fields():
    j = randstad.to_job(src(7))
    assert j["source_job_id"] == "rs-REF7"
    assert j["url"] == "https://www.randstad.ca/jobs/senior-project-manager_waterloo_7/"
    assert j["location"] == "Waterloo, Ontario, Canada"
    assert (j["employment_type"], j["salary_min"], j["salary_interval"], j["salary_currency"]) == ("contract", 51.37, "hourly", "CAD")
    assert j["description"] is None  # the search snippet is cut short; only the job page's text is used
    hidden = randstad.to_job(src(8, salary={"SalaryMin": "1", "HideSalaryMin": "true", "HideSalaryMax": "true", "SalaryMax": "2"}))
    assert hidden["salary_min"] is None and hidden["salary_max"] is None


def test_fetch_pages_filters_and_job_pages(conn, s):
    site = FakeSite()
    jobs = randstad.fetch(conn, s, get=site, sleep=lambda x: None, now=NOW)
    # one search ("Senior Project Manager" is covered by "Project Manager"), two pages
    assert site.calls[0] == "https://www.randstad.ca/jobs/q-project-manager/"
    assert [u for u in site.calls if "/q-" in u] == [site.calls[0], site.calls[0] + "page-2/"]
    ids = {j["source_job_id"] for j in jobs}
    assert "rs-REF28" not in ids  # older than HOURS_OLD
    assert len(jobs) == 34
    toronto = next(j for j in jobs if j["source_job_id"] == "rs-REF29")
    assert toronto["description"] is None  # failed the location filter: no job page fetched
    local = next(j for j in jobs if j["source_job_id"] == "rs-REF0")
    assert "Jira" in local["description"] and local["work_model"] == "hybrid"
    assert len(site.calls) == 2 + 33  # 2 search pages + job pages for the 33 Waterloo jobs


def test_known_jobs_skipped(conn, s):
    pipeline.process(conn, s, randstad.fetch(conn, s, get=FakeSite(), sleep=lambda x: None, now=NOW), run_id=None)
    site = FakeSite()
    assert randstad.fetch(conn, s, get=site, sleep=lambda x: None, now=NOW) == []
    assert all("/q-" in u for u in site.calls)  # no job pages for jobs already saved


def test_blocked_raises_and_pipeline_records_it(conn, s):
    with pytest.raises(requests.HTTPError):
        randstad.fetch(conn, s, get=FakeSite(status=429), sleep=lambda x: None, now=NOW)
    def blocked(conn, s):
        raise requests.HTTPError("429")
    jobs, found, errors, _ = pipeline.collect(conn, s, lambda s: ([], {}, {}), lambda c: [],
                                              lambda conn, s: {"jobs": [], "refreshed": {}, "found": 0, "errors": [], "skipped": "x"},
                                              blocked)
    assert errors["randstad"] == ["randstad: HTTPError: 429"] and jobs == []
