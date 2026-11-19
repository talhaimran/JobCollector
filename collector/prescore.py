"""Keyword pre-score, 0-100. Defaults live in config.py; override in .env or the settings page."""
import re

from . import config
from .normalize import phrase_in
from .settings import Settings


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def params() -> dict:
    """Current scoring values, read at call time so tests and the settings preview can override them."""
    return {"SCORE_WEIGHT_KEYWORDS": config.SCORE_WEIGHTS["keywords"],
            "KEYWORD_FULL_MARKS": config.KEYWORD_FULL_MARKS,
            "TITLE_EXACT_SCORE": config.TITLE_EXACT_SCORE,
            "TITLE_CONTAINED_SCORE": config.TITLE_CONTAINED_SCORE}


def title_score(title: str, targets: list[str], p: dict | None = None) -> int:
    p = p or params()
    if any(_norm(title) == _norm(t) for t in targets):
        return p["TITLE_EXACT_SCORE"]
    if any(phrase_in(t, title) for t in targets):
        return p["TITLE_CONTAINED_SCORE"]
    return 0


def score(job: dict, s: Settings, p: dict | None = None) -> dict:
    p = p or params()
    title = job.get("title") or ""
    text = f"{title}\n{job.get('description') or ''}"
    matched = [k for k in s.must_have if phrase_in(k, text)]
    kw = min(100.0, len(matched) / p["KEYWORD_FULL_MARKS"] * 100)
    t = title_score(title, s.target_titles, p)
    wk = p["SCORE_WEIGHT_KEYWORDS"]
    # no must-have keywords configured -> title score is the whole score
    total = kw * wk + t * (1 - wk) if s.must_have else t
    return {"prescore_total": round(total, 1), "prescore_keywords": round(kw, 1),
            "prescore_title": t, "matched_keywords": matched}
