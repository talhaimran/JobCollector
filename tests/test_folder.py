"""The data folder: where the profile, database and logs live, and switching it (no network)."""
import pytest

from collector import config, db


@pytest.fixture(autouse=True)
def restore_paths():
    yield
    config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_use_folder_repoints_paths(tmp_path):
    config.use_folder(tmp_path)
    assert config.JOB_FOLDER == tmp_path
    assert config.PROFILE_PATH == tmp_path / "candidate-profile.md"
    assert config.DB_PATH == tmp_path / "data" / "jobs.db"
    assert config.LOCK_PATH == tmp_path / "data" / "collector.lock"
    assert config.LOGS_DIR == tmp_path / "logs"


def test_legacy_db_copied_once(tmp_path, monkeypatch):
    from collector import folder
    legacy = tmp_path / "app" / "data" / "jobs.db"
    old = db.connect(legacy)
    old.execute("INSERT INTO kv(key, value) VALUES ('marker', 'kept')")
    old.commit()
    old.close()
    monkeypatch.setattr(config, "LEGACY_DB", legacy)
    config.use_folder(tmp_path / "mine")
    assert "copied" in folder.migrate_legacy()
    assert db.kv_get(db.connect(config.DB_PATH), "marker") == "kept"
    assert legacy.exists()  # the original stays
    assert folder.migrate_legacy() is None  # only once


def test_run_logs_into_the_data_folder(tmp_path, monkeypatch, caplog):
    import logging
    from collector import cli
    caplog.set_level(logging.INFO)  # pytest's handlers make basicConfig a no-op, so set the level here
    config.use_folder(tmp_path)
    monkeypatch.setattr(cli.pipeline, "run", lambda dry_run: logging.getLogger("collector.x").info("hello from a run"))
    try:
        cli.main(["run", "--dry-run"])
    finally:
        for h in logging.getLogger().handlers[:]:
            if isinstance(h, logging.FileHandler):
                logging.getLogger().removeHandler(h)
                h.close()
    assert "hello from a run" in (tmp_path / "logs" / "collector.log").read_text()
