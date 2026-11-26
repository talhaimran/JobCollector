"""Update to the latest GitHub release: check for one, and install it in place.
Design: docs/superpowers/specs/2026-11-25-self-update-design.md"""
import hashlib
import json
import logging
import re
from fnmatch import fnmatch
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import requests

from . import __version__, config, db

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/repos/talhaimran/JobCollector/releases/latest"
CHECK_EVERY = timedelta(hours=24)


def parse_version(s: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", str(s or "").strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def latest() -> dict | None:
    """The latest release on GitHub, or None if it can't be had (offline, rate-limited, odd answer)."""
    try:
        r = requests.get(API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "JobCollector"},
                         timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        tag = data["tag_name"]
        if not parse_version(tag):
            return None
        return {"version": tag.lstrip("v"), "notes_url": data["html_url"], "zip_url": data["zipball_url"]}
    except Exception as e:
        log.info("Update check failed: %s", e)
        return None


def _cache_path():
    return config.DATA_DIR / "update-check.json"  # DATA_DIR moves with the data folder, so read it at call time


def status(force: bool = False) -> dict:
    """Installed vs latest version. Asks GitHub at most once a day unless force."""
    rel, path = None, _cache_path()
    try:
        saved = json.loads(path.read_text())
        fresh = datetime.now(timezone.utc) - datetime.fromisoformat(saved["checked"]) < CHECK_EVERY
    except Exception:
        saved, fresh = None, False
    if fresh and not force:
        rel = saved["latest"]
    else:
        rel = latest()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"checked": datetime.now(timezone.utc).isoformat(), "latest": rel}))
        except OSError as e:
            log.info("Couldn't save the update check: %s", e)
    newer = bool(rel) and (parse_version(rel["version"]) or ()) > parse_version(__version__)
    return {"current": __version__, "latest": rel and rel["version"], "notes_url": rel and rel["notes_url"],
            "zip_url": rel and rel["zip_url"], "available": newer}


class UpdateError(Exception):
    """An update that couldn't be done; the message is for people."""


REQUIRED = ("collector/__init__.py", "requirements.txt", "Launch JobCollector Mac.command", "Launch JobCollector Windows.bat")


def _version_in(init_text: str) -> str | None:
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', init_text, re.M)
    return m and m.group(1)


def unpack(zip_path, dest, version: str):
    """Extract a release ZIP into dest and return its top folder, after checking it is what it claims to be.
    Anything wrong: dest is removed and UpdateError raised, before a single file is written outside dest."""
    dest = Path(dest)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            tops = {n.split("/", 1)[0] for n in names}
            for n in names:
                p = PurePosixPath(n)
                if p.is_absolute() or ".." in p.parts or "\\" in n or ":" in n:
                    raise UpdateError(f"The download has an unsafe path in it ({n}), so it wasn't used.")
            if len(tops) != 1:
                raise UpdateError("The download isn't laid out like a Job Collector release, so it wasn't used.")
            top = tops.pop()
            missing = [f for f in REQUIRED if f"{top}/{f}" not in names]
            if missing:
                raise UpdateError(f"The download is missing {', '.join(missing)}, so it wasn't used.")
            found = _version_in(zf.read(f"{top}/collector/__init__.py").decode("utf-8", "replace"))
            if found != version:
                raise UpdateError(f"The download says it is version {found or 'unknown'}, not {version}, so it wasn't used.")
            zf.extractall(dest)
    except zipfile.BadZipFile:
        shutil.rmtree(dest, ignore_errors=True)
        raise UpdateError("The download isn't a valid ZIP file, so it wasn't used. Try again later.")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return dest / top


class Busy(Exception):
    """A job check holds the run lock."""


# Never touched by an update, even if a release shipped one: the person's settings, packages, data and git checkout.
KEEP = frozenset({".env", ".venv", "data", "logs", "candidate-profile.md", ".git", ".update-backup", ".update-new",
                  ".update-download.zip"})
_ASK = object()


def _kept(name: str) -> bool:
    return name in KEEP or fnmatch(name, "*-resume.md")


def _download(url: str, to: Path):
    with requests.get(url, headers={"User-Agent": "JobCollector"}, timeout=config.HTTP_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        with open(to, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                f.write(chunk)


def _pip(root: Path):
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        "-r", str(root / "requirements.txt")], cwd=root, capture_output=True, text=True)
    if r.returncode:
        last = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise UpdateError(f"installing the new packages failed ({last})")


def _remove(p: Path):
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def install(root: Path = config.ROOT, release=_ASK) -> str:
    """Install the latest release (or `release`) into the app folder `root`. Returns a message for people.
    Raises Busy while a job check runs, UpdateError for anything else; on error the app stays as it was."""
    root = Path(root)
    if (root / ".git").exists():
        raise UpdateError("This folder is a git checkout: update it with git pull instead.")
    if release is _ASK:
        s = status(force=True)
        release = s["latest"] and {"version": s["latest"], "notes_url": s["notes_url"], "zip_url": s["zip_url"]}
    if not release:
        raise UpdateError("Couldn't reach GitHub to find the latest release. Check your internet connection and try again.")
    version = release["version"]
    if (parse_version(version) or ()) <= parse_version(__version__):
        return f"Already up to date ({__version__})."
    with db.run_lock(config.LOCK_PATH) as got:
        if not got:
            raise Busy("A job check is running; try again when it finishes.")
        new, zip_path = root / ".update-new", root / ".update-download.zip"
        _remove(new)
        try:
            try:
                _download(release["zip_url"], zip_path)
            except Exception as e:
                raise UpdateError(f"Couldn't download version {version}: {e}") from e
            _swap(root, unpack(zip_path, new, version))
        finally:
            _remove(new)
            _remove(zip_path)
    return f"Updated to {version}. Close the Job Collector window and double-click the launcher again to finish."


def _swap(root: Path, top: Path):
    backups = root / ".update-backup"
    backup = backups / __version__
    old_req = (root / "requirements.txt").read_bytes() if (root / "requirements.txt").exists() else None
    _remove(backup)
    done = []  # ("backup" | "new", name), in order, so a failure can be undone
    try:
        for src in sorted(top.iterdir()):
            name, cur = src.name, root / src.name
            if _kept(name):
                continue
            if src.is_file() and cur.is_file() and src.read_bytes() == cur.read_bytes():
                continue  # unchanged: leave it, so a running launcher isn't swapped under its window
            if cur.exists() or cur.is_symlink():
                backup.mkdir(parents=True, exist_ok=True)
                shutil.move(cur, backup / name)
                done.append(("backup", name))
            shutil.move(src, cur)
            done.append(("new", name))
        req = (root / "requirements.txt").read_bytes()
        if req != old_req:
            _pip(root)  # scheduled checks run the new code without the launcher, so they need the packages now
            if (root / ".venv").is_dir():
                (root / ".venv" / "requirements.sha").write_text(hashlib.sha256(req).hexdigest() + "\n")
    except Exception as e:
        stuck = []
        for kind, name in reversed(done):
            try:
                if kind == "new":
                    _remove(root / name)
                else:
                    shutil.move(backup / name, root / name)
            except Exception:
                log.exception("Couldn't undo the update for %s", name)
                stuck.append(name)
        note = f" Some files couldn't be put back ({', '.join(stuck)}); copies are in {backup}." if stuck else ""
        raise UpdateError(f"The update didn't finish, so nothing changed: {e}.{note}") from e
    for old in backups.iterdir() if backups.is_dir() else ():
        if old.name != __version__:
            _remove(old)
