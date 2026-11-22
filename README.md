# Job Collector (Phase 1)

Every 2 hours this collector finds job postings from the last 24 hours, filters them and gives each a keyword pre-score. It uses no AI. Every job goes into SQLite, and new matches are emailed to you. It never logs in to any site.

**Sources:** LinkedIn (its own client, see below), JobSpy (Indeed, Glassdoor) and the public Greenhouse, Lever and Ashby feeds of your "Target companies". ZipRecruiter and hiring.cafe block scripts outright, and Google for Jobs needs a real browser, so they aren't used.

Runs on macOS and Windows.

## Setup

```bash
cd /path/to/JobCollector
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # then edit .env
```

On Windows use `py -m venv .venv`, `.venv\Scripts\pip` and `.venv\Scripts\python`, and `copy` instead of `cp`.

`.env`:

| key | meaning |
|---|---|
| `JOB_FOLDER` | folder containing `candidate-profile.md` (defaults to this project folder) |
| `EMAIL_FROM`, `EMAIL_TO`, `SMTP_USER` | your Gmail address(es) |
| `SMTP_APP_PASSWORD` | a Gmail **app password** (Google Account → Security → 2-Step Verification → App passwords) |
| `RESULTS_WANTED` | results per JobSpy query (default 25) |
| `SEND_WHEN_EMPTY` | `true` to get an email even with 0 matches (default `false`) |
| `SEND_PARTIAL_SCORES` | `true` to also email passing jobs that have no description, tagged *partial score* (default `false`) |
| `LINKEDIN_MIN_HOURS`, `LINKEDIN_REQUEST_BUDGET` | at most one LinkedIn sweep per 4 h, at most 40 requests each (defaults) |
| `SCHEDULE_PLATFORM`, `SCHEDULE_HOURS` | `mac` or `windows`, and how often to run (default 2 h); also on the settings page |
| `SCORE_WEIGHT_KEYWORDS`, `KEYWORD_FULL_MARKS`, `TITLE_EXACT_SCORE`, `TITLE_CONTAINED_SCORE` | pre-score tuning (defaults 0.7, 10, 100, 70); easiest to set from the settings page |

Only the `## Job Search Settings` section of the profile is read.

**Searching vs accepting a location.** Two separate settings:

| Profile line | Settings page | Used for |
|---|---|---|
| `Search locations: Kitchener ON` and `Search radius (miles): 25` | Search locations, Search radius | Where LinkedIn, Indeed and Glassdoor look. Each board applies the radius itself, so one city also covers its neighbours. Blank search locations fall back to Locations; a blank radius means 25 miles. |
| `Locations: Kitchener ON, Waterloo ON, Cambridge ON` | Accepted cities | The filter: hybrid and on-site jobs pass only in one of these cities. |

Search wide, accept narrowly. Jobs from inside the radius but outside your accepted cities (Guelph, Brantford, ...) are saved and shown in the audit as "Location not in your cities", so you can spot a city worth adding. You can edit it on the settings page (see below), which also creates the profile if you don't have one. Leaving a setting blank turns that filter off, and each email lists the filters that were off.

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

A run searches each location and "Remote in" country once per search title. Target titles that contain another target title are not searched separately, so "Senior Project Manager" is covered by the "Project Manager" search. Indeed and Glassdoor are searched every run with a 3–8 s pause between queries. When one errors, the collector waits 30 s, then 60 s. After 3 errors in a row it skips that site for the rest of the run.

### LinkedIn

LinkedIn rate-limits hard (429 "blocked" after a few dozen requests), so it has its own client with a request budget:

- **At most one sweep every 4 hours** (`LINKEDIN_MIN_HOURS`), whatever the run schedule.
- **At most 40 requests per sweep** (`LINKEDIN_REQUEST_BUDGET`): search pages and job pages both count. 5–12 s between requests.
- **Order:** first the job pages of jobs still missing a description, then the searches whose last success is oldest. A search that didn't finish stays oldest, so it goes first next time. Each search covers the time since its own last success plus an hour, so a skipped search misses nothing.
- **Search pages:** newest first, 10 jobs each, until a page has nothing new.
- **Job pages (descriptions) only for new jobs that pass** title, location, work model and company checks. Jobs over the budget are saved without a description and fetched next sweep (up to 3 tries).
- **The first 429 ends the sweep** with no retries. The next sweep comes after the usual 4 h; there's no growing back-off, since a blocked sweep costs only a request or two.
- Searches use your Search locations and radius, like Indeed and Glassdoor.

In the `runs` table, LinkedIn's count is new jobs only (known ones are dropped before parsing).

### Partial scores

A job with no description can't be keyword-scored or checked for dealbreakers, so its score is title-only and it's marked `score_partial`. Glassdoor never gives descriptions, and LinkedIn jobs over the budget wait for theirs. By default these aren't emailed until a description arrives. Turn on `SEND_PARTIAL_SCORES` (or the checkbox on the settings page) to email them straight away, tagged *partial score*, whatever the cutoff.

### Work model

Taken from the source's own field when there is one, then the title and location, then the description. In the description it looks for phrases such as "3 days a week in the office", "2–3 days on-site", "days in office", "Workplace type: Hybrid", "fully remote" and "fully on-site". Five days in the office counts as on-site. Plain "hybrid" next to words like *delivery*, *cloud* or *agile* is ignored.

## Pipeline

1. **Normalize** every posting into one shape (see `collector/normalize.py`).
2. **De-duplicate:** a job already stored with the same `(source, source_job_id)` or URL is skipped. Otherwise the collector builds a fingerprint from company + title + city, with suffixes like Inc and Ltd stripped. If an earlier row (from any source, reposts included) has the same fingerprint, the new row is saved with `duplicate_of` set and is never emailed.
3. **Hard filters**, in order. The first failure is saved in `filter_reason`:
   1. excluded company
   2. exclude titles (whole word)
   3. target titles (whole word)
   4. work model and location:
      - Remote jobs pass unless their location names a country outside "Remote in".
      - Hybrid and on-site jobs must match one of your "Locations" (accepted cities).
      - Jobs with an unknown work model must match a location and are flagged in the email.
      - Work models you didn't list (e.g. on-site) fail.
   5. employment type (an unknown type passes)
   6. salary floor: hourly ×2080, daily ×260, weekly ×52 and monthly ×12. Only CAD (or no currency) is checked. Other currencies pass and are flagged in the email.
   7. dealbreaker phrases in the title or description.
4. **Pre-score** (0–100), for jobs that pass.
5. **Save** everything, filtered-out jobs included.
6. **Email** only new, passing, non-duplicate jobs scoring at least "Email cutoff". Jobs without a description wait for one, or are emailed as *partial score* if you turn that on (see LinkedIn below). A LinkedIn job whose description arrives in a later sweep is filtered and scored again then.

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
- **Settings**: the job search settings (titles, locations, work model, salary floor, keywords, excluded companies), pre-score weights, email cutoff, Gmail addresses and app password, and *Send test email*. The app password is never sent back to the page: leave the field blank to keep the saved one. The preview re-runs the filters and scores on saved jobs with your unsaved values.

No profile yet? Fill in *Job search* and save: the page creates `candidate-profile.md` with just a `## Job Search Settings` section. If the profile has no such section, one is added at the end. Target companies and base resume are still edited in the file.

## Scheduling (macOS or Windows)

Pick the computer and the interval on the settings page (or `SCHEDULE_PLATFORM` and `SCHEDULE_HOURS` in `.env`), then install:

```bash
.venv/bin/python -m collector schedule install      # macOS: launchd, runs once now too
.venv/bin/python -m collector schedule status
.venv/bin/python -m collector schedule uninstall
```

On Windows run the same with `.venv\Scripts\python`. That creates a Task Scheduler task called *JobCollector* that runs `windows\run-collector.cmd` every N hours. Installing again after changing the interval replaces the old schedule. If `SCHEDULE_PLATFORM` doesn't match the computer, the command refuses.

Watch the log: `tail -f logs/collector.log` (macOS) or `type logs\collector.log` (Windows).

A lock file (`data/collector.lock`) stops two runs from overlapping on both systems. The OS releases the lock if a run crashes. When the Mac is asleep, launchd runs the missed job once it wakes. Task Scheduler skips runs missed while Windows was off unless you tick *Run task as soon as possible after a scheduled start is missed* in the task's settings.

**macOS privacy:** this project lives under `~/Documents`, which macOS protects. If `logs/collector.err.log` shows `Operation not permitted`, there are two fixes:
- Give `.venv/bin/python`'s target (`/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`) Full Disk Access in System Settings → Privacy & Security.
- Or move the project out of `~/Documents`.

## Test checklist (definition of done)

1. `collector settings` prints the settings from your profile correctly.
2. `collector run --dry-run` completes and logs job counts per source in `runs`. Any source errors are logged without stopping the run.
3. Running it again right away finds 0 new jobs.
4. A real run sends you one email with correct links, and a run with nothing new sends nothing.
5. `jobs` shows a clear `filter_reason` for every job that was filtered out (see the queries above).
6. The scheduled job (launchd or Task Scheduler) runs on schedule and writes to `logs/`.

## Layout

```
collector/
  config.py        knobs: weights, delays, sites, results per query
  settings.py      Job Search Settings parser
  sources/         linkedin.py (own client, request budget), jobspy_src.py (Indeed, Glassdoor),
                   feeds.py (Greenhouse/Lever/Ashby), shared HTTP retry
  normalize.py     one job shape, work-model detection, phrase matching
  dedupe.py        fingerprints
  filters.py       hard filters
  prescore.py      keyword pre-score
  db.py            SQLite schema, lock
  emailer.py       HTML + text email, Gmail SMTP
  pipeline.py      a run, end to end
  schedule.py      launchd / Task Scheduler install
  cli.py           run / settings / report / ui / schedule
launchd/           macOS plist template
windows/           run-collector.cmd for Task Scheduler
```
