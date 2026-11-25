"""Local audit + settings UI: `python -m collector ui`. Listens on 127.0.0.1 only."""
import errno
import json
import logging
import os
import re
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import dotenv_values

from . import config, db, emailer, filters, prescore, schedule, settings

log = logging.getLogger(__name__)
ENV_PATH = config.ROOT / ".env"
PAGE = Path(__file__).parent / "web" / "index.html"
EMAIL_KEYS = ["EMAIL_FROM", "EMAIL_TO", "SMTP_USER"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LIST_FIELDS = ["target_titles", "exclude_titles", "search_locations", "locations", "remote_in", "work_model", "employment_type",
               "must_have", "dealbreakers", "excluded_companies"]
CHOICES = {"work_model": ["Remote", "Hybrid", "On-site"], "employment_type": ["Full-time", "Contract", "Part-time"]}
SECTION = "## Job Search Settings\n"
JOB_FIELDS = ("id, source, title, company, location, work_model, employment_type, posted_at, found_at, "
              "filter_passed, filter_reason, prescore_total, prescore_keywords, prescore_title, score_partial, "
              "matched_keywords, duplicate_of, emailed_at, url, run_id")


# --- file writers -----------------------------------------------------------
def _atomic_write(path: Path, text: str, mode: int | None = None):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def update_env(path: Path, updates: dict):
    """Set KEY=value lines in .env, keeping comments, order and every other key."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    left = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in left:
            lines[i] = f"{m.group(1)}={left.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in left.items()]
    _atomic_write(path, "\n".join(lines) + "\n", mode=0o600)


# --- config read / validate / save -----------------------------------------
def profile_text() -> str:
    """The profile, or a bare settings section when there's no profile (or it has no section yet)."""
    p = config.PROFILE_PATH
    text = p.read_text(encoding="utf-8") if p.exists() else ""
    try:
        settings.extract_section(text)
        return text
    except ValueError:
        return (text.rstrip("\n") + "\n\n" if text.strip() else "") + SECTION


def read_config() -> dict:
    env = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    s = settings.parse(profile_text())
    out = {k: env.get(k) or "" for k in EMAIL_KEYS}
    out["has_password"] = bool(env.get("SMTP_APP_PASSWORD"))
    out["SEND_WHEN_EMPTY"] = (env.get("SEND_WHEN_EMPTY") or "false").lower() == "true"
    out["SEND_PARTIAL_SCORES"] = (env.get("SEND_PARTIAL_SCORES") or "false").lower() == "true"
    out["SCHEDULE_PLATFORM"] = env.get("SCHEDULE_PLATFORM") or config.SCHEDULE_PLATFORM
    out["SCHEDULE_HOURS"] = float(env.get("SCHEDULE_HOURS") or 2)
    for k, d in config.SCORE_DEFAULTS.items():
        out[k] = float(env[k]) if env.get(k) else d
    out["email_cutoff"] = s.email_cutoff
    out["profile"] = {a: ", ".join(getattr(s, a)) for a in LIST_FIELDS}
    out["profile"]["salary_floor"] = s.salary_floor
    out["profile"]["search_radius"] = s.search_radius
    out["default_radius"] = settings.DEFAULT_RADIUS_MILES
    out["profile_path"] = str(config.PROFILE_PATH)
    out["profile_exists"] = config.PROFILE_PATH.exists()
    return out


def _num(body, key, lo, hi, integer=False):
    v = body.get(key)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if not lo <= v <= hi or (integer and v != int(v)):
        raise ValueError(f"{key} must be {'a whole number ' if integer else ''}between {lo} and {hi}")
    return int(v) if integer else v


def validate(body: dict) -> tuple[dict, float | None]:
    """Returns (env updates, email cutoff or None). Raises ValueError with a readable message."""
    env = {"SCORE_WEIGHT_KEYWORDS": round(_num(body, "SCORE_WEIGHT_KEYWORDS", 0, 1), 4),
           "KEYWORD_FULL_MARKS": _num(body, "KEYWORD_FULL_MARKS", 1, 100, True),
           "TITLE_EXACT_SCORE": _num(body, "TITLE_EXACT_SCORE", 0, 100, True),
           "TITLE_CONTAINED_SCORE": _num(body, "TITLE_CONTAINED_SCORE", 0, 100, True),
           "SEND_WHEN_EMPTY": "true" if body.get("SEND_WHEN_EMPTY") else "false",
           "SEND_PARTIAL_SCORES": "true" if body.get("SEND_PARTIAL_SCORES") else "false"}
    if "SCHEDULE_PLATFORM" in body:
        if body["SCHEDULE_PLATFORM"] not in ("mac", "windows"):
            raise ValueError("SCHEDULE_PLATFORM must be mac or windows")
        env["SCHEDULE_PLATFORM"] = body["SCHEDULE_PLATFORM"]
    if "SCHEDULE_HOURS" in body:
        env["SCHEDULE_HOURS"] = _num(body, "SCHEDULE_HOURS", 1, 23, True)
    for k in EMAIL_KEYS:
        v = str(body.get(k) or "").strip()
        if v and not EMAIL_RE.match(v):
            raise ValueError(f"{k} doesn't look like an email address")
        env[k] = v
    pw = re.sub(r"\s", "", str(body.get("SMTP_APP_PASSWORD") or ""))  # Google shows it in 4 groups
    if pw:
        env["SMTP_APP_PASSWORD"] = pw  # blank means keep the saved one
    cutoff = body.get("email_cutoff")
    cutoff = None if cutoff in (None, "") else _num(body, "email_cutoff", 0, 100)
    return env, cutoff


def validate_profile(body: dict) -> dict:
    """Profile settings from the page -> {attr: value text}. Empty if the page sent none."""
    prof = body.get("profile")
    if not isinstance(prof, dict):
        return {}
    out = {}
    for a in LIST_FIELDS:
        v = str(prof.get(a) or "")
        if re.search(r"[\r\n]", v):
            raise ValueError(f"{a} must be one line")  # a newline would write a new profile line
        items = [x.strip() for x in v.split(",") if x.strip()]
        if a in CHOICES:
            allowed = {c.lower(): c for c in CHOICES[a]}
            bad = [x for x in items if x.lower() not in allowed]
            if bad:
                raise ValueError(f"{a}: {', '.join(bad)} isn't one of {', '.join(CHOICES[a])}")
            items = [allowed[x.lower()] for x in items]
        out[a] = ", ".join(items)
    rad = prof.get("search_radius")
    out["search_radius"] = "" if rad in (None, "") else f"{_num(prof, 'search_radius', 1, 100, True)}"
    sal = prof.get("salary_floor")
    out["salary_floor"] = "" if sal in (None, "") else f"{_num(prof, 'salary_floor', 0, 10_000_000):g}"
    return out


def apply_profile(text: str, prof: dict, cutoff) -> str:
    for a, v in prof.items():
        text = settings.set_value(text, a, v)
    return settings.set_value(text, "email_cutoff", "" if cutoff is None else f"{cutoff:g}")


def apply_schedule(env: dict) -> str | None:
    """Install/replace this computer's scheduled job when the saved interval differs from the installed one.
    Returns a message for the page, or None when nothing changed."""
    platform, hours = env.get("SCHEDULE_PLATFORM"), env.get("SCHEDULE_HOURS")
    if platform is None or hours is None:
        return None
    if platform != schedule.this_platform():
        return (f"Schedule saved for {platform} but not applied: this computer is {schedule.this_platform()}. "
                f"Run `python -m collector schedule install` on the {platform} computer.")
    if schedule.installed_hours() == hours:
        return None  # reinstalling would start an unneeded run
    try:
        return schedule.install(platform, hours)
    except (SystemExit, subprocess.CalledProcessError, OSError) as e:
        detail = getattr(e, "stderr", None) or e
        return f"Settings saved, but the schedule wasn't applied: {str(detail).strip()[:200]}"


def save_config(body: dict) -> str | None:
    env, cutoff = validate(body)
    prof = validate_profile(body)
    update_env(ENV_PATH, env)
    _atomic_write(config.PROFILE_PATH, apply_profile(profile_text(), prof, cutoff))
    return apply_schedule(env)


# --- queries ------------------------------------------------------------------
def query_jobs(conn, start=None, end=None, field="found"):
    col = "date(found_at, 'localtime')" if field == "found" else "date(posted_at)"
    where, args = [], []
    if start:
        where.append(f"{col} >= ?"); args.append(start)
    if end:
        where.append(f"{col} <= ?"); args.append(end)
    sql = f"SELECT {JOB_FIELDS} FROM jobs {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY found_at DESC, id DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def preview(conn, body: dict, start=None, end=None):
    """Re-filter and re-score non-duplicate jobs in the date range with the proposed (unsaved) values."""
    merged = {**read_config(), **body}
    env, cutoff = validate(merged)
    p = {k: env[k] for k in config.SCORE_DEFAULTS}
    s = settings.parse(apply_profile(profile_text(), validate_profile(merged), cutoff))
    ids = [j["id"] for j in query_jobs(conn, start, end) if j["duplicate_of"] is None]
    out = []
    for i in ids:
        row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (i,)).fetchone())
        if not filters.check(row, s)[0]:
            continue
        new = prescore.score(row, s, p)["prescore_total"]
        partial = not row["description"]
        emailed = env["SEND_PARTIAL_SCORES"] == "true" if partial else cutoff is None or new >= cutoff
        out.append({"id": i, "title": row["title"], "company": row["company"],
                    "old": row["prescore_total"] if row["filter_passed"] else None,
                    "new": new, "partial": partial, "emailed": emailed})
    out.sort(key=lambda r: r["new"], reverse=True)
    return {"cutoff": cutoff, "jobs": out}


def send_test_email():
    env = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    for k in EMAIL_KEYS + ["SMTP_APP_PASSWORD"]:
        setattr(config, k, env.get(k) or "")
    text = "This is a test from the job collector's settings page. If you can read it, email works."
    emailer.send("[Jobs] Test email", f"<p>{text}</p>", text)


# --- HTTP ---------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "collector-ui"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _allowed(self, post=False) -> bool:
        # Host check blocks DNS rebinding; Origin + JSON content type block cross-site POSTs
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in hosts:
            return False
        if post:
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{h}" for h in hosts}:
                return False
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return False
        return True

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            conn = db.connect(config.DB_PATH)
            if u.path == "/api/jobs":
                return self._send(200, query_jobs(conn, q.get("from"), q.get("to"), q.get("field", "found")))
            if m := re.fullmatch(r"/api/jobs/(\d+)", u.path):
                row = conn.execute("SELECT * FROM jobs WHERE id=?", (int(m.group(1)),)).fetchone()
                return self._send(200, dict(row)) if row else self._send(404, {"error": "no such job"})
            if u.path == "/api/runs":
                return self._send(200, [dict(r) for r in db.last_runs(conn, 1000)])
            if u.path == "/api/config":
                return self._send(200, read_config())
            return self._send(404, {"error": "not found"})
        except Exception as e:
            log.exception("GET %s", self.path)
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._allowed(post=True):
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 100_000)
            body = json.loads(self.rfile.read(length) or b"{}")
            if u.path == "/api/config":
                msg = save_config(body)
                return self._send(200, {**read_config(), "schedule_message": msg})
            if u.path == "/api/preview":
                return self._send(200, preview(db.connect(config.DB_PATH), body, q.get("from"), q.get("to")))
            if u.path == "/api/test-email":
                send_test_email()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            log.exception("POST %s", self.path)
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})


def _is_job_collector(url: str) -> bool:
    """True if the server at url is ours: every reply carries our Server header."""
    import urllib.error
    import urllib.request
    try:
        resp = urllib.request.urlopen(url, timeout=3)
    except urllib.error.HTTPError as e:
        resp = e
    except OSError:
        return False
    return (resp.headers.get("Server") or "").startswith(Handler.server_version + " ")


def serve(port: int = 8765, open_browser: bool = True):
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        if e.errno not in (errno.EADDRINUSE, 10048):  # 10048: Windows' "address in use"
            raise
        if not _is_job_collector(url):
            print(f"Port {port} is used by another program, so Job Collector can't open. "
                  "Close that program and try again.")
            raise SystemExit(1)
        print(f"Job Collector is already open at {url}")  # launcher double-clicked again
        import webbrowser
        webbrowser.open(url)
        return
    print(f"Collector UI on {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
