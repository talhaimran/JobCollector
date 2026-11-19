"""Greenhouse, Lever and Ashby public job-board feeds."""
from datetime import datetime, timedelta, timezone

from .. import config, normalize
from . import get_json

URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}",
}
NORMALIZERS = {"greenhouse": normalize.from_greenhouse, "lever": normalize.from_lever,
               "ashby": normalize.from_ashby}


def _postings(system: str, data):
    return data if system == "lever" else data.get("jobs", [])


def recent(raw: list, max_age_hours: int, now: datetime | None = None) -> list:
    """Keep postings created/updated within max_age_hours; keep undated ones."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=max_age_hours)
    keep = []
    for j in raw:
        ts = normalize.feed_timestamp(j)
        if ts is None or normalize.parse_dt(ts) >= cutoff:
            keep.append(j)
    return keep


def fetch(company) -> list[dict]:
    """company: settings.TargetCompany. Raises on network/HTTP errors (caller records them)."""
    data = get_json(URLS[company.system].format(board=company.board))
    raw = recent(_postings(company.system, data), config.FEED_MAX_AGE_HOURS)
    return [NORMALIZERS[company.system](j, company.name) for j in raw]
