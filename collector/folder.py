"""The data folder (JOB_FOLDER): the profile, data/jobs.db and logs/ live together so one folder can be
backed up, synced or moved. .env (with the Gmail app password) stays in the app folder."""
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)


def _copy_db(src, dst):
    """A consistent copy even if the source is open (sqlite's backup API, not a file copy)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)


def migrate_legacy() -> str | None:
    """Once: the database moved from <app>/data into the data folder. Copy it there; the original stays."""
    if config.DB_PATH.exists() or not config.LEGACY_DB.exists() or config.DB_PATH == config.LEGACY_DB:
        return None
    _copy_db(config.LEGACY_DB, config.DB_PATH)
    msg = f"Database copied from {config.LEGACY_DB} to {config.DB_PATH} (the original was left in place)."
    log.info(msg)
    return msg


def has_data(path) -> bool:
    return (path / "candidate-profile.md").exists() or (path / "data" / "jobs.db").exists()


def describe(path: str) -> dict:
    """What the page needs to know before switching: is it usable, and does it already hold data?"""
    p = Path(path).expanduser()
    problem = None
    if not p.is_absolute():
        problem = "Choose a full folder path, e.g. /Users/you/Documents/Job Collector."
    elif p.exists() and not p.is_dir():
        problem = "That's a file, not a folder."
    elif not p.exists() and not p.parent.is_dir():
        problem = f"The folder it would go in ({p.parent}) doesn't exist."
    elif not os.access(p if p.exists() else p.parent, os.W_OK):
        problem = "Job Collector can't write to that folder. Choose one in your own Documents."
    return {"path": str(p), "has_data": problem is None and p.is_dir() and has_data(p), "problem": problem}


def switch(path: str, mode: str) -> str:
    """mode: 'use' the data already in the folder, 'copy' the current data there, or start 'fresh' there.
    Never deletes or overwrites anything; refused while a run is going."""
    from . import webui  # webui imports this module
    if mode not in ("use", "copy", "fresh"):
        raise ValueError(f"unknown mode: {mode}")
    d = describe(path)
    if d["problem"]:
        raise ValueError(d["problem"])
    target, old = Path(d["path"]), config.JOB_FOLDER
    if target.resolve() == old.resolve():
        return f"Already using {target}."
    if d["has_data"] and mode != "use":
        raise ValueError("That folder already has Job Collector data. Choose 'Use the data already there'.")
    with db.run_lock(config.LOCK_PATH) as got:
        if not got:
            raise ValueError("A check is running; try again in a minute.")
        target.mkdir(exist_ok=True)
        if mode == "copy":
            if config.PROFILE_PATH.exists():
                shutil.copy2(config.PROFILE_PATH, target / "candidate-profile.md")
            if config.DB_PATH.exists():
                _copy_db(config.DB_PATH, target / "data" / "jobs.db")
    webui.update_env(webui.ENV_PATH, {"JOB_FOLDER": str(target)})  # scheduled runs read it from .env
    config.use_folder(target)
    log.info("data folder: %s -> %s (%s)", old, target, mode)
    return {"use": f"Now using {target} and the data already there.",
            "copy": f"Now using {target}. Your data was copied there; {old} was left as it was.",
            "fresh": f"Now using {target}, starting fresh. Your old data is still in {old}."}[mode]


def choose_native() -> str | None:
    """Open the operating system's folder picker (the page can't learn real paths itself).
    Returns None when cancelled; RuntimeError when no picker is available."""
    prompt = "Choose where Job Collector keeps your data"
    if os.name == "nt":
        cmd = ["powershell", "-NoProfile", "-STA", "-Command",
               "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; "
               f"$d.Description = '{prompt}'; if ($d.ShowDialog() -eq 'OK') {{ $d.SelectedPath }}"]
    else:
        cmd = ["osascript", "-e", f'POSIX path of (choose folder with prompt "{prompt}")']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"No folder picker available ({type(e).__name__})") from e
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return None  # cancelled
    return out.rstrip("/\\") or out
