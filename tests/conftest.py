import pytest

from collector import config


@pytest.fixture(autouse=True)
def default_scoring(monkeypatch):
    """Tests use the built-in scoring values, not whatever the local .env says."""
    d = config.SCORE_DEFAULTS
    monkeypatch.setattr(config, "SCORE_WEIGHTS", {"keywords": d["SCORE_WEIGHT_KEYWORDS"], "title": 1 - d["SCORE_WEIGHT_KEYWORDS"]})
    for k in ("KEYWORD_FULL_MARKS", "TITLE_EXACT_SCORE", "TITLE_CONTAINED_SCORE"):
        monkeypatch.setattr(config, k, d[k])
