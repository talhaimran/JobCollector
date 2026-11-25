"""All tunable knobs in one place."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

JOB_FOLDER = Path(os.getenv("JOB_FOLDER") or ROOT).expanduser()
PROFILE_PATH = JOB_FOLDER / "candidate-profile.md"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "jobs.db"
LOCK_PATH = DATA_DIR / "collector.lock"

# --- Pre-score: tune in .env or with `python -m collector ui` --------------
# .env key -> default. The settings page edits exactly these keys.
SCORE_DEFAULTS = {
    "SCORE_WEIGHT_KEYWORDS": 0.70,  # title weight = 1 - this
    "KEYWORD_FULL_MARKS": 10,       # this many must-have matches = 100 keyword score
    "TITLE_EXACT_SCORE": 100,       # job title == a target title
    "TITLE_CONTAINED_SCORE": 70,    # target title appears inside a longer title
}
_kw_weight = float(os.getenv("SCORE_WEIGHT_KEYWORDS", SCORE_DEFAULTS["SCORE_WEIGHT_KEYWORDS"]))
SCORE_WEIGHTS = {"keywords": _kw_weight, "title": round(1 - _kw_weight, 4)}
KEYWORD_FULL_MARKS = int(os.getenv("KEYWORD_FULL_MARKS", SCORE_DEFAULTS["KEYWORD_FULL_MARKS"]))
TITLE_EXACT_SCORE = int(os.getenv("TITLE_EXACT_SCORE", SCORE_DEFAULTS["TITLE_EXACT_SCORE"]))
TITLE_CONTAINED_SCORE = int(os.getenv("TITLE_CONTAINED_SCORE", SCORE_DEFAULTS["TITLE_CONTAINED_SCORE"]))

# --- JobSpy ---------------------------------------------------------------
# dropped: zip_recruiter (403 from Canada), google (Google serves scripts a JavaScript-only page)
# linkedin has its own client (sources/linkedin.py) so its requests can be budgeted
JOBSPY_SITES = ["indeed", "glassdoor"]
RESULTS_WANTED = int(os.getenv("RESULTS_WANTED", "25"))
HOURS_OLD = 24
COUNTRY_INDEED = "Canada"
DELAY_SECONDS = (3, 8)       # random pause between queries
BACKOFF_BASE_SECONDS = 30    # after an error: 30s, 60s, 120s ... per site
MAX_SITE_ERRORS = 3          # consecutive errors before a site is skipped for the run

# --- LinkedIn (own client, see sources/linkedin.py) -------------------------
LINKEDIN_MIN_HOURS = float(os.getenv("LINKEDIN_MIN_HOURS", "4"))       # at most one sweep this often
LINKEDIN_REQUEST_BUDGET = int(os.getenv("LINKEDIN_REQUEST_BUDGET", "40"))  # search pages + job pages per sweep
LINKEDIN_MAX_PAGES = 5           # 10 jobs per page
LINKEDIN_DELAY_SECONDS = (5, 12)
LINKEDIN_DESCRIPTION_TRIES = 3   # give up on a job page after this many failed sweeps

# --- Randstad Canada and Robert Half (sources/randstad.py, roberthalf.py) -----
RANDSTAD_MAX_PAGES = 5           # 30 jobs per page; results come by relevance, not date
RANDSTAD_DELAY_SECONDS = (3, 8)
ROBERTHALF_MAX_PAGES = 4         # 25 jobs per page, newest first

# --- Company feeds --------------------------------------------------------
FEED_MAX_AGE_HOURS = 24
HTTP_TIMEOUT = 30

# --- Email ----------------------------------------------------------------
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_APP_PASSWORD = os.getenv("SMTP_APP_PASSWORD", "")
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
SEND_WHEN_EMPTY = os.getenv("SEND_WHEN_EMPTY", "false").lower() == "true"
# email jobs that pass the filters but have no description (score is title-only), tagged "partial score"
SEND_PARTIAL_SCORES = os.getenv("SEND_PARTIAL_SCORES", "false").lower() == "true"

# --- Schedule: `python -m collector schedule install` --------------------
SCHEDULE_PLATFORM = os.getenv("SCHEDULE_PLATFORM") or ("windows" if sys.platform == "win32" else "mac")
SCHEDULE_HOURS = float(os.getenv("SCHEDULE_HOURS", "2"))
