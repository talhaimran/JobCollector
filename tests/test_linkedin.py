"""LinkedIn client: parsing, budget, oldest-first queries, 429 handling (no network)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from collector import config, db
from collector.settings import Settings
from collector.sources import linkedin

FIX = Path(__file__).parent / "fixtures"
FULL, SHORT, JOB = ((FIX / f).read_text() for f in ("linkedin_search_full.html", "linkedin_search_short.html", "linkedin_job.html"))
NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


class Resp:
    def __init__(self, text="", status=200, url=None):
        self.text, self.status_code, self.url = text, status, url

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


class FakeLinkedIn:
    """Search: page 1 full, page 2 short. Job pages: the fixture. `block_after` returns 429 from that request on."""
    def __init__(self, block_after=None):
        self.calls, self.block_after = [], block_after

    def __call__(self, url, params=None, **kw):
        self.calls.append((url, params))
        if self.block_after is not None and len(self.calls) > self.block_after:
            return Resp(status=429)
        if url == linkedin.SEARCH:
            return Resp(FULL if params["start"] == 0 else SHORT)
        return Resp(JOB, url=url)


@pytest.fixture
def s():
    return Settings(target_titles=["Project Manager"], exclude_titles=["Junior"], locations=["Kitchener ON"],
                    remote_in=["Canada"], work_model=["Remote", "Hybrid"], must_have=["Jira"])


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "jobs.db")


def run(conn, s, fake, now=NOW):
    return linkedin.sweep(conn, s, now=now, get=fake, sleep=lambda _: None)


def test_parse_cards_and_detail():
    cards = linkedin.parse_cards(FULL, remote=False)
    assert len(cards) == 10 and cards[0]["source_job_id"] == "li-4000"
    assert cards[0]["url"] == "https://www.linkedin.com/jobs/view/4000"
    assert cards[0]["title"] == "Project Manager" and cards[0]["company"] == "Acme Inc"
    assert cards[0]["location"] == "Kitchener, Ontario, Canada" and cards[0]["posted_at"] == "2026-11-19"
    assert linkedin.parse_cards(SHORT, remote=True)[0]["work_model"] == "remote"
    d = linkedin.parse_detail(JOB)
    assert "Jira" in d["description"] and "<" not in d["description"]
    assert d["employment_type"] == "full-time"
    assert (d["salary_min"], d["salary_max"], d["salary_currency"], d["salary_interval"]) == (85000, 105000, "CAD", "yearly")


def test_sweep_pages_and_fetches_descriptions_only_for_jobs_that_can_pass(conn, s, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 100)
    fake = FakeLinkedIn()
    out = run(conn, s, fake)
    searches = [c for c in fake.calls if c[0] == linkedin.SEARCH]
    assert len(searches) == 3  # Kitchener: full page then short page; Canada: page 1 has nothing new, stop
    assert searches[0][1]["distance"] == 25 and searches[0][1]["sortBy"] == "DD"
    assert searches[2][1]["f_WT"] == "2" and "distance" not in searches[2][1]
    jobs = {j["source_job_id"]: j for j in out["jobs"]}
    assert len(jobs) == 13  # second query's cards are the same jobs, so not counted twice
    assert jobs["li-4003"].get("description") is None  # 'Junior' title fails before any job page is fetched
    assert jobs["li-4000"]["work_model"] == "hybrid" and "Jira" in jobs["li-4000"]["description"]
    assert len(fake.calls) - len(searches) == 12  # 13 new jobs, minus the one excluded title


def test_budget_leaves_rest_pending_and_waits_min_hours(conn, s, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 6)
    fake = FakeLinkedIn()
    out = run(conn, s, fake)
    assert len(fake.calls) == 6 and not out["errors"]
    assert sum(1 for j in out["jobs"] if j.get("description")) == 3  # 3 searches + 3 job pages
    assert run(conn, s, fake, NOW + timedelta(hours=3))["skipped"]  # min 4 h between sweeps
    assert not run(conn, s, fake, NOW + timedelta(hours=4))["skipped"]


def test_oldest_query_first_and_window_since_last_success(conn, s, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 1)
    run(conn, s, FakeLinkedIn())  # budget ends after page 1 of Kitchener: not a success
    assert conn.execute("SELECT COUNT(*) FROM linkedin_queries").fetchone()[0] == 0
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 2)
    run(conn, s, FakeLinkedIn(), NOW + timedelta(hours=4))   # Kitchener: 2 pages, done
    keys = [r[0] for r in conn.execute("SELECT key FROM linkedin_queries")]
    assert keys == ["Project Manager|Kitchener ON|local"]
    fake = FakeLinkedIn()
    run(conn, s, fake, NOW + timedelta(hours=8))
    assert fake.calls[0][1]["location"] == "Canada"   # never-run query goes before the one that just ran
    assert fake.calls[0][1]["f_TPR"] == f"r{24 * 3600 + 3600}"


def test_429_stops_sweep_and_retries_after_the_usual_wait(conn, s):
    fake = FakeLinkedIn(block_after=1)
    out = run(conn, s, fake)
    assert len(fake.calls) == 2 and "blocked" in out["errors"][0] and "4 h" in out["errors"][0]
    assert run(conn, s, FakeLinkedIn(), NOW + timedelta(hours=3))["skipped"]
    out = run(conn, s, FakeLinkedIn(), NOW + timedelta(hours=4))  # no back-off after a block
    assert not out["skipped"] and not out["errors"]


def test_pending_descriptions_fetched_first(conn, s):
    db.insert_job(conn, {"source": "linkedin", "source_job_id": "li-1", "url": "https://www.linkedin.com/jobs/view/1",
                         "title": "Project Manager", "found_at": "x", "filter_passed": 1, "score_partial": 1})
    fake = FakeLinkedIn()
    out = run(conn, s, fake)
    assert fake.calls[0][0] == "https://www.linkedin.com/jobs/view/1"
    assert "Jira" in out["refreshed"][1]["description"]
    assert conn.execute("SELECT description_tries FROM jobs").fetchone()[0] == 1
