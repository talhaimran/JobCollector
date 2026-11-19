import json
import threading
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
    assert pv["jobs"] == [{"id": 1, "title": "Project Manager", "company": None, "old": 37, "new": 100.0, "emailed": True}]

    assert call(server + "/api/config", {**GOOD, "EMAIL_TO": "bad"})[0] == 400


def test_api_blocks_cross_site(server):
    assert call(server + "/api/config", GOOD, {"Origin": "https://evil.example"})[0] == 403
    assert call(server + "/api/jobs", headers={"Host": "evil.example"})[0] == 403
    req = urllib.request.Request(server + "/api/config", data=b"x", headers={"Content-Type": "text/plain"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
