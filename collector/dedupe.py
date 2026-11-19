"""Fingerprints for cross-source de-duplication."""
import re

_SUFFIXES = r"\b(inc|incorporated|ltd|limited|llc|llp|corp|corporation|co|company|plc|gmbh|ulc|lp)\b"


def _clean(s: str | None) -> str:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def norm_company(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(_SUFFIXES, " ", _clean(s))).strip()


def norm_location(s: str | None) -> str:
    # sources format "Kitchener, ON, Canada" vs "Kitchener, Ontario" differently: keep the city
    return _clean((s or "").split(",")[0])


def fingerprint(company, title, location) -> str:
    return "|".join([norm_company(company), _clean(title), norm_location(location)])
