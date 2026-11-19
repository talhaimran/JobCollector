"""Local audit + settings UI: `python -m collector ui`. Listens on 127.0.0.1 only."""
import json
import logging
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import dotenv_values

from . import config, db, emailer, prescore, settings

log = logging.getLogger(__name__)
ENV_PATH = config.ROOT / ".env"
PAGE = Path(__file__).parent / "web" / "index.html"
EMAIL_KEYS = ["EMAIL_FROM", "EMAIL_TO", "SMTP_USER"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
JOB_FIELDS = ("id, source, title, company, location, work_model, employment_type, posted_at, found_at, "
              "filter_passed, filter_reason, prescore_total, prescore_keywords, prescore_title, "
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
def read_config() -> dict:
    env = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    s = settings.load(config.PROFILE_PATH)
    out = {k: env.get(k) or "" for k in EMAIL_KEYS}
    out["has_password"] = bool(env.get("SMTP_APP_PASSWORD"))
    out["SEND_WHEN_EMPTY"] = (env.get("SEND_WHEN_EMPTY") or "false").lower() == "true"
    for k, d in config.SCORE_DEFAULTS.items():
        out[k] = float(env[k]) if env.get(k) else d
    out["email_cutoff"] = s.email_cutoff
    out["must_have_count"] = len(s.must_have)
    out["profile_path"] = str(config.PROFILE_PATH)
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
           "SEND_WHEN_EMPTY": "true" if body.get("SEND_WHEN_EMPTY") else "false"}
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


def save_config(body: dict):
    env, cutoff = validate(body)
    update_env(ENV_PATH, env)
    p = config.PROFILE_PATH
    value = "" if cutoff is None else f"{cutoff:g}"
    _atomic_write(p, settings.set_value(p.read_text(encoding="utf-8"), "email_cutoff", value))


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
    """Re-score passing, non-duplicate jobs in the date range with the proposed values."""
    env, cutoff = validate({**read_config(), **body})
    p = {k: env[k] for k in config.SCORE_DEFAULTS}
    s = settings.load(config.PROFILE_PATH)
    ids = [j["id"] for j in query_jobs(conn, start, end) if j["filter_passed"] and j["duplicate_of"] is None]
    out = []
    for i in ids:
        row = dict(conn.execute("SELECT id, title, company, description, prescore_total FROM jobs WHERE id=?", (i,)).fetchone())
        new = prescore.score(row, s, p)["prescore_total"]
        out.append({"id": i, "title": row["title"], "company": row["company"], "old": row["prescore_total"],
                    "new": new, "emailed": cutoff is None or new >= cutoff})
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
                return self._send(200, [dict(r) for r in db.last_runs(conn, 30)])
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
                save_config(body)
                return self._send(200, read_config())
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


def serve(port: int = 8765, open_browser: bool = True):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Collector UI on {url}  (Ctrl+C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
