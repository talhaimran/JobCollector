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
    # Kitchener p1, 4 job pages, then Canada p1 (its reserved request); Kitchener p2 waits for next sweep
    assert sum(1 for j in out["jobs"] if j.get("description")) == 4
    assert [c[1]["location"] for c in fake.calls if c[0] == linkedin.SEARCH] == ["Kitchener ON", "Canada"]
    assert [r[0] for r in conn.execute("SELECT key FROM linkedin_queries")] == ["Project Manager|Canada|remote"]
    assert run(conn, s, fake, NOW + timedelta(hours=3))["skipped"]  # min 4 h between sweeps
    assert not run(conn, s, fake, NOW + timedelta(hours=4))["skipped"]


def test_oldest_query_first_and_window_since_last_success(conn, s, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 1)
    run(conn, s, FakeLinkedIn())  # budget ends after page 1 of Kitchener: not a success
    assert conn.execute("SELECT COUNT(*) FROM linkedin_queries").fetchone()[0] == 0
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 2)
    run(conn, s, FakeLinkedIn(), NOW + timedelta(hours=4))   # Kitchener p1, then Canada p1 (reserved): Canada done
    keys = [r[0] for r in conn.execute("SELECT key FROM linkedin_queries")]
    assert keys == ["Project Manager|Canada|remote"]
    fake = FakeLinkedIn()
    run(conn, s, fake, NOW + timedelta(hours=8))
    assert fake.calls[0][1]["location"] == "Kitchener ON"  # never finished, so it goes before the one that did
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


def test_job_pages_follow_their_search_page(conn, s, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 100)
    fake = FakeLinkedIn()
    run(conn, s, fake)
    kinds = ["search" if c[0] == linkedin.SEARCH else "job" for c in fake.calls]
    # page 1, its 9 passing jobs, page 2, its 3 jobs, then the remote search (nothing new, so one page)
    assert kinds == ["search"] + ["job"] * 9 + ["search"] + ["job"] * 3 + ["search"]


def test_block_keeps_jobs_from_finished_pages(conn, s, monkeypatch):
    monkeypatch.setattr(config, "LINKEDIN_REQUEST_BUDGET", 100)
    out = run(conn, s, FakeLinkedIn(block_after=10))  # page 1 + its 9 job pages, then page 2 is blocked
    assert "blocked" in out["errors"][0]
    assert len(out["jobs"]) == 10 and sum(1 for j in out["jobs"] if j.get("description")) == 9
    assert conn.execute("SELECT COUNT(*) FROM linkedin_queries").fetchone()[0] == 0  # unfinished: first next time


def _page(text):
    return f'<div class="show-more-less-html__markup"><p>{text}</p></div>'


@pytest.mark.parametrize("text, model", [
    ("Lead agile ceremonies for our payments teams.", "unknown"),  # LinkedIn's remote filter alone isn't proof
    ("This is a fully remote role open across Canada.", "remote"),
    ("Hybrid role: 3 days a week in our Toronto office.", "hybrid"),
])
def test_remote_search_label_comes_from_the_job_page_not_the_filter(text, model):
    # LinkedIn's logged-out remote filter (f_WT=2) returns on-site jobs too, e.g. TD's Scrum Master II in Toronto
    client = linkedin.Client(10, lambda url, params=None, **kw: Resp(_page(text), url=url), lambda _: None)
    d = linkedin._detail(client, f"{linkedin.BASE}/jobs/view/1", "Scrum Master II", "Toronto, Ontario, Canada")
    assert d["work_model"] == model
