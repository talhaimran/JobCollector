"""Update to the latest GitHub release: check for one, and install it in place.
Design: docs/superpowers/specs/2026-11-25-self-update-design.md"""
import json
import logging
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import requests

from . import __version__, config

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
