"""Turn raw postings from any source into one dict shape, plus shared text helpers."""
import html
import math
import re
from datetime import datetime, timezone
from functools import lru_cache

FIELDS = ["source", "source_job_id", "url", "title", "company", "location", "work_model",
          "employment_type", "salary_min", "salary_max", "salary_currency", "salary_interval",
          "posted_at", "found_at", "description"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(v):
    """None for NaN / empty values pandas hands us."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, str) and not v.strip():
        return None
    return v


@lru_cache(maxsize=2048)
def _phrase_re(phrase: str) -> re.Pattern:
    body = r"\s+".join(re.escape(w) for w in phrase.split())
    # word boundaries that also work for phrases like "AI/ML" or "Monday.com"
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def phrase_in(phrase: str, text: str | None) -> bool:
    return bool(text) and bool(_phrase_re(phrase).search(text))


def html_to_text(s: str | None) -> str | None:
    if not s:
        return None
    s = html.unescape(html.unescape(s))  # Greenhouse double-escapes content
    s = re.sub(r"<(br|/p|/li|/h\d|/div)\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", s).strip()


# --- work model / employment type -----------------------------------------
_HYBRID_DESC = re.compile(r"\bhybrid\s+(role|position|work|working|model|schedule|arrangement|environment)\b|\bthis is a hybrid\b", re.I)
_REMOTE_DESC = re.compile(r"\b(fully remote|100% remote|remote[- ]first|this (is a|role is) remote)\b", re.I)
_ONSITE_DESC = re.compile(r"\b(on-?site (role|position)|in[- ]office (role|position)|fully on-?site)\b", re.I)


def detect_work_model(title, location, description=None, is_remote=None, hint=None) -> str:
    """hint: a source's own field (e.g. 'Hybrid', 'on-site', 'OnSite', 'remote')."""
    if hint:
        h = re.sub(r"[^a-z]", "", str(hint).lower())
        if h in ("remote", "hybrid", "onsite"):
            return "on-site" if h == "onsite" else h
    head = f"{title or ''} {location or ''}"
    if re.search(r"\bhybrid\b", head, re.I):
        return "hybrid"
    if is_remote or re.search(r"\bremote\b|work from home|\bwfh\b", head, re.I):
        return "remote"
    # shortcut: description phrases are a heuristic; extend the regexes if 'unknown' shows up a lot
    for model, rx in (("hybrid", _HYBRID_DESC), ("remote", _REMOTE_DESC), ("on-site", _ONSITE_DESC)):
        if description and rx.search(description):
            return model
    return "unknown"


_EMP = {"fulltime": "full-time", "parttime": "part-time", "contract": "contract",
        "contractor": "contract", "temporary": "contract", "temp": "contract",
        "internship": "internship", "intern": "internship", "permanent": "full-time"}


def norm_employment(value) -> str | None:
    value = clean(value)
    if not value:
        return None
    out = []
    for part in re.split(r"[,/;]", str(value)):
        key = re.sub(r"[^a-z]", "", part.lower())
        if key in _EMP and _EMP[key] not in out:
            out.append(_EMP[key])
    return ", ".join(out) or None


def norm_interval(v) -> str | None:
    v = clean(v)
    if not v:
        return None
    v = str(v).lower()
    for k in ("hour", "day", "week", "month", "year"):
        if k in v:
            return {"hour": "hourly", "day": "daily", "week": "weekly",
                    "month": "monthly", "year": "yearly"}[k]
    if "annual" in v or "salary" in v:
        return "yearly"
    return v


def _iso(v) -> str | None:
    v = clean(v)
    if v is None:
        return None
    if isinstance(v, (int, float)):  # epoch ms (Lever)
        return datetime.fromtimestamp(v / 1000, timezone.utc).replace(microsecond=0).isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _job(**kw) -> dict:
    job = {f: clean(kw.get(f)) for f in FIELDS}
    job["found_at"] = job["found_at"] or now_iso()
    job["source_job_id"] = str(job["source_job_id"]) if job["source_job_id"] is not None else None
    return job


# --- per-source -----------------------------------------------------------
def from_jobspy(row: dict) -> dict:
    title, loc, desc = clean(row.get("title")), clean(row.get("location")), clean(row.get("description"))
    return _job(
        source=row.get("site"), source_job_id=row.get("id"), url=row.get("job_url"),
        title=title, company=row.get("company"), location=loc,
        work_model=detect_work_model(title, loc, desc, clean(row.get("is_remote")) is True,
                                     clean(row.get("work_from_home_type"))),
        employment_type=norm_employment(row.get("job_type")),
        salary_min=row.get("min_amount"), salary_max=row.get("max_amount"),
        salary_currency=row.get("currency"), salary_interval=norm_interval(row.get("interval")),
        posted_at=_iso(row.get("date_posted")), description=desc)


def from_greenhouse(j: dict, company: str) -> dict:
    title, loc = j.get("title"), (j.get("location") or {}).get("name")
    desc = html_to_text(j.get("content"))
    return _job(source="greenhouse", source_job_id=j.get("id"), url=j.get("absolute_url"),
                title=title, company=company, location=loc,
                work_model=detect_work_model(title, loc, desc),
                posted_at=j.get("first_published") or j.get("updated_at"), description=desc)


def from_lever(j: dict, company: str) -> dict:
    cat = j.get("categories") or {}
    title, loc = j.get("text"), cat.get("location")
    desc = "\n".join(filter(None, [j.get("descriptionPlain"),
                                   *[f"{l.get('text')}\n{html_to_text(l.get('content')) or ''}" for l in j.get("lists") or []],
                                   j.get("additionalPlain")]))
    sal = j.get("salaryRange") or {}
    return _job(source="lever", source_job_id=j.get("id"), url=j.get("hostedUrl"),
                title=title, company=company, location=loc,
                work_model=detect_work_model(title, loc, desc, hint=j.get("workplaceType")
                                             if j.get("workplaceType") != "unspecified" else None),
                employment_type=norm_employment(cat.get("commitment")),
                salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=sal.get("currency"),
                salary_interval=norm_interval(sal.get("interval")),
                posted_at=_iso(j.get("createdAt")), description=desc)


def from_ashby(j: dict, company: str) -> dict:
    title, loc, desc = j.get("title"), j.get("location"), j.get("descriptionPlain")
    return _job(source="ashby", source_job_id=j.get("id"), url=j.get("jobUrl"),
                title=title, company=company, location=loc,
                work_model=detect_work_model(title, loc, desc, is_remote=j.get("isRemote"),
                                             hint=j.get("workplaceType")),
                employment_type=norm_employment(j.get("employmentType")),
                posted_at=j.get("publishedAt"), description=desc)


def feed_timestamp(j: dict) -> str | None:
    """Most recent created/updated time a company feed gives, as ISO (for the 24h cut)."""
    stamps = [_iso(j.get(k)) for k in ("updated_at", "first_published", "createdAt", "updatedAt", "publishedAt")]
    stamps = [s for s in stamps if s]
    return max(stamps, key=parse_dt) if stamps else None


def parse_dt(s: str) -> datetime:
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
