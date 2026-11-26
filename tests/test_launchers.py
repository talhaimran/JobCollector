"""The double-click launchers exist, are runnable and start the settings page."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAC, WIN = ROOT / "Launch JobCollector Mac.command", ROOT / "Launch JobCollector Windows.bat"


unix_only = pytest.mark.skipif(os.name == "nt", reason="Unix file permissions and bash")


@unix_only
def test_launchers_exist_and_parse():
    assert MAC.exists() and WIN.exists()
    assert os.access(MAC, os.X_OK)  # Finder only runs .command files that are executable
    assert subprocess.run(["bash", "-n", str(MAC)]).returncode == 0
    for f in (MAC, WIN):
        text = f.read_text()
        assert "-m collector ui" in text and "requirements.sha" in text and "python.org/downloads" in text
    assert b"\r\n" in WIN.read_bytes()  # cmd.exe misreads LF-only batch files


@unix_only
def test_mac_launcher_explains_a_read_only_folder(tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    launcher = folder / MAC.name
    launcher.write_bytes(MAC.read_bytes())
    (folder / "requirements.txt").write_text("")
    folder.chmod(0o555)
    try:
        r = subprocess.run(["bash", str(launcher)], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
    finally:
        folder.chmod(0o755)
    assert r.returncode == 1
    assert "can't save files in this folder" in r.stdout
    assert not (folder / ".venv").exists()


def test_windows_launcher_checks_the_folder_is_writable():
    assert "can't save files in this folder" in WIN.read_text()


def test_windows_write_check_cannot_touch_an_existing_file():
    # it must create something new (mkdir fails rather than overwrite), never redirect into a file
    text = WIN.read_text()
    assert "type nul >" not in text and 'del "' not in text
    assert 'mkdir "%PROBE%"' in text and 'rmdir "%PROBE%"' in text and "%RANDOM%" in text
