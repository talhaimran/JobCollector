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


def _check_platform():
    want, have = config.SCHEDULE_PLATFORM, this_platform()
    if want != have:
        raise SystemExit(f"SCHEDULE_PLATFORM is '{want}' but this computer is '{have}'. "
                         f"Run this on the {want} computer, or change the setting.")


def install() -> str:
    _check_platform()
    (config.ROOT / "logs").mkdir(exist_ok=True)
    if config.SCHEDULE_PLATFORM == "windows":
        subprocess.run(windows_create(config.ROOT, config.SCHEDULE_HOURS), check=True)
        return f"Task Scheduler task '{TASK}' runs every {config.SCHEDULE_HOURS:g} h. Logs: logs\\collector.log"
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(mac_plist(config.ROOT, config.SCHEDULE_HOURS))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)  # fine if not loaded
    subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    return f"launchd job {LABEL} runs every {config.SCHEDULE_HOURS:g} h (and once now). Logs: logs/collector.log"


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
