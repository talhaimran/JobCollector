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


# --- installing --------------------------------------------------------------------------------------------------

import hashlib  # noqa: E402
import shutil  # noqa: E402

from collector import db  # noqa: E402

PERSON = {".env": b"SMTP_PASSWORD=secret\n", "data/jobs.db": b"my jobs", "candidate-profile.md": b"# me\n",
          "me-resume.md": b"resume", "logs/collector.log": b"log", ".venv/requirements.sha": b"oldhash\n"}


def rel(version="0.3.0"):
    return {"version": version, "notes_url": NOTES, "zip_url": ZIP}


@pytest.fixture
def pips():
    return []


@pytest.fixture
def app(tmp_path, monkeypatch, pips):
    """A fake install at 0.2.0 with the person's files; downloads come from tmp_path/release.zip; pip is recorded."""
    root = tmp_path / "app"
    for k, v in {**release_files(__version__, **{"requirements.txt": b"requests\n", "collector/old_module.py": b"old"}),
                 **PERSON}.items():
        (root / k).parent.mkdir(parents=True, exist_ok=True)
        (root / k).write_bytes(v)
    monkeypatch.setattr(config, "LOCK_PATH", tmp_path / "lockdir" / "collector.lock")
    monkeypatch.setattr(update, "_download", lambda url, to: shutil.copy(tmp_path / "release.zip", to))
    monkeypatch.setattr(update, "_pip", lambda r: pips.append(r))
    return root


def person_files_untouched(root):
    for k, v in PERSON.items():
        if k != ".venv/requirements.sha":
            assert (root / k).read_bytes() == v, k


def test_install_replaces_app_files_and_keeps_the_persons(app, tmp_path):
    make_release(tmp_path, "0.3.0")
    msg = update.install(app, rel())
    assert "Updated to 0.3.0" in msg and "launcher" in msg
    assert '"0.3.0"' in (app / "collector/__init__.py").read_text()
    assert (app / "collector/update.py").read_bytes() == b"# new\n"
    assert not (app / "collector/old_module.py").exists()  # gone with the old folder
    assert (app / ".update-backup/0.2.0/collector/old_module.py").exists()
    assert (app / "README.md").read_bytes() == b"new readme\n"
    person_files_untouched(app)
    assert not (app / ".update-new").exists() and not list(app.glob("*.zip"))


def test_release_shipping_person_files_does_not_overwrite_them(app, tmp_path):
    make_release(tmp_path, "0.3.0", {".env": b"X", "data/jobs.db": b"X", "candidate-profile.md": b"X",
                                     "someone-resume.md": b"X", ".venv/x": b"X", "logs/x": b"X"})
    update.install(app, rel())
    person_files_untouched(app)
    assert not (app / "someone-resume.md").exists() and not (app / ".venv/x").exists()


def test_unchanged_launcher_is_not_moved(app, tmp_path):
    make_release(tmp_path, "0.3.0")
    before = (app / LAUNCHERS[1]).stat().st_ino
    update.install(app, rel())
    assert (app / LAUNCHERS[1]).stat().st_ino == before
    assert not (app / ".update-backup/0.2.0" / LAUNCHERS[1]).exists()


def test_changed_requirements_runs_pip_and_writes_hash(app, tmp_path, pips, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(app / ".venv"))
    make_release(tmp_path, "0.3.0", **{"requirements.txt": b"requests\nnewpkg\n"})
    update.install(app, rel())
    assert pips == [app]
    want = hashlib.sha256(b"requests\nnewpkg\n").hexdigest()
    assert (app / ".venv/requirements.sha").read_text().strip() == want


def test_same_requirements_skips_pip(app, tmp_path, pips):
    make_release(tmp_path, "0.3.0")
    update.install(app, rel())
    assert pips == [] and (app / ".venv/requirements.sha").read_bytes() == b"oldhash\n"


def _assert_still_old(app):
    assert f'"{__version__}"' in (app / "collector/__init__.py").read_text()
    assert (app / "collector/old_module.py").exists()
    assert (app / "requirements.txt").read_bytes() == b"requests\n"
    person_files_untouched(app)
    assert not (app / ".update-new").exists() and not list(app.glob("*.zip"))


def test_pip_failure_rolls_everything_back(app, tmp_path, monkeypatch):
    make_release(tmp_path, "0.3.0", **{"requirements.txt": b"requests\nnewpkg\n", "README.md": b"r3\n"})
    (app / "README.md").write_bytes(b"r2\n")

    def boom(root):
        raise update.UpdateError("pip failed")
    monkeypatch.setattr(update, "_pip", boom)
    with pytest.raises(update.UpdateError, match="pip failed"):
        update.install(app, rel())
    _assert_still_old(app)
    assert (app / "README.md").read_bytes() == b"r2\n"
    assert (app / ".venv/requirements.sha").read_bytes() == b"oldhash\n"


def test_refuses_git_clone(app, tmp_path, monkeypatch):
    (app / ".git").mkdir()
    monkeypatch.setattr(update, "_download", lambda *a: pytest.fail("downloaded"))
    with pytest.raises(update.UpdateError, match="git pull"):
        update.install(app, rel())


def test_refuses_while_a_check_runs(app, monkeypatch):
    monkeypatch.setattr(update, "_download", lambda *a: pytest.fail("downloaded"))
    with db.run_lock(config.LOCK_PATH) as got:
        assert got
        with pytest.raises(update.Busy):
            update.install(app, rel())


@pytest.mark.parametrize("release", [rel(__version__), rel("0.1.0"), None])
def test_nothing_newer_downloads_nothing(app, monkeypatch, release):
    monkeypatch.setattr(update, "_download", lambda *a: pytest.fail("downloaded"))
    if release is None:
        with pytest.raises(update.UpdateError, match="GitHub"):
            update.install(app, release)
    else:
        assert "up to date" in update.install(app, release)


def test_install_without_release_asks_github(app, tmp_path, monkeypatch):
    make_release(tmp_path, "0.3.0")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(update, "latest", lambda: rel())
    assert "Updated to 0.3.0" in update.install(app)


def test_keeps_only_the_latest_backup(app, tmp_path):
    (app / ".update-backup/0.1.0").mkdir(parents=True)
    make_release(tmp_path, "0.3.0")
    update.install(app, rel())
    assert [p.name for p in (app / ".update-backup").iterdir()] == ["0.2.0"]


# --- command line ------------------------------------------------------------------------------------------------

from collector import cli  # noqa: E402


@pytest.mark.parametrize("latest,line", [("0.3.0", "Latest: 0.3.0 (update available)"),
                                         (__version__, f"Latest: {__version__} (up to date)"),
                                         (None, "Latest: unknown (couldn't reach GitHub)")])
def test_cli_check(monkeypatch, capsys, latest, line):
    seen = []
    monkeypatch.setattr(update, "status", lambda force=False: seen.append(force) or {
        "current": __version__, "latest": latest, "notes_url": None, "zip_url": None, "available": latest == "0.3.0"})
    monkeypatch.setattr(update, "install", lambda *a, **k: pytest.fail("installed"))
    cli.main(["update", "--check"])
    out = capsys.readouterr().out
    assert f"Installed: {__version__}" in out and line in out and seen == [True]


def test_cli_update_prints_the_message(monkeypatch, capsys):
    monkeypatch.setattr(update, "install", lambda *a, **k: "Updated to 0.3.0. Close ...")
    cli.main(["update"])
    assert "Updated to 0.3.0" in capsys.readouterr().out


@pytest.mark.parametrize("err,code", [(update.Busy("A job check is running"), 75), (update.UpdateError("no zip"), 1)])
def test_cli_update_errors(monkeypatch, capsys, err, code):
    def boom(*a, **k):
        raise err
    monkeypatch.setattr(update, "install", boom)
    with pytest.raises(SystemExit) as e:
        cli.main(["update"])
    assert e.value.code == code and str(err) in capsys.readouterr().out


def test_docs_explain_releases_and_updating():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    readme, contributing = (root / "README.md").read_text(), (root / "CONTRIBUTING.md").read_text()
    assert "Releases" in readme and "collector update" in readme and "Code → Download ZIP" not in readme
    assert "__version__" in contributing


# --- final review fixes ------------------------------------------------------------------------------------------

import os  # noqa: E402
import stat  # noqa: E402
import sys  # noqa: E402


def test_swap_never_uses_copying_moves(app, tmp_path, monkeypatch):
    # shutil.move falls back to copy + delete, which can half-fail; every move must be a plain rename
    make_release(tmp_path, "0.3.0", **{"README.md": b"r3\n"})
    monkeypatch.setattr(update.shutil, "move", lambda *a: pytest.fail("shutil.move used"))
    update.install(app, rel())
    assert '"0.3.0"' in (app / "collector/__init__.py").read_text()


def test_rename_failure_midway_rolls_back(app, tmp_path, monkeypatch):
    make_release(tmp_path, "0.3.0", **{"README.md": b"r3\n"})
    (app / "README.md").write_bytes(b"r2\n")
    real, n = os.rename, []

    def flaky(src, dst):
        if ".update-new" in str(src):
            n.append(src)
            if len(n) == 2:
                raise PermissionError("file in use")
        return real(src, dst)
    monkeypatch.setattr(update.os, "rename", flaky)
    with pytest.raises(update.UpdateError, match="nothing changed"):
        update.install(app, rel())
    _assert_still_old(app)
    assert (app / "README.md").read_bytes() == b"r2\n"


def test_rollback_never_moves_into_an_existing_folder(app, tmp_path, monkeypatch):
    make_release(tmp_path, "0.3.0")
    real = os.rename

    def leave_new_in_place(src, dst):  # the new collector/ lands, then pip fails, then removing it fails
        return real(src, dst)
    monkeypatch.setattr(update.os, "rename", leave_new_in_place)
    monkeypatch.setattr(update, "_pip", lambda r: (_ for _ in ()).throw(update.UpdateError("pip failed")))
    monkeypatch.setattr(update, "_remove", lambda p: None if p.name == "collector" else update.shutil.rmtree(p, True))
    make_release(tmp_path, "0.3.0", **{"requirements.txt": b"changed\n"})
    with pytest.raises(update.UpdateError, match="couldn't be put back"):
        update.install(app, rel())
    assert not (app / "collector/collector").exists()
    assert (app / ".update-backup/0.2.0/collector/old_module.py").exists()


def test_after_an_update_the_installed_version_is_read_from_disk(app, tmp_path, monkeypatch):
    make_release(tmp_path, "0.3.0")
    update.install(app, rel())
    assert "up to date (0.3.0)" in update.install(app, rel())  # the running code still says 0.2.0
    assert (app / ".update-backup/0.2.0/collector/old_module.py").exists()  # the way back survives
    monkeypatch.setattr(config, "ROOT", app)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(update, "latest", lambda: rel())
    s = update.status(force=True)
    assert s["current"] == "0.3.0" and not s["available"]


def test_mac_launcher_stays_executable(app, tmp_path):
    make_release(tmp_path, "0.3.0", **{LAUNCHERS[0]: b"#!/bin/bash\necho new\n"})
    update.install(app, rel())
    assert os.stat(app / LAUNCHERS[0]).st_mode & stat.S_IXUSR


def test_unpack_keeps_permissions_from_the_zip(tmp_path):
    import zipfile
    z = make_release(tmp_path, "0.3.0")
    with zipfile.ZipFile(z, "a") as zf:
        info = zipfile.ZipInfo(TOP + "tool.sh")
        info.external_attr = 0o755 << 16
        zf.writestr(info, b"#!/bin/sh\n")
    top = update.unpack(z, tmp_path / "new", "0.3.0")
    assert os.stat(top / "tool.sh").st_mode & 0o777 == 0o755


def test_a_retry_keeps_earlier_rescue_copies(app, tmp_path, monkeypatch):
    rescue = app / ".update-backup/0.2.0/collector/rescued.py"
    rescue.parent.mkdir(parents=True)
    rescue.write_text("only copy")
    make_release(tmp_path, "0.3.0", **{"requirements.txt": b"changed\n"})
    monkeypatch.setattr(update, "_pip", lambda r: (_ for _ in ()).throw(update.UpdateError("pip failed")))
    with pytest.raises(update.UpdateError):
        update.install(app, rel())
    assert rescue.read_text() == "only copy"


def test_cleanup_trouble_after_success_is_not_a_failure(app, tmp_path, monkeypatch):
    (app / ".update-backup/0.1.0").mkdir(parents=True)
    make_release(tmp_path, "0.3.0")
    real = update._remove

    def stubborn(p):
        if p.name == "0.1.0":
            raise PermissionError("in use")
        return real(p)
    monkeypatch.setattr(update, "_remove", stubborn)
    assert "Updated to 0.3.0" in update.install(app, rel())


def test_hash_written_only_for_the_apps_own_python(app, tmp_path, monkeypatch):
    make_release(tmp_path, "0.3.0", **{"requirements.txt": b"requests\nnewpkg\n"})
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "system-python"))
    update.install(app, rel())
    assert (app / ".venv/requirements.sha").read_bytes() == b"oldhash\n"  # launcher will still install into .venv
