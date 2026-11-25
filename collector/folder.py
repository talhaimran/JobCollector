"""The data folder (JOB_FOLDER): the profile, data/jobs.db and logs/ live together so one folder can be
backed up, synced or moved. .env (with the Gmail app password) stays in the app folder."""
import logging
from contextlib import closing
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)
ON_WINDOWS = os.name == "nt"


def _copy_db(src, dst):
    """A consistent copy even if the source is open (sqlite's backup API, not a file copy)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(src)) as a, closing(sqlite3.connect(dst)) as b:
        a.backup(b)


def _moved_marker():
    return config.LEGACY_DB.with_name("MOVED.txt")


def migrate_legacy() -> str | None:
    """Once: the database moved from <app>/data into the data folder. Copy it there; the original stays.
    A marker next to the old database makes it once for real: a deleted or not-yet-synced jobs.db later
    must not be refilled with old jobs."""
    if (config.DB_PATH.exists() or not config.LEGACY_DB.exists() or config.DB_PATH == config.LEGACY_DB
            or _moved_marker().exists()):
        return None
    _copy_db(config.LEGACY_DB, config.DB_PATH)
    _moved_marker().write_text(f"Copied to {config.DB_PATH}; Job Collector no longer reads this folder's jobs.db.\n")
    msg = f"Database copied from {config.LEGACY_DB} to {config.DB_PATH} (the original was left in place)."
    log.info(msg)
    return msg


# Everything Job Collector writes in a data folder. A symlink here could send a copy or a new database
# to some other place, so a folder holding one is refused (a dangling one counts too: .exists() misses it).
OWN_NAMES = ("candidate-profile.md", "data", "data/jobs.db", "data/jobs.db-journal", "data/jobs.db-wal",
             "data/collector.lock", "logs", "logs/collector.log")


def _symlink_in(p: Path) -> str | None:
    return next((n for n in OWN_NAMES if (p / n).is_symlink()), None)


def has_data(path) -> bool:
    return (path / "candidate-profile.md").exists() or (path / "data" / "jobs.db").exists()


def describe(path: str) -> dict:
    """What the page needs to know before switching: is it usable, and does it already hold data?"""
    p = Path(path).expanduser()
    problem = None
    if any(ord(c) < 32 for c in path):
        problem = "That path has a line break or other control character in it."
    elif not p.is_absolute():
        problem = "Choose a full folder path, e.g. /Users/you/Documents/Job Collector."
    elif p.exists() and not p.is_dir():
        problem = "That's a file, not a folder."
    elif not p.exists() and not p.parent.is_dir():
        problem = f"The folder it would go in ({p.parent}) doesn't exist."
    elif not os.access(p if p.exists() else p.parent, os.W_OK):
        problem = "Job Collector can't write to that folder. Choose one in your own Documents."
    elif p.is_symlink() or (p.is_dir() and _symlink_in(p)):
        problem = (f"That folder has a shortcut (symlink) in it ({_symlink_in(p) if p.is_dir() else p.name}). "
                   "Choose a folder without one.")
    return {"path": str(p), "has_data": problem is None and p.is_dir() and has_data(p), "problem": problem}


def switch(path: str, mode: str) -> str:
    """mode: 'use' the data already in the folder, 'copy' the current data there, or start 'fresh' there.
    Never deletes or overwrites anything; refused while a run is going."""
    from . import webui  # webui imports this module
    if mode not in ("use", "copy", "fresh"):
        raise ValueError(f"unknown mode: {mode}")

    def check():
        d = describe(path)
        if d["problem"]:
            raise ValueError(d["problem"])
        if d["has_data"] and mode != "use":
            raise ValueError("That folder already has Job Collector data. Choose 'Use the data already there'.")
        return Path(d["path"])

    target, old = check(), config.JOB_FOLDER
    if target.resolve() == old.resolve():
        return f"Already using {target}."
    # Held until the new folder is saved and in use: a scheduled run starting meanwhile skips its turn
    with db.run_lock(config.LOCK_PATH) as got:
        if not got:
            raise ValueError("A check is running; try again in a minute.")
        check()  # again, right before writing: the folder may have changed since the page asked
        target.mkdir(exist_ok=True)
        if mode == "copy":
            if config.PROFILE_PATH.exists():
                shutil.copy2(config.PROFILE_PATH, target / "candidate-profile.md")
            if config.DB_PATH.exists():
                _copy_db(config.DB_PATH, target / "data" / "jobs.db")
        # single-quoted for dotenv: a '#' would otherwise start a comment, and double quotes expand \n in C:\new
        quoted = "'" + str(target).replace("\\", "\\\\").replace("'", "\\'") + "'"
        webui.update_env(webui.ENV_PATH, {"JOB_FOLDER": quoted})  # scheduled runs read it from .env
        if config.LEGACY_DB.exists() and target / "data" / "jobs.db" != config.LEGACY_DB:
            _moved_marker().write_text(f"Data folder switched to {target}.\n")  # never seed it with old jobs
        config.use_folder(target)
    log.info("data folder: %s -> %s (%s)", old, target, mode)
    return {"use": f"Now using {target} and the data already there.",
            "copy": f"Now using {target}. Your data was copied there; {old} was left as it was.",
            "fresh": f"Now using {target}, starting fresh. Your old data is still in {old}."}[mode]


def choose_native() -> str | None:
    """Open the operating system's folder picker (the page can't learn real paths itself).
    Returns None when cancelled; RuntimeError when no picker is available."""
    prompt = "Choose where Job Collector keeps your data"
    if ON_WINDOWS:
        cmd = ["powershell", "-NoProfile", "-STA", "-Command",
               "[Console]::OutputEncoding = [Text.Encoding]::UTF8; Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
               f"$d.Description = '{prompt}'; if ($d.ShowDialog() -eq 'OK') {{ $d.SelectedPath }}"]
    else:
        cmd = ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"No folder picker available ({type(e).__name__})") from e
    out = r.stdout.strip()
    if r.returncode != 0 and "(-128)" not in r.stderr:  # -128: osascript's "User canceled"
        raise RuntimeError(f"Folder picker failed: {r.stderr.strip()[:200]}")
    if not out:
        return None  # cancelled (PowerShell prints nothing)
    return out.rstrip("/\\") or out
