"""End-to-end run with fake sources and a temp DB (no network)."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from collector import db, emailer, pipeline
from collector.settings import Settings, TargetCompany
from collector.sources import feeds, jobspy_src

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def s():
    return Settings(target_titles=["Project Manager"], exclude_titles=["Technical Project Manager"],
                    locations=["Kitchener ON"], remote_in=["Canada"], work_model=["Remote", "Hybrid"],
                    must_have=["Jira", "Agile"], email_cutoff=30,
                    target_companies=[TargetCompany("Acme", "greenhouse", "acme"),
                                      TargetCompany("Down", "lever", "down")])


def jobspy_row(i, **kw):
    r = {"site": "linkedin", "id": f"li-{i}", "job_url": f"https://li/{i}", "title": "Project Manager",
         "company": "Acme Inc", "location": "Kitchener, ON, Canada", "is_remote": False,
         "description": "Hybrid role. Jira and Agile.", "date_posted": "2026-11-18"}
    r.update(kw)
    return r


ROWS = [jobspy_row(1),
        jobspy_row(2, title="Technical Project Manager"),
        jobspy_row(3, company="Other", description="Fully remote", location="Remote, US", is_remote=True)]


def fake_scrape(**kw):
    if kw["site_name"] == ["glassdoor"]:
        raise RuntimeError("403 blocked")
    return pd.DataFrame(ROWS if kw["site_name"] == ["linkedin"] else [])


def fake_jobspy(s):
    return jobspy_src.fetch(s, scrape=fake_scrape, sleep=lambda _: None)


def fake_feed(c):
    if c.board == "down":
        raise ConnectionError("boom")
    raw = json.loads((FIX / "greenhouse.json").read_text())["jobs"]
    return [feeds.NORMALIZERS[c.system](j, c.name) for j in raw]  # same job as li-1 -> duplicate


def test_full_run_then_rerun(s, tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    sent, printed = [], []
    r = pipeline.run(False, s, conn, fake_jobspy, fake_feed, send=lambda *a: sent.append(a))

    # every query x site was tried; glassdoor failures and the lever feed didn't stop the run
    assert "glassdoor" in r["errors"] and "lever:down" in r["errors"]
    assert r["found_by_source"]["linkedin"] == 6  # raw rows from 2 queries; dedupe happens after
    assert r["emailed"] == 2 and len(sent) == 1 and "https://li/1" in sent[0][2]  # li-1 + greenhouse 111

    rows = {x["source_job_id"]: x for x in conn.execute("SELECT * FROM jobs")}
    assert rows["li-2"]["filter_reason"] == "excluded title: Technical Project Manager"
    assert rows["li-3"]["filter_reason"] == "remote restricted to united states"
    assert rows["111"]["filter_passed"] == 1 and rows["111"]["duplicate_of"] is None
    assert rows["112"]["filter_reason"] == "title not in target titles"
    assert rows["li-1"]["emailed_at"] and json.loads(rows["li-1"]["matched_keywords"]) == ["Jira", "Agile"]

    run = conn.execute("SELECT * FROM runs").fetchone()
    assert json.loads(run["jobs_found"])["linkedin"] == 6 and run["new_jobs"] == 5
    assert "salary floor" in json.loads(run["filters_off"])

    r2 = pipeline.run(True, s, conn, fake_jobspy, fake_feed, send=lambda *a: sent.append(a),
                      out=printed.append)
    assert r2["new"] == 0 and r2["emailed"] == 0 and len(sent) == 1 and printed == []


def test_cross_source_duplicate_not_emailed(s, tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    s.target_companies = [TargetCompany("Acme", "greenhouse", "acme")]
    gh = lambda c: [feeds.NORMALIZERS["greenhouse"](
        {"id": 9, "title": "Project Manager", "absolute_url": "https://gh/9",
         "location": {"name": "Kitchener, Ontario"}, "content": "Hybrid role. Jira"}, "ACME Ltd.")]
    printed = []
    pipeline.run(True, s, conn, fake_jobspy, gh, out=printed.append)
    dup = conn.execute("SELECT duplicate_of FROM jobs WHERE source='greenhouse'").fetchone()[0]
    first = conn.execute("SELECT id FROM jobs WHERE source_job_id='li-1'").fetchone()[0]
    assert dup == first and "https://gh/9" not in printed[0]


def test_lock_blocks_second_run(tmp_path):
    with db.run_lock(tmp_path / "l") as a:
        with db.run_lock(tmp_path / "l") as b:
            assert a is True and b is False


def test_feed_recent_filter():
    raw = json.loads((FIX / "greenhouse.json").read_text())["jobs"]
    now = datetime(2026, 11, 18, 20, tzinfo=timezone.utc)
    assert [j["id"] for j in feeds.recent(raw, 24, now)] == [111]


def test_jobspy_query_shapes(s):
    s.locations, s.target_titles = ["Kitchener ON", "Waterloo ON"], ["PM", "Senior PM", "Scrum Master"]
    assert jobspy_src.search_titles(s.target_titles) == ["PM", "Scrum Master"]  # 'Senior PM' covered by 'PM'
    assert len(jobspy_src.build_queries(s)) == 2 * 2 + 2 * 1
    remote = {"title": "PM", "location": "Canada", "remote": True}
    assert "is_remote" not in jobspy_src.scrape_kwargs("indeed", remote)
    assert jobspy_src.scrape_kwargs("linkedin", remote)["is_remote"] is True


def test_email_content():
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    j = {"title": "PM <x>", "url": "https://a", "company": "A", "location": "Kitchener", "work_model": "unknown",
         "source": "indeed", "posted_at": "2026-11-18T09:30:00+00:00", "prescore_total": 72.5,
         "prescore_keywords": 60, "prescore_title": 100, "matched_keywords": ["Jira"],
         "salary_max": 100000, "salary_currency": "USD", "salary_interval": "yearly"}
    subj, html, text = emailer.build([j], {"found": 9, "new": 3, "passed": 2}, ["salary floor"],
                                     {"glassdoor": ["glassdoor: blocked"]}, now)
    assert subj.startswith("[Jobs] 1 new matches – 2026-11-18")
    assert "2h ago" in text and "work model unknown" in text and "salary in USD" in text
    assert "PM &lt;x&gt;" in html and "href='https://a'" in html
    assert "Filters off: salary floor" in text and "glassdoor: blocked" in text
    assert "found 9 / new 3 / passed 2 / emailed 1" in text
    assert emailer.ago("2026-11-16", now) == "2d ago"


def test_jobspy_logged_errors_count_as_errors_with_backoff(s):
    import logging
    s.target_titles, s.locations = ["PM"], ["A ON", "B ON", "C ON", "D ON"]  # 5 queries
    s.remote_in = ["Canada"]

    def scrape(**kw):
        if kw["site_name"] == ["linkedin"]:
            logging.getLogger("jobspy.linkedin").error("response status code 429 (blocked: use a proxy)")
            return pd.DataFrame([jobspy_row(kw["location"].replace(" ", ""))])  # partial rows kept
        return pd.DataFrame([])

    sleeps = []
    jobs, found, errors = jobspy_src.fetch(s, scrape=scrape, sleep=sleeps.append)
    assert found["linkedin"] == 3                       # 3 attempts, then skipped
    assert len(errors["linkedin"]) == 4 and "429" in errors["linkedin"][0]
    assert "skipped for rest of run" in errors["linkedin"][-1]
    assert 30 in sleeps and 60 in sleeps                # backoff
    assert "indeed" not in errors
    assert logging.getLogger("jobspy").handlers == []   # capture handler removed
