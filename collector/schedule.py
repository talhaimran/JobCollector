"""Install the every-N-hours run: launchd on macOS, Task Scheduler on Windows.
`python -m collector schedule install | uninstall | status`. The platform comes from SCHEDULE_PLATFORM."""
import os
import re
import subprocess
import sys
from pathlib import Path

from . import config

LABEL, TASK = "com.jobcollector.collector", "JobCollector"
PLIST_TEMPLATE = config.ROOT / "launchd" / f"{LABEL}.plist"
WINDOWS_SCRIPT = config.ROOT / "windows" / "run-collector.cmd"


def this_platform() -> str:
    return "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else sys.platform


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def mac_plist(root: Path, hours: float) -> str:
    text = PLIST_TEMPLATE.read_text().replace("__ROOT__", str(root))
    return re.sub(r"(<key>StartInterval</key>\s*<integer>)\d+(</integer>)", rf"\g<1>{int(hours * 3600)}\g<2>", text)


def windows_create(root: Path, hours: float) -> list[str]:
    return ["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "HOURLY", "/MO", str(int(hours)),
            "/TR", f'"{root / "windows" / "run-collector.cmd"}"']


def installed_hours() -> float | None:
    """The interval of the job installed on this computer, or None if there isn't one."""
    if this_platform() == "windows":
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/XML"], capture_output=True, text=True)
        m = re.search(r"<Interval>PT(\d+)H</Interval>", r.stdout)
        return float(m[1]) if m else None
    path = plist_path()
    m = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", path.read_text()) if path.exists() else None
    return int(m[1]) / 3600 if m else None


def _check_platform(want=None):
    want, have = want or config.SCHEDULE_PLATFORM, this_platform()
    if want != have:
        raise SystemExit(f"SCHEDULE_PLATFORM is '{want}' but this computer is '{have}'. "
                         f"Run this on the {want} computer, or change the setting.")


def install(platform=None, hours=None) -> str:
    """Install or replace the scheduled job (same name, so never a second one)."""
    platform, hours = platform or config.SCHEDULE_PLATFORM, hours or config.SCHEDULE_HOURS
    _check_platform(platform)
    (config.ROOT / "logs").mkdir(exist_ok=True)
    if platform == "windows":
        subprocess.run(windows_create(config.ROOT, hours), check=True, capture_output=True)
        return f"Task Scheduler task '{TASK}' now runs every {hours:g} h."
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mac_plist(config.ROOT, hours))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)  # fine if not loaded
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True, capture_output=True)
    return f"launchd job now runs every {hours:g} h; a run started now."


def uninstall() -> str:
    _check_platform()
    if config.SCHEDULE_PLATFORM == "windows":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK], check=True)
        return f"Removed Task Scheduler task '{TASK}'."
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    plist_path().unlink(missing_ok=True)
    return f"Removed launchd job {LABEL}."


def status() -> str:
    _check_platform()
    if config.SCHEDULE_PLATFORM == "windows":
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/V", "/FO", "LIST"], capture_output=True, text=True)
    else:
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    return r.stdout.strip() or "Not installed."
