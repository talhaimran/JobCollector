import json
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from collector import config, db, prescore, settings, webui
from collector.settings import Settings


def test_update_env_keeps_comments_and_other_keys(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# mail\nEMAIL_TO=old@x.com\nJOB_FOLDER=/a\n")
    webui.update_env(p, {"EMAIL_TO": "new@x.com", "KEYWORD_FULL_MARKS": 8})
    assert p.read_text() == "# mail\nEMAIL_TO=new@x.com\nJOB_FOLDER=/a\nKEYWORD_FULL_MARKS=8\n"
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_set_value_rewrites_only_the_section():
    text = "Email cutoff: 10\n## Job Search Settings\nTarget titles: PM\nEmail cutoff (pre-score): 60\n\n## Next\n"
    out = settings.set_value(text, "email_cutoff", "45")
    assert out == "Email cutoff: 10\n## Job Search Settings\nTarget titles: PM\nEmail cutoff (pre-score): 45\n\n## Next\n"
    assert settings.parse(settings.set_value(out, "email_cutoff", "")).email_cutoff is None
    added = settings.set_value("## Job Search Settings\nTarget titles: PM\n\n## Next\n", "email_cutoff", "50")
    assert settings.parse(added).email_cutoff == 50 and added.endswith("Email cutoff: 50\n\n## Next\n")


GOOD = {"SCORE_WEIGHT_KEYWORDS": 0.6, "KEYWORD_FULL_MARKS": 8, "TITLE_EXACT_SCORE": 100,
        "TITLE_CONTAINED_SCORE": 70, "EMAIL_FROM": "a@b.co", "EMAIL_TO": "", "SMTP_USER": "a@b.co",
        "SMTP_APP_PASSWORD": "abcd efgh ijkl mnop", "SEND_WHEN_EMPTY": False, "email_cutoff": "55"}


def test_validate():
    env, cutoff = webui.validate(GOOD)
    assert env["SMTP_APP_PASSWORD"] == "abcdefghijklmnop" and cutoff == 55 and env["EMAIL_TO"] == ""
    assert "SMTP_APP_PASSWORD" not in webui.validate({**GOOD, "SMTP_APP_PASSWORD": ""})[0]  # keep saved
    for bad, msg in [({"SCORE_WEIGHT_KEYWORDS": 1.5}, "between 0 and 1"),
                     ({"KEYWORD_FULL_MARKS": 2.5}, "whole number"),
                     ({"EMAIL_TO": "nope"}, "email address"),
                     ({"EMAIL_TO": "a@b.co\nEVIL=1"}, "email address"),
                     ({"email_cutoff": "abc"}, "number")]:
        with pytest.raises(ValueError, match=msg):
            webui.validate({**GOOD, **bad})


def test_score_overrides():
    s = Settings(target_titles=["Project Manager"], must_have=["Jira", "Agile"])
    j = {"title": "Project Manager", "description": "Jira"}
    assert prescore.score(j, s)["prescore_total"] == pytest.approx(10 * 0.7 + 100 * 0.3)
    p = {**prescore.params(), "SCORE_WEIGHT_KEYWORDS": 0.5, "KEYWORD_FULL_MARKS": 1}
    assert prescore.score(j, s, p)["prescore_total"] == 100


@pytest.fixture
def server(tmp_path, monkeypatch):
    profile = tmp_path / "candidate-profile.md"
    profile.write_text("## Job Search Settings\nTarget titles: Project Manager\nMust-have keywords: Jira\n"
                       "Email cutoff (pre-score): 60\n")
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(webui, "ENV_PATH", tmp_path / ".env")
    conn = db.connect(config.DB_PATH)
    db.insert_job(conn, {"source": "indeed", "source_job_id": "1", "url": "u1", "title": "Project Manager",
                         "description": "Jira", "found_at": "2026-11-18T15:00:00+00:00", "filter_passed": 1,
                         "prescore_total": 37, "posted_at": "2026-11-17"})
    db.insert_job(conn, {"source": "indeed", "source_job_id": "2", "url": "u2", "title": "Chef",
                         "found_at": "2026-11-10T15:00:00+00:00", "filter_passed": 0,
                         "filter_reason": "title not in target titles"})
    conn.commit()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(url, body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_jobs_date_filter_and_config_roundtrip(server):
    assert len(call(server + "/api/jobs")[1]) == 2
    assert [j["id"] for j in call(server + "/api/jobs?from=2026-11-15&to=2026-11-19")[1]] == [1]
    assert [j["id"] for j in call(server + "/api/jobs?from=2026-11-17&to=2026-11-17&field=posted")[1]] == [1]

    code, cfg = call(server + "/api/config", GOOD)
    assert code == 200 and cfg["email_cutoff"] == 55 and cfg["has_password"] and "SMTP_APP_PASSWORD" not in cfg
    assert cfg["SCORE_WEIGHT_KEYWORDS"] == 0.6 and cfg["KEYWORD_FULL_MARKS"] == 8

    code, pv = call(server + "/api/preview", {"SCORE_WEIGHT_KEYWORDS": 0.5, "KEYWORD_FULL_MARKS": 1, "email_cutoff": 90})
    assert pv["jobs"] == [{"id": 1, "title": "Project Manager", "company": None, "old": 37, "new": 100.0,
                           "partial": False, "emailed": True}]

    assert call(server + "/api/config", {**GOOD, "EMAIL_TO": "bad"})[0] == 400


def test_api_blocks_cross_site(server):
    assert call(server + "/api/config", GOOD, {"Origin": "https://evil.example"})[0] == 403
    assert call(server + "/api/jobs", headers={"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(server + "/api/config", data=b"x", headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


PROFILE_BODY = {"target_titles": "Project Manager,  Senior Project Manager", "exclude_titles": "",
                "locations": "Kitchener ON", "remote_in": "Canada", "work_model": "remote, Hybrid",
                "employment_type": "Full-time", "salary_floor": "85000", "must_have": "Jira, Agile",
                "dealbreakers": "French required", "excluded_companies": ""}


def test_validate_profile():
    out = webui.validate_profile({"profile": PROFILE_BODY})
    assert out["target_titles"] == "Project Manager, Senior Project Manager"
    assert out["work_model"] == "Remote, Hybrid" and out["salary_floor"] == "85000"
    assert webui.validate_profile({}) == {}  # page sent no profile: leave it alone
    for bad, msg in [({"work_model": "Office"}, "isn't one of"),
                     ({"locations": "Kitchener\nEmail cutoff: 0"}, "one line"),
                     ({"salary_floor": "-5"}, "between")]:
        with pytest.raises(ValueError, match=msg):
            webui.validate_profile({"profile": {**PROFILE_BODY, **bad}})


def test_save_creates_missing_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "candidate-profile.md")
    monkeypatch.setattr(webui, "ENV_PATH", tmp_path / ".env")
    cfg = webui.read_config()
    assert not cfg["profile_exists"] and cfg["profile"]["target_titles"] == ""
    webui.save_config({**GOOD, "profile": PROFILE_BODY})
    s = settings.load(config.PROFILE_PATH)
    assert s.target_titles == ["Project Manager", "Senior Project Manager"] and s.exclude_titles == []
    assert s.salary_floor == 85000 and s.email_cutoff == 55 and s.must_have == ["Jira", "Agile"]


def test_save_adds_section_to_profile_without_one(tmp_path, monkeypatch):
    p = tmp_path / "candidate-profile.md"
    p.write_text("# Resume\nSome text\n")
    monkeypatch.setattr(config, "PROFILE_PATH", p)
    monkeypatch.setattr(webui, "ENV_PATH", tmp_path / ".env")
    webui.save_config({**GOOD, "profile": PROFILE_BODY})
    assert p.read_text().startswith("# Resume\nSome text\n\n## Job Search Settings\n")
    assert settings.load(p).locations == ["Kitchener ON"]


def test_preview_uses_unsaved_filters(server):
    # job 2 (Chef) failed before; with Chef as a target title it passes in the preview, nothing saved
    body = {"profile": {**PROFILE_BODY, "target_titles": "Chef", "locations": "", "work_model": ""}}
    pv = call(server + "/api/preview", body)[1]
    assert [(j["id"], j["old"]) for j in pv["jobs"]] == [(2, None)]
    assert settings.load(config.PROFILE_PATH).target_titles == ["Project Manager"]


def _hold_port(handler):
    import threading
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_serve_when_job_collector_already_open_opens_browser_and_returns(monkeypatch):
    import webbrowser
    other = _hold_port(webui.Handler)
    port = other.server_address[1]
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    try:
        webui.serve(port)  # must return instead of raising "Address already in use"
    finally:
        other.shutdown()
    assert opened == [f"http://127.0.0.1:{port}/"]


def test_folder_endpoints(server, tmp_path, monkeypatch):
    from collector import folder
    config.use_folder(tmp_path / "cur")
    try:
        monkeypatch.setattr(folder, "choose_native", lambda: "/Users/x/Job Collector")
        assert call(server + "/api/folder/choose", {}) == (200, {"path": "/Users/x/Job Collector"})
        monkeypatch.setattr(folder, "choose_native", lambda: None)
        assert call(server + "/api/folder/choose", {}) == (200, {"path": None})

        target = tmp_path / "New Folder"
        code, d = call(server + "/api/folder?path=" + urllib.parse.quote(str(target)))
        assert code == 200 and d == {"path": str(target), "has_data": False, "problem": None}

        code, cfg = call(server + "/api/folder", {"path": str(target), "mode": "fresh"})
        assert code == 200 and cfg["job_folder"] == str(target) and "starting fresh" in cfg["message"]
        assert call(server + "/api/config")[1]["job_folder"] == str(target)
        assert call(server + "/api/folder", {"path": "relative", "mode": "fresh"})[0] == 400
    finally:
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_partial_save_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_PATH", tmp_path / "candidate-profile.md")
    monkeypatch.setattr(webui, "ENV_PATH", tmp_path / ".env")
    applied = []
    monkeypatch.setattr(webui, "apply_schedule", lambda env: applied.append(env))
    webui.save_config({**GOOD, "SCHEDULE_HOURS": 2, "profile": PROFILE_BODY})
    assert len(applied) == 1
    webui.save_config({"profile": {"target_titles": "Scrum Master"}})  # one wizard step
    cfg = webui.read_config()
    assert cfg["profile"]["target_titles"] == "Scrum Master"
    assert cfg["profile"]["salary_floor"] == 85000 and cfg["profile"]["dealbreakers"] == "French required"
    assert cfg["EMAIL_FROM"] == "a@b.co" and cfg["email_cutoff"] == 55 and cfg["has_password"]
    assert len(applied) == 1  # the schedule is applied only when the page sends it


def test_test_email_auth_error_is_plain(monkeypatch, tmp_path):
    import smtplib
    monkeypatch.setattr(webui, "ENV_PATH", tmp_path / ".env")
    def refuse(*a):
        raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    monkeypatch.setattr(webui.emailer, "send", refuse)
    with pytest.raises(ValueError, match="app password"):
        webui.send_test_email()


def test_run_now_starts_once_and_streams_new_log_lines(server, tmp_path, monkeypatch):
    import subprocess
    config.use_folder(tmp_path / "data folder")
    try:
        log = config.LOGS_DIR / "collector.log"
        log.parent.mkdir(parents=True)
        log.write_text("2026-11-25 09:00:00,000 INFO collector.pipeline: an older run\n")

        class Proc:
            code = None
            def poll(self):
                return self.code
        started = []
        def popen(cmd, **kw):
            started.append(cmd)
            return Proc()
        monkeypatch.setattr(subprocess, "Popen", popen)

        assert call(server + "/api/run", {})[0] == 200
        assert started and started[0][-3:] == ["-m", "collector", "run"]
        assert call(server + "/api/run", {}) == (409, {"error": "A check is already running."})

        with log.open("a") as f:
            f.write("2026-11-25 10:00:00,500 INFO collector.pipeline: Settings:\n  target titles  Project Manager\n")
            f.write("2026-11-25 10:00:01,000 INFO collector.sources.randstad: randstad: 4 requests, 2 new jobs\n")
            f.write("2026-11-25 10:00:02,000 INFO collector.pipeline: Run 1 done: {'run_id': 1, 'found': 13}\n")
        code, st = call(server + "/api/run")
        assert code == 200 and st["running"] is True
        assert st["lines"] == ["randstad: 4 requests, 2 new jobs"]  # not the older run, no timestamps

        webui.RUN["proc"].code = 0
        assert call(server + "/api/run")[1]["running"] is False
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_page_uses_plain_labels():
    page = webui.PAGE.read_text()
    for label in ("Jobs found", "Match score", "Job titles to look for", "Skip titles containing",
                  "Remote jobs allowed in", "Skip jobs that mention", "Skip these companies",
                  "Keywords that raise the match score", "Lowest match score to email", "Gmail address used to send"):
        assert label in page, label
    for old in ("Dealbreaker keywords", "Filter audit", ">Audit<", "Email cutoff</label>", "Must-have keywords</label>"):
        assert old not in page, old


def test_config_suggests_documents_folder():
    from pathlib import Path
    assert webui.read_config()["suggested_folder"] == str(Path.home() / "Documents" / "Job Collector")


def test_run_now_joins_a_check_already_running(server, tmp_path):
    config.use_folder(tmp_path / "f")
    try:
        log = config.LOGS_DIR / "collector.log"
        log.parent.mkdir(parents=True)
        log.write_text("2026-11-25 09:00:00,000 INFO collector.pipeline: before\n")
        with db.run_lock(config.LOCK_PATH) as got:  # e.g. the schedule just started one
            assert got
            assert call(server + "/api/run", {}) == (200, {"started": False})
            with log.open("a") as f:
                f.write("2026-11-25 09:00:05,000 INFO collector.sources.linkedin: linkedin: 3 new jobs\n")
            st = call(server + "/api/run")[1]
            assert st["running"] is True and st["lines"] == ["linkedin: 3 new jobs"]
        assert call(server + "/api/run")[1]["running"] is False
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_setup_wizard_markup():
    page = webui.PAGE.read_text()
    assert 'id="view-setup"' in page and page.count('<section class="group" data-step="') == 5
    assert ("Skip email? Job Collector won't send you any updates. "
            "You'll only see new jobs when you open this page.") in page
    assert "Run setup again" in page


def test_run_now_child_gets_current_settings_and_only_its_own_run(server, tmp_path, monkeypatch):
    import subprocess
    config.use_folder(tmp_path / "now here")
    try:
        (tmp_path / ".env").write_text("EMAIL_TO=new@x.co\nJOB_FOLDER=/stale\n")
        monkeypatch.setenv("EMAIL_TO", "old@x.co")  # what the UI process loaded at startup
        conn = db.connect(config.DB_PATH)
        db.start_run(conn, "2026-11-01T00:00:00+00:00", "[]")  # an earlier run
        conn.commit()
        seen = {}
        class Proc:
            def poll(self):
                return 1  # crashed before writing a run row
        def popen(cmd, **kw):
            seen.update(kw)
            return Proc()
        monkeypatch.setattr(subprocess, "Popen", popen)
        assert call(server + "/api/run", {}) == (200, {"started": True})
        assert seen["env"]["EMAIL_TO"] == "new@x.co" and seen["env"]["JOB_FOLDER"] == str(config.JOB_FOLDER)
        assert seen.get("start_new_session") or seen.get("creationflags")  # survives closing the launcher window
        st = call(server + "/api/run")[1]
        assert st["running"] is False and st["run"] is None  # the old run isn't passed off as this one
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_serve_when_another_program_has_port_says_so_and_opens_nothing(monkeypatch, capsys):
    import webbrowser
    from http.server import BaseHTTPRequestHandler

    class Stranger(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *a):
            pass

    other = _hold_port(Stranger)
    port = other.server_address[1]
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    try:
        with pytest.raises(SystemExit) as e:
            webui.serve(port)
    finally:
        other.shutdown()
    assert e.value.code == 1
    assert opened == []
    assert f"Port {port} is used by another program" in capsys.readouterr().out


def test_run_now_joining_a_check_that_already_has_its_run_row_reports_that_run(server, tmp_path):
    config.use_folder(tmp_path / "f")
    try:
        conn = db.connect(config.DB_PATH)
        db.start_run(conn, "2026-11-25T09:00:00+00:00", "[]")  # an older, finished run
        conn.execute("UPDATE runs SET finished_at='2026-11-25T09:05:00+00:00'")
        with db.run_lock(config.LOCK_PATH):
            joined = db.start_run(conn, "2026-11-25T11:00:00+00:00", "[]")  # the scheduled check, mid-run
            assert call(server + "/api/run", {}) == (200, {"started": False})
            conn.execute("UPDATE runs SET finished_at='2026-11-25T11:04:00+00:00' WHERE id=?", (joined,))
            conn.commit()
        st = call(server + "/api/run")[1]
        assert st["running"] is False and st["run"]["id"] == joined
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False, after_id=0)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


@pytest.mark.parametrize("code, reported", [(1, False), (75, True)])
def test_run_now_child_that_did_not_run_its_own_check(server, tmp_path, monkeypatch, code, reported):
    """1: it crashed, so a later run isn't its result. 75: it lost the lock to a check starting at the
    same moment, so the page follows that check, as when it joins one already running."""
    import subprocess
    config.use_folder(tmp_path / "f")
    try:
        conn = db.connect(config.DB_PATH)

        class Proc:
            def poll(self):
                return code
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: Proc())
        assert call(server + "/api/run", {}) == (200, {"started": True})
        other = db.start_run(conn, "2026-11-25T11:00:00+00:00", "[]")
        conn.execute("UPDATE runs SET finished_at='2026-11-25T11:04:00+00:00' WHERE id=?", (other,))
        conn.commit()
        st = call(server + "/api/run")[1]
        assert st["running"] is False
        assert (st["run"] is not None and st["run"]["id"] == other) is reported
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False, after_id=0)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_run_lost_to_another_check_exits_75(tmp_path, monkeypatch):
    import argparse
    from collector import cli
    config.use_folder(tmp_path)
    with db.run_lock(config.LOCK_PATH):
        with pytest.raises(SystemExit) as e:
            cli.cmd_run(argparse.Namespace(dry_run=True))
    assert e.value.code == 75
    config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_run_now_on_windows_is_detached_from_the_launcher_console(server, tmp_path, monkeypatch):
    import subprocess
    config.use_folder(tmp_path / "f")
    try:
        seen = {}

        class Proc:
            def poll(self):
                return None
        def popen(cmd, **kw):
            seen.update(kw)
            return Proc()
        monkeypatch.setattr(subprocess, "Popen", popen)
        monkeypatch.setattr(webui, "ON_WINDOWS", True)
        assert call(server + "/api/run", {})[0] == 200
        # DETACHED_PROCESS (0x8): no console, so closing the launcher window doesn't send it CTRL_CLOSE
        assert seen["creationflags"] & 0x8 and seen["creationflags"] & 0x200
        assert seen["stdout"] == subprocess.DEVNULL and seen["stderr"] is not None
        assert "start_new_session" not in seen
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False, after_id=0)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_setup_schedules_for_this_computer_not_the_saved_platform(monkeypatch, tmp_path):
    from collector import schedule
    webui.ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    webui.ENV_PATH.write_text("SCHEDULE_PLATFORM=windows\n")  # .env came from another computer
    monkeypatch.setattr(schedule, "this_platform", lambda: "mac")
    cfg = webui.read_config()
    assert cfg["SCHEDULE_PLATFORM"] == "windows" and cfg["this_platform"] == "mac"
    assert "SCHEDULE_PLATFORM: CFG.this_platform" in webui.PAGE.read_text()  # the wizard's schedule step


def _finished_run(conn, started):
    rid = db.start_run(conn, started, "[]")
    conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (started, rid))
    conn.commit()
    return rid


def test_run_now_reports_its_own_run_even_if_a_scheduled_one_starts_before_the_next_poll(server, tmp_path, monkeypatch):
    import subprocess
    config.use_folder(tmp_path / "f")
    try:
        conn = db.connect(config.DB_PATH)

        class Proc:
            code = None
            def poll(self):
                return self.code
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: Proc())
        assert call(server + "/api/run", {}) == (200, {"started": True})
        mine = _finished_run(conn, "2026-11-25T11:00:00+00:00")
        webui.RUN["proc"].code = 0
        with db.run_lock(config.LOCK_PATH):  # the schedule starts right away
            db.start_run(conn, "2026-11-25T11:03:00+00:00", "[]")
            st = call(server + "/api/run")[1]
        assert st["running"] is False and st["run"]["id"] == mine
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False, after_id=0)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_run_now_clean_exit_without_a_finished_run_says_it_stopped(server, tmp_path, monkeypatch):
    import subprocess
    config.use_folder(tmp_path / "f")
    try:
        class Proc:
            def poll(self):
                return 0  # e.g. skipped because the data folder changed
        monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: Proc())
        call(server + "/api/run", {})
        st = call(server + "/api/run")[1]
        assert st["running"] is False and st["run"] is None  # the page says the check stopped
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False, after_id=0)
        config.use_folder(config.JOB_FOLDER_DEFAULT)


def test_simultaneous_run_now_clicks_start_one_check(server, tmp_path, monkeypatch):
    import subprocess
    import threading
    import time
    config.use_folder(tmp_path / "f")
    try:
        started = []

        class Proc:
            def poll(self):
                return None
        def slow_popen(cmd, **kw):
            time.sleep(0.2)  # widen the window between "is one running?" and "started"
            started.append(cmd)
            return Proc()
        monkeypatch.setattr(subprocess, "Popen", slow_popen)
        codes = []
        ts = [threading.Thread(target=lambda: codes.append(call(server + "/api/run", {})[0])) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len(started) == 1 and sorted(codes) == [200, 409]
    finally:
        webui.RUN.update(proc=None, offset=0, watching=False, after_id=0)
        config.use_folder(config.JOB_FOLDER_DEFAULT)
