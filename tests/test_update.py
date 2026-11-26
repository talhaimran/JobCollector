"""Updating to the latest GitHub release: the check, the ZIP check, the install and the command line."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from collector import __version__, config, update

NOTES, ZIP = "https://github.com/x/releases/v0.3.0", "https://api.github.com/x/zipball/v0.3.0"


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body, self._text = status, body, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._text is not None:
            return json.loads(self._text)
        return self._body


def fake_get(resp):
    def get(url, **kw):
        if isinstance(resp, Exception):
            raise resp
        return resp
    return get


def test_parse_version():
    assert update.parse_version("v0.10.0") > update.parse_version("0.9.1")
    assert update.parse_version("v1.2") == (1, 2)
    assert update.parse_version("nightly") is None
    assert update.parse_version(__version__) == (0, 2, 0)


def test_latest_reads_github(monkeypatch):
    monkeypatch.setattr(update.requests, "get", fake_get(Resp(body={"tag_name": "v0.3.0", "html_url": NOTES, "zipball_url": ZIP})))
    assert update.latest() == {"version": "0.3.0", "notes_url": NOTES, "zip_url": ZIP}


@pytest.mark.parametrize("fake", [
    requests.ConnectionError("offline"),
    Resp(status=403, body={"message": "rate limited"}),
    Resp(text="not json"),
    Resp(body={"tag_name": "nightly", "html_url": NOTES, "zipball_url": ZIP}),
    Resp(body=["not", "a", "dict"]),
])
def test_latest_is_none_on_any_problem(monkeypatch, fake):
    monkeypatch.setattr(update.requests, "get", fake_get(fake))
    assert update.latest() is None


def test_status_caches_for_a_day(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    calls = []
    answer = {"version": "0.3.0", "notes_url": NOTES, "zip_url": ZIP}
    monkeypatch.setattr(update, "latest", lambda: calls.append(1) or answer)

    s = update.status()
    assert s["available"] and s["current"] == __version__ and s["latest"] == "0.3.0" and s["notes_url"] == NOTES
    update.status()
    assert len(calls) == 1  # second call used the cache

    cache = tmp_path / "data" / "update-check.json"
    saved = json.loads(cache.read_text())
    saved["checked"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    cache.write_text(json.dumps(saved))
    update.status()
    assert len(calls) == 2  # a day old: asked again

    update.status(force=True)
    assert len(calls) == 3

    cache.write_text("garbage")
    update.status()
    assert len(calls) == 4  # unreadable cache counts as expired


@pytest.mark.parametrize("answer", [None, {"version": __version__, "notes_url": NOTES, "zip_url": ZIP},
                                    {"version": "0.1.0", "notes_url": NOTES, "zip_url": ZIP}])
def test_status_not_available(tmp_path, monkeypatch, answer):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(update, "latest", lambda: answer)
    assert update.status(force=True)["available"] is False
