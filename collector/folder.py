"""The data folder (JOB_FOLDER): the profile, data/jobs.db and logs/ live together so one folder can be
backed up, synced or moved. .env (with the Gmail app password) stays in the app folder."""
import logging
import sqlite3

from . import config

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
