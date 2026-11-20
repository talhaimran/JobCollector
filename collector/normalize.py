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
_N = r"(?:[1-5]|one|two|three|four|five)"
_PLACE = r"(?:(?:the|our|an?)\s+)?(?:[\w-]+\s+){0,2}?(?:office|on-?\s?site|in[- ]person)"  # "in our Kitchener office"
_LABEL = r"\b(?:work(?:place)?\s*(?:model|type|arrangement|location|style)|location\s*type)\s*[:\-–]\s*"
_NOT_WORK = r"(?!\s+(?:cloud|delivery|agile|approach|methodolog\w*|project|waterfall|framework))"
# "3 days a week in the office", "2-3 days per week on-site", "in office 4 days", "days on site"
_DAYS = [re.compile(rf"\b({_N})(?:\s*(?:-|–|to|or)\s*({_N}))?\s+days?\s*(?:a|per|each|/)?\s*(?:week|wk)?"
                    rf"\s*(?:in|at|on|from)?\s*{_PLACE}", re.I),
         re.compile(rf"\b(?:in[- ]?{_PLACE}|on-?\s?site)\s+(?:for\s+)?({_N})(?:\s*(?:-|–|to|or)\s*({_N}))?\s+days?", re.I)]
_DAYS_PLAIN = re.compile(rf"\bdays?\s+(?:on-?\s?site|in[- ]{_PLACE}|in[- ]person)\b", re.I)
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_DESC = [  # checked in order; the first match wins
    ("on-site", re.compile(rf"\b(?:fully|100%)\s+(?:on-?\s?site|in[- ]office|in[- ]person)\b|{_LABEL}on-?\s?site", re.I)),
    ("hybrid", re.compile(rf"\bhybrid\s*[(\-–:]?\s*(?:role|position|work(?:ing|place)?|model|schedule|arrangement|setting|"
                          rf"opportunity|basis|job|environment|\d)\b|\b(?:is|be|a|an|on a|in a)\s+hybrid\b{_NOT_WORK}|"
                          rf"{_LABEL}hybrid|\bthis is a hybrid\b", re.I)),
    ("remote", re.compile(rf"\b(?:fully remote|100% remote|remote[- ]first|work(?:ing)? from home|"
                          rf"remote\s+(?:role|position|job|opportunity)|this (?:is a|role is)\s+(?:fully\s+)?remote)\b|"
                          rf"{_LABEL}remote", re.I)),
    ("on-site", re.compile(r"\b(?:on-?\s?site|in[- ]office|in[- ]person)\s+(?:role|position|job)\b|"
                           r"\bthis (?:is an?|role is)\s+(?:an?\s+)?(?:on-?\s?site|in[- ]office|in[- ]person)\b", re.I)),
]


def work_model_from_text(text: str | None) -> str | None:
    """'hybrid', 'remote' or 'on-site' from phrases like '3 days a week in office'; None if nothing found."""
    if not text:
        return None
    for rx in _DAYS:
        if m := rx.search(text):
            days = max(int(_WORDS.get(g.lower(), g)) for g in m.groups() if g)
            return "on-site" if days >= 5 else "hybrid"
    if _DAYS_PLAIN.search(text):
        return "hybrid"
    for model, rx in _DESC:
        if rx.search(text):
            return model
    return None


def detect_work_model(title, location, description=None, is_remote=None, hint=None) -> str:
    """hint: a source's own field (e.g. 'Hybrid', 'on-site', 'OnSite', 'remote')."""
    if hint:
        h = re.sub(r"[^a-z]", "", str(hint).lower())
        if h in ("remote", "hybrid", "onsite"):
            return "on-site" if h == "onsite" else h
    head = f"{title or ''} {location or ''}"
    if re.search(r"\bhybrid\b", head, re.I):
        return "hybrid"
    from_desc = work_model_from_text(description)
    if is_remote or re.search(r"\bremote\b|work from home|\bwfh\b", head, re.I):
        # a "remote" search can still return hybrid jobs; trust days-in-office wording over the label
        return from_desc if from_desc == "hybrid" else "remote"
    return from_desc or "unknown"


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
