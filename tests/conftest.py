import pytest

from collector import config


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any test that forgets to fake a source fails instead of calling the real site."""
    def refuse(*a, **kw):
        raise AssertionError(f"network call in a test: {a[1] if len(a) > 1 else a}")
    monkeypatch.setattr("requests.Session.request", refuse)


@pytest.fixture(autouse=True)
def default_scoring(monkeypatch):
    """Tests use the built-in scoring values, not whatever the local .env says."""
    d = config.SCORE_DEFAULTS
    monkeypatch.setattr(config, "SCORE_WEIGHTS", {"keywords": d["SCORE_WEIGHT_KEYWORDS"], "title": 1 - d["SCORE_WEIGHT_KEYWORDS"]})
    for k in ("KEYWORD_FULL_MARKS", "TITLE_EXACT_SCORE", "TITLE_CONTAINED_SCORE"):
        monkeypatch.setattr(config, k, d[k])
    monkeypatch.setattr(config, "SEND_PARTIAL_SCORES", False)  # also off by default, whatever .env says
    monkeypatch.setattr(config, "SEND_WHEN_EMPTY", False)
