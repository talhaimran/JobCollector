# Update to the Latest Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** People who installed from a release ZIP can see whether a newer release exists and install it in place, from Settings → This computer or with `python -m collector update`, without touching their data.

**Architecture:** A new `collector/update.py` holds everything: the version check against GitHub's releases/latest API, a 24-hour cache, and `install()`, which downloads the release ZIP, checks it, swaps only the app's own top-level files (backing them up), installs packages if `requirements.txt` changed, and rolls back on any failure, all under the existing run lock. `cli.py` and `webui.py` are thin callers; `index.html` adds a version line, an Update now button and a banner.

**Tech Stack:** Python 3.10+, `requests`, standard library (`zipfile`, `shutil`, `hashlib`, `subprocess`), the existing `http.server` page, vanilla JS. pytest.

**Spec:** `docs/superpowers/specs/2026-11-25-self-update-design.md`

## Global Constraints

- No AI/LLM calls. No sign-ins. Network only to `https://api.github.com/repos/talhaimran/JobCollector/releases/latest` and the `zipball_url` it returns, timeout `config.HTTP_TIMEOUT`.
- `__version__ = "0.2.0"`; release tag = `"v" + __version__`.
- Never touched by an update: `.env`, `.venv`, `data`, `logs`, `candidate-profile.md`, `*-resume.md`, `.git`, `.update-backup`, `.update-new`, and any top-level name the release doesn't ship.
- Refuse in a git clone (`<root>/.git` exists) with a message to use `git pull`.
- Under `db.run_lock(config.LOCK_PATH)`; lock held → `update.Busy("A job check is running; try again when it finishes.")`; CLI exit 75.
- Packages hash = SHA-256 hex of `requirements.txt`, written to `.venv/requirements.sha` exactly like the launchers.
- Page checks GitHub at most once per 24 h (cache `config.DATA_DIR / "update-check.json"`); scheduled runs never check.
- Tests never touch the network (`no_network` fixture) or the real app folder: every update function takes `root: Path`.
- Every change on branch `self-update`; PR to main; don't merge.

## Review Focus

1. A ZIP entry with `..` or an absolute path, or a second top-level folder: install must refuse before writing anything into the app folder.
2. Failure halfway through the swap (e.g. a file in use on Windows): every already-moved name is put back, the app stays on the old version, and `.update-new/` is cleaned up.
3. A release whose ZIP contains `.env`, `data/` or `candidate-profile.md`: those are ignored, the person's copies are untouched.
4. GitHub offline / rate-limited (403) / malformed JSON / unparseable tag: check returns "no update", never an error page or a crash.
5. A job check holding the lock: nothing is downloaded or moved; page gets 409 with the message, CLI exits 75.

---

### Task 1: Version and the release check

**Files:**
- Modify: `collector/__init__.py`
- Create: `collector/update.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Produces: `collector.__version__: str`; `update.parse_version(s: str) -> tuple[int, ...] | None` (accepts `"0.2.0"` and `"v0.2.0"`); `update.latest() -> dict | None` returning `{"version": str, "notes_url": str, "zip_url": str}`; `update.status(force: bool = False) -> dict` returning `{"current", "latest", "notes_url", "zip_url", "available": bool}` using the 24 h cache unless `force`; `update.API_URL`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_version():
    assert update.parse_version("v0.10.0") > update.parse_version("0.9.1")
    assert update.parse_version("v1.2") == (1, 2)
    assert update.parse_version("nightly") is None

def test_latest_reads_github(monkeypatch):  # fake requests.get → 200 {"tag_name": "v0.3.0", "html_url": N, "zipball_url": Z}
    assert update.latest() == {"version": "0.3.0", "notes_url": N, "zip_url": Z}

@pytest.mark.parametrize("fake", [<raises ConnectionError>, <403>, <200 with "not json">, <200 {"tag_name": "nightly"}>])
def test_latest_is_none_on_any_problem(monkeypatch, fake):
    assert update.latest() is None

def test_status_caches_for_a_day(tmp_path, monkeypatch):
    # config.DATA_DIR → tmp_path; count calls to update.latest
    # two status() calls → 1 call; cache file's checked time set 25 h back → 2 calls; status(force=True) always calls
    # with latest 0.3.0: available is True; with latest == __version__ or None: available is False
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_update.py -q` — Expected: FAIL (no module `collector.update`).
- [ ] **Step 3: Implement** `__version__ = "0.2.0"` and the four functions. `latest()` uses `requests.get(API_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "JobCollector"}, timeout=config.HTTP_TIMEOUT)` inside `try/except Exception: return None`. Cache JSON: `{"checked": <iso>, "latest": <latest() result or null>}`; an unreadable cache counts as expired.
- [ ] **Step 4: Run** the same command — Expected: PASS. Then the whole suite: `.venv/bin/python -m pytest -q` — Expected: all pass.
- [ ] **Step 5: Commit** `git add collector/__init__.py collector/update.py tests/test_update.py && git commit -m "Update check: version 0.2.0 and the latest release from GitHub (cached a day)"`

### Task 2: Download and check the release ZIP

**Files:**
- Modify: `collector/update.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Consumes: `latest()` result shape from Task 1.
- Produces: `update.UpdateError(Exception)`; `update.unpack(zip_path: Path, dest: Path, version: str) -> Path` — extracts to `dest`, returns the release's top folder inside it; raises `UpdateError` (and leaves nothing in `dest`) if the ZIP is invalid, has more than one top-level folder, any entry is absolute or contains `..`, any of `collector/__init__.py`, `requirements.txt`, `Launch JobCollector Mac.command`, `Launch JobCollector Windows.bat` is missing, or its `__version__` isn't `version`. Test helper `make_release(tmp_path, version, extra: dict[str, bytes] = {}) -> Path` in the test file builds such a ZIP (top folder `talhaimran-JobCollector-abc123/`).

- [ ] **Step 1: Write the failing tests**

```python
def test_unpack_good_release(tmp_path):
    top = update.unpack(make_release(tmp_path, "0.3.0"), tmp_path / "new", "0.3.0")
    assert (top / "collector" / "__init__.py").exists()

@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "second-top/x.txt"])
def test_unpack_refuses_unsafe_layout(tmp_path, bad):
    z = make_release(tmp_path, "0.3.0", {bad: b"x"})
    with pytest.raises(update.UpdateError):
        update.unpack(z, tmp_path / "new", "0.3.0")
    assert not (tmp_path / "evil.txt").exists() and not any((tmp_path / "new").glob("**/*"))

def test_unpack_refuses_missing_launcher_or_wrong_version(tmp_path): ...  # both raise UpdateError
def test_unpack_refuses_a_file_that_is_not_a_zip(tmp_path): ...
```

(`bad` paths go in raw, not under the top folder, for the second-top case; the helper's `extra` keys are relative to the top folder except when they start with `/`, `..` or `second-top/`.)

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_update.py -q -k unpack` — Expected: FAIL (`unpack` missing).
- [ ] **Step 3: Implement** `UpdateError` and `unpack`: validate every `ZipInfo.filename` before extracting anything; read `__version__` from `collector/__init__.py` text with a regex, not by importing. On any failure `shutil.rmtree(dest, ignore_errors=True)` then raise.
- [ ] **Step 4: Run** — Expected: PASS; full suite passes.
- [ ] **Step 5: Commit** `"Update: check the release ZIP's layout and version before using it"`

### Task 3: Install in place, with backup, packages and rollback

**Files:**
- Modify: `collector/update.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Consumes: `status`, `unpack`, `UpdateError` (Tasks 1–2); `db.run_lock`, `config.LOCK_PATH`.
- Produces: `update.KEEP: frozenset[str]` (the never-touched names; `*-resume.md` matched by `fnmatch`); `update.Busy(Exception)`; `update.install(root: Path = config.ROOT, release: dict | None = None) -> str` — `release` defaults to `status(force=True)`; returns a message: `"Already up to date (0.2.0)."` or `"Updated to 0.3.0. Close the Job Collector window and double-click the launcher again to finish."`. Internal `_download(url, to: Path)` (streamed `requests.get`) and `_pip(root)` (runs `[sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(root / "requirements.txt")]`, raises `UpdateError` on non-zero) so tests can replace them.

Swap algorithm (spec "Installing", steps 4–7): for each top-level name in the release not in `KEEP` — skip files whose bytes are identical; otherwise move the current one (if any) into `root/.update-backup/<old version>/`, then move the new one in, recording each move. If `requirements.txt` changed, `_pip(root)` then write the hash to `root/.venv/requirements.sha` (create `.venv` dir only if it exists already — if it doesn't, skip both). On any exception after the first move: undo recorded moves in reverse, raise `UpdateError`. Finally remove `.update-new/` and the ZIP and any older `.update-backup/*` folders.

- [ ] **Step 1: Write the failing tests** (each builds a fake app in `tmp_path/app` with `collector/__init__.py` at 0.2.0, an extra `collector/old_module.py`, both launchers, `requirements.txt`, plus person files `.env`, `data/jobs.db`, `candidate-profile.md`, `me-resume.md`; `_download` patched to copy a `make_release` ZIP; `_pip` patched to record calls)

```python
def test_install_replaces_app_files_and_keeps_the_persons(app, ...):
    msg = update.install(app, REL_030)
    assert "Updated to 0.3.0" in msg
    assert '"0.3.0"' in (app / "collector/__init__.py").read_text()
    assert not (app / "collector/old_module.py").exists()           # removed with the old folder
    assert (app / ".update-backup/0.2.0/collector/old_module.py").exists()
    for f in (".env", "data/jobs.db", "candidate-profile.md", "me-resume.md"): assert original bytes unchanged
    assert not (app / ".update-new").exists()

def test_release_shipping_person_files_does_not_overwrite_them(...)  # release extra {".env": b"X", "data/jobs.db": b"X"}
def test_unchanged_launcher_is_not_moved(...)                         # launcher inode / mtime unchanged
def test_changed_requirements_runs_pip_and_writes_hash(...)           # sha256(new requirements) == .venv/requirements.sha
def test_same_requirements_skips_pip(...)
def test_pip_failure_rolls_everything_back(...)                       # old __init__ back, old_module back, raises UpdateError
def test_move_failure_midway_rolls_back(monkeypatch, ...)             # shutil.move raises on the 2nd new name
def test_refuses_git_clone(...)                                       # app/.git exists → UpdateError mentioning "git pull", nothing downloaded
def test_refuses_while_a_check_runs(...)                              # hold db.run_lock(config.LOCK_PATH) → Busy, nothing downloaded
def test_already_up_to_date(...)                                      # release version == current → message, nothing downloaded
def test_keeps_only_the_latest_backup(...)                            # pre-existing .update-backup/0.1.0 removed
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_update.py -q -k install` — Expected: FAIL.
- [ ] **Step 3: Implement** `KEEP`, `Busy`, `install`, `_download`, `_pip`.
- [ ] **Step 4: Run** — Expected: PASS; full suite passes.
- [ ] **Step 5: Commit** `"Update: install the release in place, keep the person's files, back up and roll back"`

### Task 4: Command line

**Files:**
- Modify: `collector/cli.py`
- Test: `tests/test_update.py`

**Interfaces:**
- Consumes: `update.status`, `update.install`, `update.Busy`, `update.UpdateError`, `collector.__version__`.
- Produces: `collector update [--check]`.

- [ ] **Step 1: Write the failing tests** via `cli.main(["update", "--check"])` with `update.status` patched: prints `Installed: 0.2.0` and `Latest: 0.3.0 (update available)` / `Latest: 0.2.0 (up to date)` / `Latest: unknown (couldn't reach GitHub)`. `cli.main(["update"])`: prints `install()`'s message, returns normally; `Busy` → `SystemExit(75)`; `UpdateError` → prints its message, `SystemExit(1)`.
- [ ] **Step 2: Run** `-k cli` — Expected: FAIL (invalid choice `update`).
- [ ] **Step 3: Implement** `cmd_update(args)` and the subparser (`--check` uses `status(force=True)`).
- [ ] **Step 4: Run** — PASS; full suite passes.
- [ ] **Step 5: Commit** `"collector update [--check] on the command line"`

### Task 5: Page API, and serve the page loaded at startup

**Files:**
- Modify: `collector/webui.py`
- Test: `tests/test_webui.py`

**Interfaces:**
- Consumes: `update.status`, `update.install`, `update.Busy`, `update.UpdateError`.
- Produces: `GET /api/update` → `update.status()`; `POST /api/update` → `{"ok": true, "message": str}`; `Busy` → 409 `{"error"}`; `UpdateError` → 400 `{"error"}` (map in `do_POST` beside the existing `Busy`/`ValueError` handlers). `GET /` serves bytes read once (module-level `PAGE_HTML`, read on first use and kept).

- [ ] **Step 1: Write the failing tests** with the existing `server` fixture and `call()` helper: GET `/api/update` returns the patched status; POST returns `install`'s message; POST with `install` raising `Busy` → 409; cross-site POST (bad Origin) → 403 and `install` not called; after the first `GET /`, changing `webui.PAGE`'s file contents doesn't change the next `GET /` response.
- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_webui.py -q -k update` — Expected: FAIL (404).
- [ ] **Step 3: Implement** the two routes and the page cache.
- [ ] **Step 4: Run** — PASS; full suite passes.
- [ ] **Step 5: Commit** `"Page API for updates; serve the page as loaded at startup"`

### Task 6: Version line, Update now button and banner on the page

**Files:**
- Modify: `collector/web/index.html`
- Test: `tests/test_webui.py`

**Interfaces:**
- Consumes: `GET/POST /api/update`.
- Produces: in `#tab-schedule`, a `<fieldset class="field" id="update-box">` with legend "Version": `#update-now` text (`Version 0.2.0`, or `Version 0.3.0 is available` + release-notes link), `button.btn.primary#update-go` "Update now" (hidden unless available), `#update-msg` with `role="status"`. A `#update-banner` (`role="status"`, hidden by default) above the views: "Version X is available. <a href="#settings/schedule">Update</a>". After success both show the install message and the button stays hidden.

- [ ] **Step 1: Write the failing test** `test_update_markup`: the HTML contains `id="update-box"`, `id="update-go"`, `id="update-banner"`, `role="status"` on both message elements, and `/api/update`.
- [ ] **Step 2: Run** `-k update_markup` — Expected: FAIL.
- [ ] **Step 3: Implement** markup + JS: on page load, `api("/api/update")` (errors ignored, nothing shown); the button disables itself and reads "Updating…" during the POST (`saving()` pattern), shows `error` text on failure. Use existing classes and tokens only.
- [ ] **Step 4: Run** — PASS; full suite passes. Then check in the browser with the sandbox copy (`scratchpad/launchtest`, port 8799, `update.status` faked via a cache file holding a newer `latest`): banner shows, link opens This computer, button visible; screenshot in light and dark.
- [ ] **Step 5: Commit** `"Settings → This computer: version, Update now, and a banner when a release is out"`

### Task 7: Docs

**Files:**
- Modify: `README.md` (Quick start step 1; a short "Updating" subsection under Quick start; `update` in the commands list), `CONTRIBUTING.md` (new "## Releasing": bump `collector/__init__.py` `__version__`, tag `v<version>`, publish the GitHub release)
- Test: `tests/test_launchers.py` (or a new small doc test)

- [ ] **Step 1: Write the failing test**: README contains "Releases" and `python -m collector update`, and no longer "Code → Download ZIP"; CONTRIBUTING contains `__version__`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Write** the docs: download from **Releases → latest → Source code (zip)**; Updating: Settings → This computer → **Update now**, or `.venv/bin/python -m collector update` (Windows `.venv\Scripts\python`), then close and reopen the launcher; your data stays; v0.1.0 users download v0.2.0 once by hand; old files are kept in `.update-backup/`.
- [ ] **Step 4: Run** — PASS; full suite passes.
- [ ] **Step 5: Commit** `"README and CONTRIBUTING: download from Releases, how to update, how to release"`; push `self-update`, open the PR (don't merge). Note in the PR: Windows not verified, and v0.2.0 must be tagged and released for the check to find it.
