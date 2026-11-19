"""All tunable knobs in one place."""
import os
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
JOBSPY_SITES = ["linkedin", "indeed", "glassdoor"]
RESULTS_WANTED = int(os.getenv("RESULTS_WANTED", "25"))
HOURS_OLD = 24
COUNTRY_INDEED = "Canada"
LINKEDIN_FETCH_DESCRIPTION = True
DELAY_SECONDS = (3, 8)       # random pause between queries
BACKOFF_BASE_SECONDS = 30    # after an error: 30s, 60s, 120s ... per site
MAX_SITE_ERRORS = 3          # consecutive errors before a site is skipped for the run

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
