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


@pytest.fixture
def app(tmp_path, monkeypatch):
    """Current data folder with a profile and one saved value; .env in a temp app folder."""
    from collector import folder, webui
    env = tmp_path / "app" / ".env"
    env.parent.mkdir()
    monkeypatch.setattr(webui, "ENV_PATH", env)
    current = tmp_path / "current"
    config.use_folder(current)
    current.mkdir()
    config.PROFILE_PATH.write_text("## Job Search Settings\n- Target titles: Project Manager\n")
    conn = db.connect(config.DB_PATH)
    db.kv_set(conn, "marker", "mine")
    conn.commit()
    conn.close()
    return folder, env


def test_copy_into_folder_with_spaces(app, tmp_path):
    folder, env = app
    target = tmp_path / "Job Collector – é"
    msg = folder.switch(str(target), "copy")
    assert config.JOB_FOLDER == target and str(target) in msg
    assert "Project Manager" in (target / "candidate-profile.md").read_text()
    assert db.kv_get(db.connect(target / "data" / "jobs.db"), "marker") == "mine"
    from dotenv import dotenv_values
    assert dotenv_values(env)["JOB_FOLDER"] == str(target)
    assert (tmp_path / "current" / "data" / "jobs.db").exists()  # nothing deleted


def test_existing_data_never_overwritten(app, tmp_path):
    folder, _ = app
    other = tmp_path / "synced"
    (other / "data").mkdir(parents=True)
    (other / "candidate-profile.md").write_text("theirs")
    assert folder.describe(str(other))["has_data"] is True
    for mode in ("copy", "fresh"):
        with pytest.raises(ValueError, match="already has Job Collector data"):
            folder.switch(str(other), mode)
    folder.switch(str(other), "use")
    assert (other / "candidate-profile.md").read_text() == "theirs"


def test_switch_refused_while_locked(app, tmp_path):
    folder, _ = app
    with db.run_lock(config.LOCK_PATH) as got:
        assert got
        with pytest.raises(ValueError, match="A check is running"):
            folder.switch(str(tmp_path / "new"), "copy")
    assert config.JOB_FOLDER == tmp_path / "current"


def test_fresh_leaves_old_data(app, tmp_path):
    folder, _ = app
    folder.switch(str(tmp_path / "empty"), "fresh")
    assert not config.PROFILE_PATH.exists() and not config.DB_PATH.exists()
    assert (tmp_path / "current" / "candidate-profile.md").exists()


def test_describe_problems(app, tmp_path):
    folder, _ = app
    assert folder.describe("relative/path")["problem"]
    assert folder.describe(str(tmp_path / "no" / "such" / "parent"))["problem"]
    ok = folder.describe(str(tmp_path / "new"))
    assert ok == {"path": str(tmp_path / "new"), "has_data": False, "problem": None}
    with pytest.raises(ValueError):
        folder.switch("relative/path", "fresh")


def test_choose_native_cancel_returns_none(monkeypatch):
    import subprocess
    from collector import folder
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="execution error: User canceled. (-128)"))
    assert folder.choose_native() is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="/Users/x/Job Collector/\n", stderr=""))
    assert folder.choose_native() == "/Users/x/Job Collector"


def test_folder_name_with_hash_and_quote_round_trips_through_env(app, tmp_path):
    from dotenv import dotenv_values
    folder, env = app
    target = tmp_path / "Job #2 it's"
    folder.switch(str(target), "fresh")
    assert dotenv_values(env)["JOB_FOLDER"] == str(target)  # scheduled runs read it from here


def test_control_characters_rejected(app, tmp_path):
    folder, _ = app
    assert folder.describe(str(tmp_path / "x\nEMAIL_TO=a@b"))["problem"]


def test_migration_happens_once_even_if_the_db_goes_missing(tmp_path, monkeypatch):
    from collector import folder
    legacy = tmp_path / "app" / "data" / "jobs.db"
    db.connect(legacy).close()
    monkeypatch.setattr(config, "LEGACY_DB", legacy)
    config.use_folder(tmp_path / "mine")
    assert folder.migrate_legacy()
    config.DB_PATH.unlink()  # user deletes it to start over, or a synced copy isn't downloaded yet
    assert folder.migrate_legacy() is None and not config.DB_PATH.exists()


def test_no_migration_into_a_folder_switched_to(app, tmp_path, monkeypatch):
    folder, _ = app
    legacy = tmp_path / "legacy" / "data" / "jobs.db"
    db.connect(legacy).close()
    monkeypatch.setattr(config, "LEGACY_DB", legacy)
    folder.switch(str(tmp_path / "fresh"), "fresh")
    assert folder.migrate_legacy() is None  # a run before the page creates the db must not seed old jobs


def test_osascript_only_cancel_is_none_other_failures_raise(monkeypatch):
    import subprocess
    from collector import folder
    run = lambda code, err: (lambda *a, **k: subprocess.CompletedProcess(a, code, stdout="", stderr=err))
    monkeypatch.setattr(subprocess, "run", run(1, "execution error: User canceled. (-128)"))
    assert folder.choose_native() is None
    monkeypatch.setattr(subprocess, "run", run(1, "execution error: Not authorized to send Apple events. (-1743)"))
    with pytest.raises(RuntimeError):
        folder.choose_native()


def test_windows_picker_output_decoded_as_utf8(monkeypatch):
    import subprocess
    from collector import folder
    seen = {}
    def fake(cmd, **kw):
        seen.update(kw, script=cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, stdout="C:\\Users\\Zoë\\Documents\\Café\r\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(folder, "ON_WINDOWS", True)
    assert folder.choose_native() == "C:\\Users\\Zoë\\Documents\\Café"
    assert seen["encoding"] == "utf-8" and "OutputEncoding" in seen["script"]


@pytest.mark.parametrize("name", ["candidate-profile.md", "data/jobs.db", "data", "logs"])
def test_symlink_in_target_refused_and_nothing_written_through_it(app, tmp_path, name):
    folder, _ = app
    target, outside = tmp_path / "picked", tmp_path / "elsewhere" / "victim"
    (target / "data").mkdir(parents=True)
    outside.parent.mkdir()
    link = target / name
    if link.is_dir():
        link.rmdir()
    link.symlink_to(outside)  # dangling: .exists() is False
    assert "shortcut" in folder.describe(str(target))["problem"]
    for mode in ("copy", "fresh", "use"):
        with pytest.raises(ValueError, match="shortcut"):
            folder.switch(str(target), mode)
    assert not outside.exists() and config.JOB_FOLDER == tmp_path / "current"


def test_symlink_planted_after_the_check_still_refused(app, tmp_path, monkeypatch):
    folder, _ = app
    target, outside = tmp_path / "picked", tmp_path / "victim.md"
    real_lock = db.run_lock

    def lock_then_plant(path):  # the link appears between the first check and the copy
        target.mkdir(exist_ok=True)
        (target / "candidate-profile.md").symlink_to(outside)
        return real_lock(path)

    monkeypatch.setattr(folder.db, "run_lock", lock_then_plant)
    with pytest.raises(ValueError, match="shortcut"):
        folder.switch(str(target), "copy")
    assert not outside.exists()


def test_switch_keeps_the_lock_until_the_new_folder_is_saved(app, tmp_path, monkeypatch):
    folder, _ = app
    from collector import webui
    old_lock, real_update = config.LOCK_PATH, webui.update_env
    held = []

    def update_env(path, values):  # a scheduled run starting now must skip, not use the old folder
        with db.run_lock(old_lock) as got:
            held.append(not got)
        return real_update(path, values)

    monkeypatch.setattr(webui, "update_env", update_env)
    folder.switch(str(tmp_path / "new"), "copy")
    assert held == [True]


def test_copy_db_closes_both_connections(tmp_path, monkeypatch):
    import sqlite3
    from collector import folder
    src = tmp_path / "a.db"
    sqlite3.connect(src).close()
    opened, real_connect = [], sqlite3.connect

    class Tracked:
        def __init__(self, *a):
            self.c, self.closed = real_connect(*a), False
            opened.append(self)
        def __getattr__(self, n):
            return getattr(self.c, n)
        def backup(self, other):
            return self.c.backup(other.c)
        def close(self):
            self.closed = True
            self.c.close()

    monkeypatch.setattr(folder.sqlite3, "connect", Tracked)
    folder._copy_db(src, tmp_path / "out" / "b.db")
    assert len(opened) == 2 and all(c.closed for c in opened)


def test_copy_brings_the_logs_and_keeps_existing_ones(app, tmp_path):
    folder, _ = app
    config.LOGS_DIR.mkdir()
    (config.LOGS_DIR / "collector.log").write_text("old run history\n")
    (config.LOGS_DIR / "collector.err.log").write_text("old errors\n")
    target = tmp_path / "new"
    (target / "logs").mkdir(parents=True)
    (target / "logs" / "collector.err.log").write_text("already here\n")
    folder.switch(str(target), "copy")
    assert (target / "logs" / "collector.log").read_text() == "old run history\n"
    assert (target / "logs" / "collector.err.log").read_text() == "already here\n"  # never overwritten


def test_interrupted_db_copy_leaves_no_half_database(tmp_path, monkeypatch):
    import sqlite3
    from collector import folder
    src = tmp_path / "a.db"
    sqlite3.connect(src).close()
    dst = tmp_path / "out" / "jobs.db"

    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(folder, "_backup", boom)
    with pytest.raises(sqlite3.OperationalError):
        folder._copy_db(src, dst)
    assert list(dst.parent.iterdir()) == []  # another process never sees a half-copied jobs.db


def test_run_started_before_a_switch_skips_instead_of_using_the_old_folder(app, tmp_path, monkeypatch, caplog):
    import argparse
    import logging
    from collector import cli, pipeline
    folder, env = app
    ran = []
    monkeypatch.setattr(pipeline, "run", lambda **k: ran.append(config.JOB_FOLDER))
    loaded = config.JOB_FOLDER  # this process loaded its settings, then got delayed...
    folder.switch(str(tmp_path / "new"), "copy")  # ...while the page switched folders
    config.use_folder(loaded)
    caplog.set_level(logging.INFO)
    cli.cmd_run(argparse.Namespace(dry_run=True))
    assert ran == [] and "data folder changed" in caplog.text
    config.use_folder(tmp_path / "new")
    cli.cmd_run(argparse.Namespace(dry_run=True))  # the next check uses the new folder
    assert ran == [tmp_path / "new"]
