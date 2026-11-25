"""Robert Half Canada: decoding the embedded JS string, paging newest-first, recency (no network)."""
import json
from datetime import datetime, timezone

import pytest
import requests

from collector import db, pipeline
from collector.settings import Settings
from collector.sources import roberthalf

NOW = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)


def job(n, posted="2026-11-25", **kw):
    r = {"unique_job_number": f"00{n}-caen", "jobtitle": "IT Project Manager", "city": "Kitchener", "stateprovince": "ON",
         "description": "<p>On behalf of our client, a hybrid role.</p><ul><li>Jira</li></ul>", "remote": "Hybrid",
         "emptype": "Contract to Permanent", "payrate_min": "45.00", "payrate_max": "55.00", "payrate_period": "Hourly",
         "salary_currency": "CAD", "date_posted": f"{posted}T00:00:00Z",
         "job_detail_url": f"https://www.roberthalf.com/ca/en/job/kitchener-on/it-project-manager/00{n}-caen"}
    r.update(kw)
    return r


def page(jobs, found):
    """Like the real page: JSON inside a single-quoted JS string, with \\x22 and \\u002D escapes,
    and non-ASCII as JS \\uXXXX UTF-16 units (an emoji is two of them)."""
    text = json.dumps({"data": {"found": str(found), "jobs": jobs}}, ensure_ascii=False)
    text = text.replace("\\", "\\\\").replace('"', "\\x22").replace("-", "\\u002D").replace("/", "\\/").replace("'", "\\'")
    units = lambda c: c.encode("utf-16-be")
    text = "".join(c if ord(c) < 128 else "".join(f"\\u{units(c)[i:i + 2].hex()}" for i in range(0, len(units(c)), 2))
                   for c in text)
    return f"<script>aemSettings.rh_job_search.initialResults = JSON.parse('{text}');</script>"


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSite:
    """Page 1: 25 of today's jobs. Page 2: 2 from today, then older ones."""
    def __init__(self, status=200):
        self.calls, self.status = [], status

    def __call__(self, url, **kw):
        self.calls.append(url)
        if self.status != 200:
            return Resp(status=self.status)
        if url.endswith("pagenumber=2"):
            return Resp(page([job(100), job(101), job(102, posted="2026-11-20"), job(103, posted="2026-11-19")], 60))
        return Resp(page([job(i) for i in range(25)], 60))


@pytest.fixture
def s():
    return Settings(target_titles=["Project Manager", "IT Project Manager"], locations=["Kitchener ON"])


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_to_job_fields():
    j = roberthalf.to_job(job(1, description="<p>Caf\u00e9 \u2019quotes\u2019 \\ and 'single'</p>"))
    assert j["source_job_id"] == "rh-001-caen" and j["location"] == "Kitchener, ON, Canada"
    assert (j["work_model"], j["employment_type"]) == ("hybrid", "contract, full-time")
    assert (j["salary_min"], j["salary_max"], j["salary_interval"], j["salary_currency"]) == (45.0, 55.0, "hourly", "CAD")
    assert j["posted_at"] == "2026-11-25"
    no_pay = roberthalf.to_job(job(2, payrate_min="", payrate_max=""))
    assert no_pay["salary_min"] is None and no_pay["salary_currency"] is None


def test_js_string_round_trip():
    raw = [job(1, description="Caf\u00e9 \u2019x\u2019 \\ back'slash \"quoted\" a/b \u002D-")]
    got, found = roberthalf.parse_page(page(raw, 1))
    assert got == raw and found == 1


def test_emoji_survives_decoding_and_sqlite(conn, s):
    html = page([job(1, description="<p>Great team \U0001F600 caf\u00e9</p>")], 1)
    assert "\\ud83d\\ude00" in html  # the page carries the emoji as a surrogate pair, like the real one
    raw, _ = roberthalf.parse_page(html)
    j = roberthalf.to_job(raw[0])
    assert j["description"] == "Great team \U0001F600 caf\u00e9"
    pipeline.process(conn, s, [j], run_id=None)  # used to raise UnicodeEncodeError on lone surrogates
    assert conn.execute("SELECT description FROM jobs").fetchone()[0] == "Great team \U0001F600 caf\u00e9"


def test_js_string_escapes():
    assert roberthalf.js_string(r"a\bb\fc\nd\x41\u0042\\\'") == "a\bb\fc\nd" + "AB\\'"
    assert roberthalf.js_string(r"x\ud83dy") == "x\ufffdy"  # a stray half can't be stored: replaced


def test_malformed_job_is_skipped_not_the_page(conn, s):
    bad_id = job(1); del bad_id["unique_job_number"]
    site = lambda url, **kw: Resp(page([bad_id, job(2, payrate_min="N/A"), job(3)], 3))
    jobs = roberthalf.fetch(conn, s, get=site, sleep=lambda x: None, now=NOW)
    assert [j["source_job_id"] for j in jobs] == ["rh-003-caen"]


def test_fetch_pages_until_old_jobs(conn, s):
    site = FakeSite()
    jobs = roberthalf.fetch(conn, s, get=site, sleep=lambda x: None, now=NOW)
    # one search ("IT Project Manager" is covered by "Project Manager"); page 2 reaches old jobs, so no page 3
    assert site.calls == ["https://www.roberthalf.com/ca/en/jobs/all/project-manager",
                          "https://www.roberthalf.com/ca/en/jobs/all/project-manager?pagenumber=2"]
    assert len(jobs) == 27 and all(j["description"] for j in jobs)


def test_undated_job_is_kept_and_does_not_stop_paging(conn, s):
    site = FakeSite()
    real = site.__call__
    def first_page_undated(url, **kw):
        r = real(url, **kw)
        if url.endswith("project-manager"):
            r.text = page([job(0, date_posted="")] + [job(i) for i in range(1, 25)], 60)
        return r
    jobs = roberthalf.fetch(conn, s, get=first_page_undated, sleep=lambda x: None, now=NOW)
    assert len(site.calls) == 2  # page 2 still fetched
    assert "rh-000-caen" in {j["source_job_id"] for j in jobs} and len(jobs) == 27


def test_known_jobs_skipped_and_errors_recorded(conn, s):
    pipeline.process(conn, s, roberthalf.fetch(conn, s, get=FakeSite(), sleep=lambda x: None, now=NOW), run_id=None)
    assert roberthalf.fetch(conn, s, get=FakeSite(), sleep=lambda x: None, now=NOW) == []
    def blocked(conn, s):
        return roberthalf.fetch(conn, s, get=FakeSite(status=403), sleep=lambda x: None, now=NOW)
    _, _, errors, _ = pipeline.collect(conn, s, lambda s: ([], {}, {}), lambda c: [],
                                       lambda conn, s: {"jobs": [], "refreshed": {}, "found": 0, "errors": [], "skipped": "x"},
                                       {"roberthalf": blocked})
    assert errors["roberthalf"] == ["roberthalf: HTTPError: 403"]
