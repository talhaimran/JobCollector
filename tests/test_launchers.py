"""The double-click launchers exist, are runnable and start the settings page."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAC, WIN = ROOT / "Start Job Collector.command", ROOT / "Start Job Collector.bat"


def test_launchers_exist_and_parse():
    assert MAC.exists() and WIN.exists()
    assert os.access(MAC, os.X_OK)  # Finder only runs .command files that are executable
    assert subprocess.run(["bash", "-n", str(MAC)]).returncode == 0
    for f in (MAC, WIN):
        text = f.read_text()
        assert "-m collector ui" in text and "requirements.sha" in text and "python.org/downloads" in text
    assert b"\r\n" in WIN.read_bytes()  # cmd.exe misreads LF-only batch files
