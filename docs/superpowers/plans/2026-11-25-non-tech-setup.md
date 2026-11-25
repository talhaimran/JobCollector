# Non-tech Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A non-technical person downloads the ZIP, double-clicks a launcher, and sets everything up in the browser (data folder, job search, email, schedule, first search) without a terminal.

**Architecture:** Two launcher scripts bootstrap the venv and start the existing local web UI. A new `collector/folder.py` owns the data folder (profile + database + logs) and the native folder dialog; `config.use_folder()` re-points every path at runtime. The existing single-page UI gains a `#setup` wizard that reuses `/api/config`, plus `/api/folder*` and `/api/run` endpoints.

**Tech Stack:** Python 3.10+ stdlib (`http.server`, `subprocess`, `sqlite3`, `shutil`), existing deps only; plain HTML/CSS/JS in `collector/web/index.html`; bash (`.command`) and cmd (`.bat`).

**Spec:** the decisions below (agreed in conversation on 2026-11-25).

## Decisions (the spec)

1. Launcher scripts now (no PyInstaller app): `Start Job Collector.command` (Mac), `Start Job Collector.bat` (Windows).
2. The chosen folder holds **profile, database and logs**. `.env` (Gmail app password) stays in the app folder.
3. Folder picker = **native OS dialog** opened by the local server.
4. Email step is **skippable, with a warning that the system will not send any updates**.
5. On-screen renames are **included** in this work.
6. Changing folder never deletes anything. Target already has data → "use it"; otherwise "copy my data there" or "start fresh there".

## Global Constraints

- No new dependencies; no AI/LLM calls; UI listens on 127.0.0.1 only (keep `Handler._allowed`).
- Never delete or overwrite user data; never commit `.env`, the database or the profile.
- Python floor for launchers: **3.10** (python-jobspy 1.4.1 needs it).
- Profile file keys (`settings.LABELS`) do not change; renames are on-screen only.
- Default folder offered to new users: `~/Documents/Job Collector`.
- Each part ships as its own branch + PR from `main`: Part A `launchers`, Part B `data-folder`, Part C `first-run-setup`.
- UI work follows ui-ux-pro-max: visible labels, errors under the field (`aria-describedby`), one primary button per screen, focus moves to the step heading on step change, ≥44px targets, `prefers-reduced-motion` respected, light + dark checked.

## Review Focus

1. Folder paths with spaces / non-ASCII (`~/Documents/Job Collector`, `OneDrive - Personal`) → work everywhere (Task 4 test).
2. Target folder already holds `data/jobs.db` (synced from another computer) → never overwritten; only "use" is allowed (Task 4 test).
3. Folder change while a run holds the lock → refused with "A check is running; try again in a minute." (Task 4 test).
4. Native dialog cancelled or unavailable → `{"path": null}`; page says "No folder chosen" and shows a paste-a-path box as fallback (Task 4 + Task 5).
5. Launcher started twice (UI already on port 8765) → second launch just opens the browser, no traceback (Task 1 test).

---

## Part A — Launchers and quick start (branch `launchers`)

### Task 1: `serve()` reuses a running UI

**Files:** Modify `collector/webui.py` (`serve`); Test `tests/test_webui.py`

**Interfaces:** Produces `serve(port=8765, open_browser=True) -> None` that, when the port is taken by our UI, opens the browser at it and returns.

- [ ] **Step 1: Failing test** `test_serve_when_port_busy_opens_browser_and_returns`: bind a socket on a free port, monkeypatch `webbrowser.open` to record; call `webui.serve(port)`; assert it returns and recorded `[f"http://127.0.0.1:{port}/"]`.
- [ ] **Step 2:** `pytest tests/test_webui.py -k port_busy -v` → FAIL (`OSError: Address already in use`).
- [ ] **Step 3:** In `serve`, catch `OSError` with `errno.EADDRINUSE` (Windows: `WSAEADDRINUSE` 10048) around `ThreadingHTTPServer(...)`; print "Job Collector is already open in your browser." and open the URL.
- [ ] **Step 4:** test passes; full `pytest -q` passes.
- [ ] **Step 5:** commit.

### Task 2: Launcher scripts + quick start

**Files:** Create `Start Job Collector.command` (mode 755), `Start Job Collector.bat` (CRLF via existing `.gitattributes`; add `*.bat text eol=crlf`); Modify `README.md` (Quick start, new "For developers" section), `.gitattributes`; Test `tests/test_launchers.py`

Both scripts, in order:
1. `cd` to the script's own folder.
2. Find Python ≥3.10 (`python3` on Mac; `py -3` then `python` on Windows). Missing/too old → print one line ("Job Collector needs Python 3.10 or newer. Opening the download page…"), open `https://www.python.org/downloads/`, wait for a key, exit. Windows text adds: "tick *Add python.exe to PATH* when installing".
3. No `.venv` → create it.
4. Install when `.venv/requirements.sha` differs from the hash of `requirements.txt` (`shasum -a 256` / `certutil -hashfile … SHA256`): print "Installing (first time takes a few minutes)…", `pip install -r requirements.txt`, write the stamp. Failure → print "Install failed. Check your internet connection and double-click again." and wait for a key.
5. Run `<venv python> -m collector ui` (Task 1 handles "already running").
6. Print "Keep this window open while you use Job Collector. Close it to stop the page (scheduled checks keep running)."

- [ ] **Step 1: Failing test** `test_launchers_exist_and_parse`: both files exist; `.command` is executable (`os.access`); `bash -n` on it exits 0; `.bat` contains `-m collector ui` and `requirements.sha`.
- [ ] **Step 2:** run → FAIL (files missing).
- [ ] **Step 3:** write both scripts.
- [ ] **Step 4:** test passes. Manual on this Mac: delete a scratch copy's `.venv`, double-click → venv built, browser opens; double-click again → browser opens, no reinstall.
- [ ] **Step 5:** README Quick start becomes: 1. Download ZIP (Code → Download ZIP), unzip. 2. Double-click the launcher (Mac first time: right-click → Open; Windows SmartScreen: More info → Run anyway). 3. Follow the setup screens. Move today's terminal steps under "For developers". Commit, push, PR.

---

## Part B — Data folder (branch `data-folder`)

### Task 3: Paths follow the folder; logs written by the app

**Files:** Modify `collector/config.py`, `collector/cli.py`, `launchd/com.jobcollector.collector.plist`, `windows/run-collector.cmd`, `collector/schedule.py` (logs dir); Test `tests/test_folder.py`

**Interfaces:** Produces `config.use_folder(path: Path) -> None` setting `JOB_FOLDER`, `PROFILE_PATH = folder/"candidate-profile.md"`, `DATA_DIR = folder/"data"`, `DB_PATH`, `LOCK_PATH`, `LOGS_DIR = folder/"logs"`; called at import with `JOB_FOLDER` env or `ROOT` (so default layout = today's). `config.LEGACY_DB = ROOT/"data"/"jobs.db"`. Produces `folder.migrate_legacy() -> str | None` (in Task 4's module, written here): when `DB_PATH` is missing, `LEGACY_DB` exists and differs → copy it with `sqlite3` backup API; returns a message.

- [ ] **Step 1: Failing tests** `test_use_folder_repoints_paths` (paths under tmp folder), `test_legacy_db_copied_once` (legacy DB with one job → after `migrate_legacy()` the new DB has it, legacy still exists, second call returns None).
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** implement; `cli.main` calls `migrate_legacy()` before any command; `cmd_run` adds a `FileHandler(LOGS_DIR/"collector.log")` (mkdir first). Plist `StandardOutPath` → `/dev/null` (the app logs itself; stderr stays at `__ROOT__/logs/collector.err.log` for crashes); cmd file → `>nul 2>>logs\collector.err.log`.
- [ ] **Step 4:** tests + full suite pass; `python -m collector run --dry-run` writes to `<folder>/logs/collector.log`.
- [ ] **Step 5:** commit.

### Task 4: `collector/folder.py`

**Files:** Create `collector/folder.py`; Test `tests/test_folder.py`

**Interfaces (produces):**
- `describe(path: str) -> dict` → `{"path": str, "has_data": bool, "problem": str | None}`; `has_data` = profile or `data/jobs.db` exists; `problem` for: not absolute, parent missing, not writable.
- `switch(path: str, mode: str) -> str` with `mode in {"use", "copy", "fresh"}`: validates via `describe`; `mode="copy"` or `"fresh"` on a folder with data → `ValueError("That folder already has Job Collector data. Choose 'Use it'.")`; lock held (`db.run_lock` not acquired) → `ValueError("A check is running; try again in a minute.")`; `copy` copies the profile (if any) and the DB (sqlite backup); then writes `JOB_FOLDER` to `.env` (`webui.update_env`), calls `config.use_folder`, re-applies the schedule if one is installed (`schedule.installed_hours()` not None → `schedule.install`). Returns a one-line message.
- `choose_native(start: str | None) -> str | None`: Mac `osascript -e 'POSIX path of (choose folder with prompt "Choose where Job Collector keeps your data")'`; Windows PowerShell `System.Windows.Forms.FolderBrowserDialog`; cancel → None; tool missing → `RuntimeError`.

- [ ] **Step 1: Failing tests:** `test_copy_into_folder_with_spaces` (`tmp/"Job Collector – é"`), `test_existing_data_never_overwritten`, `test_switch_refused_while_locked`, `test_fresh_leaves_old_data`, `test_choose_native_cancel_returns_none` (monkeypatched `subprocess.run` returning code 1 / empty stdout).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** pass. **Step 5:** commit.

### Task 5: Folder API + Settings field

**Files:** Modify `collector/webui.py`, `collector/web/index.html`; Test `tests/test_webui.py`

**Interfaces:** `POST /api/folder/choose` → `{"path": str | null}` (500 → page shows fallback box); `GET /api/folder?path=` → `describe()`; `POST /api/folder {path, mode}` → `{**read_config(), "message": str}`; `read_config()` gains `"job_folder"`.

UI: the **Schedule** tab becomes **This computer** and holds Schedule plus "Data folder": current path (mono), "Choose folder…" button, then the chosen path with either one primary button "Use the data already there" or two buttons "Copy my data there" (primary) / "Start fresh there". Applies immediately with a status line; the main Save button is not involved.

- [ ] **Step 1: Failing tests** for the three endpoints (choose mocked) and `read_config()["job_folder"]`.
- [ ] **Step 2–4:** implement, pass; browser check: choose (mock cancelled), copy to a tmp folder, page reloads settings from the new folder; light + dark screenshots.
- [ ] **Step 5:** README "Data folder" section; commit, push, PR.

---

## Part C — First-time setup (branch `first-run-setup`)

### Task 6: Partial saves, optional email, plain Gmail errors

**Files:** Modify `collector/webui.py` (`save_config`, `send_test_email`), `collector/emailer.py`, `collector/pipeline.py`; Tests `tests/test_webui.py`, `tests/test_pipeline.py`

**Interfaces:** `save_config(body)` merges `{**read_config(), **body}` and `profile = {**current_profile, **body.get("profile", {})}` before validating (wizard sends one step at a time). `emailer.configured() -> bool` (all four email settings set). Pipeline: not configured → no send, `log.info("Email not set up: %d matches are on the Jobs found page", n)`, no `errors["email"]`. `send_test_email` turns `smtplib.SMTPAuthenticationError` into `ValueError("Gmail didn't accept that password. Use an app password (16 letters from Google), not your normal Gmail password.")`.

- [ ] **Step 1: Failing tests** `test_partial_save_keeps_other_settings`, `test_run_without_email_records_no_error`, `test_test_email_auth_error_is_plain`.
- [ ] **Step 2–4:** implement, pass. **Step 5:** commit.

### Task 7: Run now

**Files:** Modify `collector/webui.py`; Test `tests/test_webui.py`

**Interfaces:** `POST /api/run` → starts `[sys.executable, "-m", "collector", "run"]` (cwd `ROOT`) unless one started here is still running or the lock is held → 409 `{"error": "A check is already running."}`; remembers the log file size at start. `GET /api/run` → `{"running": bool, "lines": [last 8 new log lines without timestamps], "run": latest runs row or null}`.

- [ ] **Step 1: Failing tests** with `subprocess.Popen` monkeypatched: start → 200; second start → 409; status returns only lines appended after start.
- [ ] **Step 2–4:** implement, pass. **Step 5:** commit.

### Task 8: On-screen renames

**Files:** Modify `collector/web/index.html`, `README.md` (where it names page labels)

Exact copy: nav "Audit" → **Jobs found** (h1 too); tab "Scoring" → **Match score**; "Target titles" → **Job titles to look for**; "Exclude titles" → **Skip titles containing**; "Remote in" → **Remote jobs allowed in**; "Dealbreaker keywords" → **Skip jobs that mention**; "Excluded companies" → **Skip these companies**; "Must-have keywords" → **Keywords that raise the match score**; "Email cutoff" → **Lowest match score to email**; "Gmail login (SMTP user)" → **Gmail address used to send**; remaining "pre-score" text → "match score". Hints that say "Saved in your profile as …" keep the file key name.

- [ ] **Step 1:** `test_webui.py::test_page_uses_plain_labels` asserts the new strings are in `index.html` and "Dealbreaker keywords" is not. **Step 2–4:** FAIL → edit → PASS. **Step 5:** commit.

### Task 9: Setup wizard

**Files:** Modify `collector/web/index.html`; Test `tests/test_webui.py` (markup presence) + browser verification

Route `#setup/<1-5>`; shown automatically when `read_config().profile_exists` is false; "Run setup again" link in Settings. Layout: centred card (max 640px), "Step N of 5" + progress bar, Back (secondary) / Next (primary), "Set up later" link to Settings on every step. Each Next saves via `POST /api/config` (or `/api/folder`) and shows errors under the field.

1. **Welcome + folder:** one sentence on what it does; folder defaults to `~/Documents/Job Collector`, "Choose folder…" (Task 5 flow).
2. **What job:** job titles, search location + radius (km hint), work model as three large toggle buttons. Other filters keep defaults.
3. **Email:** send-to address, Gmail address, app password with show/hide toggle, numbered 3-line help + link `https://myaccount.google.com/apppasswords`, "Send test email". "Skip email" opens a confirm: **"Skip email? Job Collector won't send you any updates. You'll only see new jobs when you open this page."** — buttons "Skip email" / "Go back". After skipping, the Email tab shows the same warning as a banner until email is set.
4. **How often:** "Check for new jobs every [2] hours while this computer is on" (select 1, 2, 3, 4, 6, 12); platform from `schedule.this_platform()`; Next saves and installs the schedule.
5. **First search:** "Find jobs now" → `POST /api/run`, poll `GET /api/run` every 2 s, show the new log lines in a live region (`aria-live="polite"`), then "Found N new jobs, M match your settings" + primary "See jobs" → `#audit`.

- [ ] **Step 1:** markup test (`id="setup"`, five `data-step` sections, the exact skip-warning text). **Step 2–4:** build; browser-verify all steps with a temp `JOB_FOLDER`, keyboard-only pass, 375px width, dark mode, reduced motion.
- [ ] **Step 5:** README Quick start step 3 points at the setup screens; screenshots; commit, push, PR.
