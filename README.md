# Job Collector (Phase 1)

Every 2 hours this collector finds job postings from the last 24 hours, filters them and gives each a keyword pre-score. It uses no AI. Every job goes into SQLite, and new matches are emailed to you. It never logs in to any site.

**Sources:** JobSpy (LinkedIn, Indeed, Glassdoor) and the public Greenhouse, Lever and Ashby feeds of your "Target companies". ZipRecruiter (blocked from Canada) and Google for Jobs (needs a real browser) were dropped.

## Setup

```bash
cd /path/to/JobCollector
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

`.env`:

| key | meaning |
|---|---|
| `JOB_FOLDER` | folder containing `candidate-profile.md` (defaults to this project folder) |
| `EMAIL_FROM`, `EMAIL_TO`, `SMTP_USER` | your Gmail address(es) |
| `SMTP_APP_PASSWORD` | a Gmail **app password** (Google Account → Security → 2-Step Verification → App passwords) |
| `RESULTS_WANTED` | results per JobSpy query (default 25) |
| `SEND_WHEN_EMPTY` | `true` to get an email even with 0 matches (default `false`) |
| `SCORE_WEIGHT_KEYWORDS`, `KEYWORD_FULL_MARKS`, `TITLE_EXACT_SCORE`, `TITLE_CONTAINED_SCORE` | pre-score tuning (defaults 0.7, 10, 100, 70); easiest to set from the settings page |

Only the `## Job Search Settings` section of the profile is read. Leaving a setting blank turns that filter off, and each email lists the filters that were off.

## Commands

```bash
.venv/bin/python -m collector settings        # show parsed settings
.venv/bin/python -m collector run --dry-run   # everything except sending; prints the email
.venv/bin/python -m collector run             # real run
.venv/bin/python -m collector report          # last 10 runs + which source is fastest
.venv/bin/python -m collector ui              # audit + settings page at http://127.0.0.1:8765
.venv/bin/pytest -q                           # tests (no network)
```

A dry run still saves jobs to the database. Those jobs count as "seen", so a later real run won't email them again.

A run searches each location and "Remote in" country once per search title. Target titles that contain another target title are not searched separately, so your 7 titles become one "Project Manager" search. With 3 sites that is 12 queries with a 3–8 s pause between each, about 3–5 minutes. LinkedIn descriptions add one request per LinkedIn job. When a site errors, the collector waits 30 s, then 60 s, before retrying. After 3 errors in a row it skips that site for the rest of the run.

## Pipeline

1. **Normalize** every posting into one shape (see `collector/normalize.py`).
2. **De-duplicate:** a job already stored with the same `(source, source_job_id)` or URL is skipped. Otherwise the collector builds a fingerprint from company + title + city, with suffixes like Inc and Ltd stripped. If an earlier row (from any source, reposts included) has the same fingerprint, the new row is saved with `duplicate_of` set and is never emailed.
3. **Hard filters**, in order. The first failure is saved in `filter_reason`:
   1. excluded company
   2. exclude titles (whole word)
   3. target titles (whole word)
   4. work model and location:
      - Remote jobs pass unless their location names a country outside "Remote in".
      - Hybrid and on-site jobs must match one of your "Locations".
      - Jobs with an unknown work model must match a location and are flagged in the email.
      - Work models you didn't list (e.g. on-site) fail.
   5. employment type (an unknown type passes)
   6. salary floor: hourly ×2080, daily ×260, weekly ×52 and monthly ×12. Only CAD (or no currency) is checked. Other currencies pass and are flagged in the email.
   7. dealbreaker phrases in the title or description.
4. **Pre-score** (0–100), for jobs that pass.
5. **Save** everything, filtered-out jobs included.
6. **Email** only new, passing, non-duplicate jobs scoring at least "Email cutoff".

## How the pre-score works

```
keywords = min(100, matched_must_have / KEYWORD_FULL_MARKS * 100)     # default 10 matches = 100
title    = 100 if title == a target title
           70  if a target title appears inside the title
           0   otherwise
total    = keywords * 0.70 + title * 0.30
```

Keywords match case-insensitively as whole words or phrases in the title and description, so `AI/ML` and `Monday.com` work. When "Must-have keywords" is blank, total = title score.

**Tuning:** run `python -m collector ui` and open **Settings**. Move the keyword/title slider, the full-marks count, the two title scores or the email cutoff, and the preview re-scores recent passing jobs and shows which would be emailed, before you save. Saving writes the scoring values to `.env` and the cutoff to your profile. Defaults live in `SCORE_DEFAULTS` in `collector/config.py`. Saved changes apply from the next run; scores already in the database don't change.

Note that Glassdoor returns no descriptions (JobSpy 1.4.1), so Glassdoor jobs score on title only and dealbreakers are checked against the title alone.

## Checking the filters

```bash
sqlite3 data/jobs.db "SELECT filter_reason, COUNT(*) FROM jobs WHERE filter_passed=0 GROUP BY 1 ORDER BY 2 DESC"
sqlite3 data/jobs.db "SELECT title, company, location, work_model, filter_reason FROM jobs WHERE filter_passed=0 ORDER BY id DESC LIMIT 30"
sqlite3 data/jobs.db "SELECT prescore_total, title, company, matched_keywords FROM jobs WHERE filter_passed=1 ORDER BY id DESC LIMIT 30"
```

## Audit and settings page

`.venv/bin/python -m collector ui` serves a page on `http://127.0.0.1:8765`. It listens only on your Mac and rejects requests from other sites. Stop it with Ctrl+C.

- **Audit**: every saved job, filtered by date found or date posted (Today, Since yesterday, 7 days, 30 days, All, or a custom range). Click a rejection reason to list those jobs, and click *description* to read the text the keyword and dealbreaker checks saw. *Recent runs* lists per-source counts and errors.
- **Settings**: pre-score weights, email cutoff, Gmail addresses and app password, and *Send test email*. The app password is never sent back to the page: leave the field blank to keep the saved one.

## Scheduling (launchd, every 2 hours)

Install:

```bash
mkdir -p logs && sed "s|__ROOT__|$PWD|g" launchd/com.jobcollector.collector.plist > ~/Library/LaunchAgents/com.jobcollector.collector.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jobcollector.collector.plist
```

Run it now and watch the log:

```bash
launchctl kickstart gui/$(id -u)/com.jobcollector.collector
tail -f logs/collector.log
```

Uninstall:

```bash
launchctl bootout gui/$(id -u)/com.jobcollector.collector
rm ~/Library/LaunchAgents/com.jobcollector.collector.plist
```

A lock file (`data/collector.lock`) stops two runs from overlapping. The OS releases the lock if a run crashes. When the Mac is asleep, launchd runs the missed job once it wakes.

**macOS privacy:** this project lives under `~/Documents`, which macOS protects. If `logs/collector.err.log` shows `Operation not permitted`, there are two fixes:
- Give `.venv/bin/python`'s target (`/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`) Full Disk Access in System Settings → Privacy & Security.
- Or move the project out of `~/Documents`.

## Test checklist (definition of done)

1. `collector settings` prints the settings from your profile correctly.
2. `collector run --dry-run` completes and logs job counts per source in `runs`. Any source errors are logged without stopping the run.
3. Running it again right away finds 0 new jobs.
4. A real run sends you one email with correct links, and a run with nothing new sends nothing.
5. `jobs` shows a clear `filter_reason` for every job that was filtered out (see the queries above).
6. The launchd job runs on schedule and writes to `logs/`.

## Layout

```
collector/
  config.py        knobs: weights, delays, sites, results per query
  settings.py      Job Search Settings parser
  sources/         jobspy_src.py, feeds.py (Greenhouse/Lever/Ashby), shared HTTP retry
  normalize.py     one job shape, work-model detection, phrase matching
  dedupe.py        fingerprints
  filters.py       hard filters
  prescore.py      keyword pre-score
  db.py            SQLite schema, lock
  emailer.py       HTML + text email, Gmail SMTP
  pipeline.py      a run, end to end
  cli.py           run / settings / report
```
