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


# --- the release ZIP ---------------------------------------------------------------------------------------------

TOP = "talhaimran-JobCollector-abc123/"
LAUNCHERS = ("Launch JobCollector Mac.command", "Launch JobCollector Windows.bat")


def release_files(version, **over):
    files = {"collector/__init__.py": f'__version__ = "{version}"\n'.encode(), "collector/update.py": b"# new\n",
             "requirements.txt": b"requests\n", LAUNCHERS[0]: b"#!/bin/bash\n", LAUNCHERS[1]: b"@echo off\r\n",
             "README.md": b"new readme\n"}
    files.update(over)
    return {k: v for k, v in files.items() if v is not None}


def make_release(tmp_path, version, extra=None, name="release.zip", **over):
    """A ZIP shaped like GitHub's zipball. extra keys starting with / .. or second-top/ go in raw, not under the top folder."""
    import zipfile
    z = tmp_path / name
    with zipfile.ZipFile(z, "w") as zf:
        for k, v in release_files(version, **over).items():
            zf.writestr(TOP + k, v)
        for k, v in (extra or {}).items():
            raw = k.startswith(("/", "..", "second-top/"))
            zf.writestr(k if raw else TOP + k, v)
    return z


def test_unpack_good_release(tmp_path):
    top = update.unpack(make_release(tmp_path, "0.3.0"), tmp_path / "new", "0.3.0")
    assert (top / "collector" / "__init__.py").exists() and (top / LAUNCHERS[1]).exists()


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "second-top/x.txt", TOP + "a/../../evil.txt"])
def test_unpack_refuses_unsafe_layout(tmp_path, bad):
    z = make_release(tmp_path, "0.3.0", {bad: b"x"})
    with pytest.raises(update.UpdateError):
        update.unpack(z, tmp_path / "new", "0.3.0")
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "new").exists()


@pytest.mark.parametrize("over", [{LAUNCHERS[1]: None}, {"requirements.txt": None}, {"collector/__init__.py": None}])
def test_unpack_refuses_a_release_missing_a_file(tmp_path, over):
    with pytest.raises(update.UpdateError):
        update.unpack(make_release(tmp_path, "0.3.0", **over), tmp_path / "new", "0.3.0")
    assert not (tmp_path / "new").exists()


def test_unpack_refuses_wrong_version(tmp_path):
    with pytest.raises(update.UpdateError, match="0.4.0"):
        update.unpack(make_release(tmp_path, "0.3.0"), tmp_path / "new", "0.4.0")


def test_unpack_refuses_a_file_that_is_not_a_zip(tmp_path):
    z = tmp_path / "x.zip"
    z.write_text("<html>rate limited</html>")
    with pytest.raises(update.UpdateError):
        update.unpack(z, tmp_path / "new", "0.3.0")
