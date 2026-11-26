import os
from pathlib import Path

import pytest

from collector import config, schedule, webui
from collector.normalize import work_model_from_text


@pytest.mark.skipif(os.name == "nt", reason="the plist is built from Mac paths")
def test_mac_plist():
    plist = schedule.mac_plist(Path("/x/proj"), 3)
    assert "<string>/x/proj/.venv/bin/python</string>" in plist and "<integer>10800</integer>" in plist


def test_windows_command():
    cmd = schedule.windows_create(Path(r"C:\proj"), 2)
    assert cmd[:4] == ["schtasks", "/Create", "/F", "/TN"] and cmd[cmd.index("/MO") + 1] == "2"
    assert cmd[-1].endswith('run-collector.cmd"')


def test_wrong_platform_is_refused(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULE_PLATFORM", "windows" if schedule.this_platform() == "mac" else "mac")
    with pytest.raises(SystemExit, match="SCHEDULE_PLATFORM"):
        schedule.install()


def test_windows_runner_uses_crlf():
    assert b"\r\n" in (config.ROOT / "windows" / "run-collector.cmd").read_bytes()


def test_validate_schedule_fields():
    from tests.test_webui import GOOD
    env, _ = webui.validate({**GOOD, "SCHEDULE_PLATFORM": "windows", "SCHEDULE_HOURS": "3", "SEND_PARTIAL_SCORES": True})
    assert env["SCHEDULE_PLATFORM"] == "windows" and env["SCHEDULE_HOURS"] == 3 and env["SEND_PARTIAL_SCORES"] == "true"
    for bad in ({"SCHEDULE_PLATFORM": "linux"}, {"SCHEDULE_HOURS": "0"}, {"SCHEDULE_HOURS": "1.5"}):
        with pytest.raises(ValueError):
            webui.validate({**GOOD, **bad})


@pytest.mark.parametrize("text, model", [
    ("You will work 3 days a week in our Kitchener office.", "hybrid"),
    ("2-3 days per week on-site", "hybrid"),
    ("Expect days in office twice a month", "hybrid"),
    ("three days on site", "hybrid"),
    ("We need 5 days in office", "on-site"),
    ("Workplace type: Hybrid", "hybrid"),
    ("Experience with hybrid delivery (Agile/Waterfall)", None),
    ("This role is fully remote", "remote"),
    ("Fully on-site at our plant", "on-site"),
])
def test_work_model_from_description(text, model):
    assert work_model_from_text(text) == model


def test_save_applies_schedule_only_when_it_changed(monkeypatch):
    calls = []
    monkeypatch.setattr(schedule, "this_platform", lambda: "mac")
    monkeypatch.setattr(schedule, "installed_hours", lambda: 2.0)
    monkeypatch.setattr(schedule, "install", lambda p, h: calls.append((p, h)) or "installed")
    assert webui.apply_schedule({"SCHEDULE_PLATFORM": "mac", "SCHEDULE_HOURS": 2}) is None and calls == []
    assert webui.apply_schedule({"SCHEDULE_PLATFORM": "mac", "SCHEDULE_HOURS": 3}) == "installed"
    assert calls == [("mac", 3)]
    assert "not applied" in webui.apply_schedule({"SCHEDULE_PLATFORM": "windows", "SCHEDULE_HOURS": 3})
    assert webui.apply_schedule({}) is None and len(calls) == 1


def test_save_reports_install_failure(monkeypatch):
    import subprocess
    monkeypatch.setattr(schedule, "this_platform", lambda: "mac")
    monkeypatch.setattr(schedule, "installed_hours", lambda: None)
    def fail(p, h):
        raise subprocess.CalledProcessError(5, "launchctl", stderr="Bootstrap failed: 5")
    monkeypatch.setattr(schedule, "install", fail)
    assert "wasn't applied: Bootstrap failed" in webui.apply_schedule({"SCHEDULE_PLATFORM": "mac", "SCHEDULE_HOURS": 3})
