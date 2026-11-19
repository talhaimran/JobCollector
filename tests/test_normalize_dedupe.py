import json
import math
from pathlib import Path

from collector import dedupe, normalize as n

FIX = Path(__file__).parent / "fixtures"


def test_fingerprint_strips_suffixes_punctuation_and_country():
    a = dedupe.fingerprint("Acme Inc.", "Senior Project Manager!", "Kitchener, ON, Canada")
    b = dedupe.fingerprint("ACME", "senior project  manager", "Kitchener, Ontario")
    assert a == b == "acme|senior project manager|kitchener"
    assert dedupe.fingerprint("Acme Ltd", "PM", "Waterloo") != a


def test_phrase_in_handles_punctuation_and_whole_words():
    assert n.phrase_in("AI/ML", "Experience with AI/ML tools")
    assert n.phrase_in("Monday.com", "We use monday.com daily")
    assert n.phrase_in("risk management", "strong Risk\nManagement skills")
    assert not n.phrase_in("SLA", "translations")
    assert not n.phrase_in("Claude", "Claudette")


def test_jobspy_row():
    row = {"site": "indeed", "id": "in-1", "job_url": "https://x/1", "title": "Project Manager",
           "company": "Acme", "location": "Kitchener, ON, Canada", "date_posted": None,
           "job_type": "fulltime, contract", "interval": "yearly", "min_amount": math.nan,
           "max_amount": 90000.0, "currency": "CAD", "is_remote": False,
           "description": "This is a hybrid role in Kitchener."}
    j = n.from_jobspy(row)
    assert j["employment_type"] == "full-time, contract"
    assert j["work_model"] == "hybrid" and j["salary_min"] is None and j["salary_max"] == 90000
    assert j["found_at"] and j["posted_at"] is None


def test_work_model_detection():
    assert n.detect_work_model("PM (Remote)", "Canada") == "remote"
    assert n.detect_work_model("PM", "Toronto", is_remote=True) == "remote"
    assert n.detect_work_model("PM", "Toronto", hint="OnSite") == "on-site"
    assert n.detect_work_model("PM", "Toronto", "Great team") == "unknown"


def test_feed_fixtures():
    gh = json.loads((FIX / "greenhouse.json").read_text())["jobs"][0]
    j = n.from_greenhouse(gh, "Acme")
    assert j["source"] == "greenhouse" and j["source_job_id"] == "111"
    assert "<" not in j["description"] and "Jira" in j["description"]
    lv = json.loads((FIX / "lever.json").read_text())[0]
    j = n.from_lever(lv, "Beta")
    assert j["work_model"] == "hybrid" and j["employment_type"] == "full-time"
    assert j["salary_interval"] == "yearly" and j["posted_at"].startswith("2026-11-18")
    ab = json.loads((FIX / "ashby.json").read_text())["jobs"][0]
    j = n.from_ashby(ab, "Gamma")
    assert j["work_model"] == "remote" and j["employment_type"] == "full-time"
    assert n.feed_timestamp(gh) == "2026-11-18T10:00:00-05:00"
