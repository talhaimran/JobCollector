# Update to the latest release — design

## Why

Most people install Job Collector by downloading the ZIP from GitHub and double-clicking a launcher. They have no git,
so today the only way to get a new version is to download it again and move their data across by hand, which is easy to
get wrong (the app folder holds `.env` with the Gmail app password, the database and the profile by default). Releases
are now published on GitHub (v0.1.0 is the first), so people should be able to move to the latest one from the settings
page or with one command.

## Goals

- See the installed version and whether a newer release exists: on the page and on the command line.
- Install the latest release in place, from the page (**Update now**) or with `python -m collector update`.
- Keep everything that is the person's: `.env`, `.venv/`, `data/`, `logs/`, `candidate-profile.md`, `*-resume.md`, and
  anything else not shipped in a release.
- Keep the schedule working: the folder path doesn't change, so the installed launchd job / Task Scheduler task keeps
  pointing at the same `.venv` and project folder.
- An update can be undone: the replaced files are kept in `.update-backup/<old version>/`.

## Non-goals

- Updating a git clone. If the folder has `.git`, `update` refuses and says to use `git pull`.
- Picking an older version, pre-releases, or rolling back from the page (undo is by hand from `.update-backup/`).
- Updating while the app is closed, or in the background on a timer. Scheduled job checks never check for updates.
- Restarting the page by itself. After an update the page asks the person to close the launcher window and open it
  again; the launcher then reinstalls packages if `requirements.txt` changed (it already does this via
  `.venv/requirements.sha`).
- Reinstalling the schedule. If a future release changes the schedule files, that release says so in its notes.

## Anti-goals (a failure even if it "works")

- Losing or overwriting any of the person's files, or leaving the app half-updated with no way back.
- Updating in the middle of a job check.
- Any sign-in, any AI/LLM call, or any request other than to GitHub's public API and its download of the release.
- Checking GitHub on every page load or from scheduled runs.

## Constraints

- Phase 1 rules hold: no AI calls, no sign-ins, polite network use, no secrets in commits.
- Network: `GET https://api.github.com/repos/talhaimran/JobCollector/releases/latest` (no token), then the release's
  `zipball_url`. HTTPS only. Timeouts as for the job sources (`config.HTTP_TIMEOUT`).
- Must work on macOS and Windows with only what is already installed (`requests`, the standard library's `zipfile`).
- The page server is local-only and its POSTs already need the Origin / JSON checks; the update POST uses the same.

## Approaches considered

1. **Replace the app in place from the release ZIP (picked).** Works for ZIP installs, keeps the folder path (so the
   schedule and data stay put), and the launcher's existing package check covers new dependencies.
2. **`git pull`.** Only works for clones; ZIP users have no git. Kept only as the refusal message for clones.
3. **Only tell people a new version exists**, linking to the download. Smallest, but leaves the risky by-hand move of
   `.env` and the database to every person.

## Design

### Version

- `collector/__init__.py` gets `__version__ = "0.2.0"` (decided). The existing v0.1.0 release is an older codebase with
  no launchers or update code, so v0.2.0 is the first release that can update itself; people on v0.1.0 download once
  more by hand. A release's tag is `v` + that string; the release checklist in CONTRIBUTING gains "bump `__version__`
  before tagging".
- The README's download step points to **Releases → latest → Source code (zip)** instead of **Code → Download ZIP**,
  so an install always matches a release (decided).
- Versions compare as tuples of integers (`"0.10.0" > "0.9.1"`). A tag that doesn't parse is treated as "no update".

### Checking (`collector/update.py`)

- `latest() -> dict | None`: asks GitHub's releases/latest API; returns `{version, notes_url, zip_url}` or `None` on any
  error (offline, rate-limited, bad JSON). Never raises.
- The page caches the answer for 24 hours in `data/update-check.json` in the data folder (checked time + result), so it
  asks GitHub at most once a day, and only when the Settings page loads. `--check` on the command line always asks.

### Installing

`install(release) -> str` (message for the person), run under the existing run lock (`db.run_lock(config.LOCK_PATH)`):
if a job check holds it, refuse with "A job check is running; try again when it finishes."

1. Refuse if the folder has `.git` (use `git pull`).
2. Download the ZIP to a temporary file in the project folder (same drive, so later renames are cheap). Check it:
   it is a valid ZIP with one top folder that contains `collector/__init__.py`, `requirements.txt` and both launchers,
   and no entry escapes that folder (no absolute paths, no `..`). Its `__version__` must equal the release's.
3. Extract to `.update-new/`.
4. The release owns exactly the top-level names it ships, minus a fixed keep-list that is never touched even if a
   release contained it: `.env`, `.venv`, `data`, `logs`, `candidate-profile.md`, `*-resume.md`, `.git`,
   `.update-backup`, `.update-new`. For each owned name: move the current one (if any) into
   `.update-backup/<old version>/`, then move the new one in. Whole folders are swapped, so files a release removed
   (an old module) go away with the old folder.
   A file whose bytes are identical in both versions is left where it is (this keeps the launchers untouched in most
   releases; see the Windows note).
5. If `requirements.txt` changed, install the packages now with the app's own Python
   (`sys.executable -m pip install --disable-pip-version-check -q -r requirements.txt`) and write the new hash to
   `.venv/requirements.sha` exactly as the launchers do (SHA-256 hex of `requirements.txt`). Scheduled checks start
   the new code without going through the launcher, so they need the packages before the next run.
   *(Builder check, my call: install here rather than leave it to the launcher.)*
6. If any move or the package install fails, put everything back from the backup and report the error; the app stays
   on the old version. (Packages pip added are left installed; old code ignores them.)
7. Remove `.update-new/` and the downloaded ZIP. Keep only the latest backup (delete older `.update-backup/*`).

The page server keeps running the old code it already loaded; only a restart picks up the new code. The server reads
`index.html` once at startup instead of on every request, so a reload before restarting doesn't pair the new page with
the old API. *(Builder check, my call.)*

### Page

- **Settings → This computer** shows "Version 0.1.0". When a newer release is known: "Version 0.2.0 is available",
  a link to its release notes, and **Update now**. While installing, the button shows progress and is disabled.
- A one-line banner across the top of every view when an update is available, linking to that section.
- After success: "Updated to 0.2.0. Close the Job Collector window and double-click the launcher again to finish."
  The banner changes to that message until the page is reloaded by the new server.
- API: `GET /api/update` → `{current, latest, notes_url, available}` (uses the 24-hour cache);
  `POST /api/update` → runs `install`, returns `{ok, message}`.

### Command line

- `python -m collector update --check`: prints the installed and latest versions.
- `python -m collector update`: installs the latest release if newer, printing what it did; exits 0 when updated or
  already current, 1 on error, 75 when a job check holds the lock (same code `run` uses).

### Windows launcher note

`cmd.exe` reads a running `.bat` file as it goes, so replacing `Launch JobCollector Windows.bat` while its window is
open could make that window run garbled lines when the server exits (cmd reopens the file by path at its old offset).
Mitigations: unchanged files aren't replaced (step 4), so the launcher is only swapped in a release that changes it;
only one line (`pause`) follows the `ui` line; and the success message says to close the window rather than press a
key in it. Not verifiable on this Mac; needs a Windows check before release. *(Builder check, my call.)*

## Left to the builder

- Exact wording and placement of the banner and the This computer section, following the existing page styles.
- How the page shows progress while installing (the download is a few hundred KB).
- Test layout: fake GitHub responses and a small in-memory ZIP; the existing `no_network` fixture keeps tests offline.

## Risks

- GitHub releases aren't signed. Protection is HTTPS to github.com plus the layout and version checks above.
- A person who edited the app's own files (not their data) loses those edits to the update; they're in the backup.
