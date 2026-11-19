"""Hard filters. check() returns (passed, first failure reason)."""
import re

from .dedupe import norm_company
from .normalize import phrase_in
from .settings import Settings

CA_PROVINCES = {"on", "ontario", "bc", "british columbia", "ab", "alberta", "qc", "quebec", "québec",
                "mb", "manitoba", "sk", "saskatchewan", "ns", "nova scotia", "nb", "new brunswick",
                "nl", "newfoundland and labrador", "pe", "prince edward island", "yt", "yukon",
                "nt", "northwest territories", "nu", "nunavut"}
US_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id", "il", "in", "ia",
             "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt", "ne", "nv", "nh", "nj",
             "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt",
             "va", "wa", "wv", "wi", "wy", "dc", "california", "new york", "texas", "florida",
             "washington", "illinois", "massachusetts", "colorado", "georgia", "virginia"}
COUNTRIES = {"canada": "canada", "united states": "united states", "usa": "united states",
             "us": "united states", "u s": "united states", "america": "united states",
             "united kingdom": "united kingdom", "uk": "united kingdom", "england": "united kingdom",
             "ireland": "ireland", "india": "india", "germany": "germany", "france": "france",
             "netherlands": "netherlands", "spain": "spain", "poland": "poland", "mexico": "mexico",
             "brazil": "brazil", "australia": "australia", "philippines": "philippines",
             "singapore": "singapore", "emea": "emea", "europe": "europe", "latam": "latam", "apac": "apac"}


def _tokens(location: str) -> list[str]:
    return [re.sub(r"[^\w\s]", " ", t).strip().lower() for t in re.split(r"[,(/|\-–]", location or "")]


def detect_country(location: str | None) -> str | None:
    """Best-effort country from a location string. None = no country restriction found."""
    # shortcut: token lists, not a geocoder; add names to COUNTRIES/US_STATES if something slips through
    toks = [re.sub(r"\s+", " ", t) for t in _tokens(location) if t]
    for t in toks:
        if t in COUNTRIES:
            return COUNTRIES[t]
    if any(t in CA_PROVINCES for t in toks):
        return "canada"
    if any(t in US_STATES for t in toks):
        return "united states"
    return None


def location_matches(job_location: str | None, wanted: list[str]) -> bool:
    """'Kitchener ON' matches 'Kitchener, ON, Canada' but not 'Waterloo, IA'."""
    job_country = detect_country(job_location)
    for w in wanted:
        words = w.replace(",", " ").split()
        region = words[-1] if len(words) > 1 and words[-1].lower() in (CA_PROVINCES | US_STATES) else None
        city = " ".join(words[:-1] if region else words)
        if not phrase_in(city, job_location):
            continue
        want_country = detect_country(region) if region else None
        if job_country is None or want_country is None or job_country == want_country:
            return True
    return False


def _norm_model(m: str) -> str:
    m = re.sub(r"[^a-z]", "", m.lower())
    return "on-site" if m in ("onsite", "inoffice", "office") else m


HOURS_PER = {"hourly": 2080, "daily": 260, "weekly": 52, "monthly": 12, "yearly": 1}


def yearly_salary(amount, interval) -> float | None:
    if amount is None:
        return None
    return float(amount) * HOURS_PER.get(interval or "yearly", 1)


def check(job: dict, s: Settings) -> tuple[bool, str | None]:
    title, desc = job.get("title") or "", job.get("description") or ""

    if s.excluded_companies:
        c = norm_company(job.get("company"))
        if c and any(norm_company(x) == c for x in s.excluded_companies):
            return False, f"excluded company: {job.get('company')}"

    for t in s.exclude_titles:
        if phrase_in(t, title):
            return False, f"excluded title: {t}"

    if s.target_titles and not any(phrase_in(t, title) for t in s.target_titles):
        return False, "title not in target titles"

    model, loc = job.get("work_model") or "unknown", job.get("location")
    if s.work_model and model != "unknown" and model not in {_norm_model(m) for m in s.work_model}:
        return False, f"work model {model} not wanted"
    if model == "remote":
        country = detect_country(loc)
        if s.remote_in and country and country not in {r.lower() for r in s.remote_in}:
            return False, f"remote restricted to {country}"
    elif s.locations and not location_matches(loc, s.locations):
        return False, f"location not matched ({model}): {loc}"

    if s.employment_type and job.get("employment_type"):
        wanted = {_norm_model(e) for e in s.employment_type}  # strips to 'fulltime' etc.
        have = {_norm_model(e) for e in job["employment_type"].split(",")}
        if not wanted & have:
            return False, f"employment type: {job['employment_type']}"

    if s.salary_floor is not None:
        top = job.get("salary_max") if job.get("salary_max") is not None else job.get("salary_min")
        cur = (job.get("salary_currency") or "CAD").upper()
        yearly = yearly_salary(top, job.get("salary_interval"))
        # shortcut: non-CAD salaries are not converted, they pass and get flagged in the email
        if yearly is not None and cur == "CAD" and yearly < s.salary_floor:
            return False, f"salary below floor: {yearly:,.0f} CAD/year"

    for d in s.dealbreakers:
        if phrase_in(d, title) or phrase_in(d, desc):
            return False, f"dealbreaker: {d}"

    return True, None
