"""SQLite storage."""
import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, source_job_id TEXT, url TEXT,
  title TEXT, company TEXT, location TEXT, work_model TEXT, employment_type TEXT,
  salary_min REAL, salary_max REAL, salary_currency TEXT, salary_interval TEXT,
  posted_at TEXT, found_at TEXT NOT NULL, description TEXT,
  fingerprint TEXT, duplicate_of INTEGER REFERENCES jobs(id),
  filter_passed INTEGER, filter_reason TEXT,
  prescore_total REAL, prescore_keywords REAL, prescore_title REAL, matched_keywords TEXT,
  emailed_at TEXT, status TEXT DEFAULT 'New', run_id INTEGER REFERENCES runs(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_source_id ON jobs(source, source_job_id);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_url ON jobs(url);
CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
  jobs_found TEXT, new_jobs INTEGER, passed_filters INTEGER, emailed INTEGER,
  errors TEXT, filters_off TEXT
);
"""

JOB_COLS = ["source", "source_job_id", "url", "title", "company", "location", "work_model",
            "employment_type", "salary_min", "salary_max", "salary_currency", "salary_interval",
            "posted_at", "found_at", "description", "fingerprint", "duplicate_of", "filter_passed",
            "filter_reason", "prescore_total", "prescore_keywords", "prescore_title",
            "matched_keywords", "run_id"]


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def run_lock(path):
    """Yields True if we got the lock, False if another run holds it. The OS frees it on crash."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def start_run(conn, started_at, filters_off) -> int:
    cur = conn.execute("INSERT INTO runs(started_at, filters_off) VALUES (?, ?)",
                       (started_at, json.dumps(filters_off)))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, **kw):
    for k in ("jobs_found", "errors"):
        kw[k] = json.dumps(kw[k])
    sets = ", ".join(f"{k} = ?" for k in kw)
    conn.execute(f"UPDATE runs SET {sets} WHERE id = ?", (*kw.values(), run_id))
    conn.commit()


def seen(conn, source, source_job_id, url) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE (source = ? AND source_job_id = ?) OR url = ? LIMIT 1",
                        (source, source_job_id, url)).fetchone() is not None


def fingerprint_owner(conn, fp) -> int | None:
    row = conn.execute("SELECT id FROM jobs WHERE fingerprint = ? AND duplicate_of IS NULL ORDER BY id LIMIT 1",
                       (fp,)).fetchone()
    return row["id"] if row else None


def insert_job(conn, job: dict) -> int:
    vals = [json.dumps(job[c]) if c == "matched_keywords" and job.get(c) is not None else job.get(c)
            for c in JOB_COLS]
    cur = conn.execute(f"INSERT INTO jobs({', '.join(JOB_COLS)}) VALUES ({', '.join('?' * len(JOB_COLS))})", vals)
    return cur.lastrowid


def mark_emailed(conn, ids, ts):
    conn.executemany("UPDATE jobs SET emailed_at = ? WHERE id = ?", [(ts, i) for i in ids])
    conn.commit()


def last_runs(conn, n=10):
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()


def posted_found_pairs(conn):
    return conn.execute("SELECT source, posted_at, found_at FROM jobs WHERE posted_at IS NOT NULL "
                        "AND duplicate_of IS NULL").fetchall()
