"""Build and send the per-run email."""
import html
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage

from . import config
from .normalize import parse_dt


def ago(posted_at: str | None, now: datetime) -> str:
    if not posted_at:
        return "unknown"
    date_only = len(posted_at) == 10
    delta = now - parse_dt(posted_at)
    if date_only:
        days = max(0, delta.days)
        return "today" if days == 0 else f"{days}d ago"
    mins = max(0, int(delta.total_seconds() // 60))
    return f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago" if mins < 1440 else f"{mins // 1440}d ago"


def flags(j: dict) -> list[str]:
    out = []
    if j.get("score_partial"):
        out.append("partial score: no description, so keywords and dealbreakers weren't checked")
    if j.get("work_model") == "unknown":
        out.append("work model unknown")
    if j.get("salary_currency") and j["salary_currency"].upper() != "CAD" and (j.get("salary_min") or j.get("salary_max")):
        out.append(f"salary in {j['salary_currency']} (floor not checked)")
    return out


def _salary(j):
    lo, hi = j.get("salary_min"), j.get("salary_max")
    if lo is None and hi is None:
        return None
    rng = "–".join(f"{v:,.0f}" for v in (lo, hi) if v is not None)
    return f"{rng} {j.get('salary_currency') or ''} {j.get('salary_interval') or ''}".strip()


def build(jobs: list[dict], stats: dict, filters_off: list, errors: dict, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone().strftime("%Y-%m-%d %H:%M")
    subject = f"[Jobs] {len(jobs)} new matches – {stamp}"
    counts = f"found {stats['found']} / new {stats['new']} / passed {stats['passed']} / emailed {len(jobs)}"
    err_lines = [m for msgs in errors.values() for m in msgs]

    text, rows = [subject, ""], []
    for j in jobs:
        kws = ", ".join(j.get("matched_keywords") or []) or "none"
        meta = [j.get("company") or "?", j.get("location") or "?", j.get("work_model"), j["source"],
                f"posted {j.get('posted_at') or '?'} ({ago(j.get('posted_at'), now)})"]
        extra = [x for x in [_salary(j), *flags(j)] if x]
        score = f"{j['prescore_total']:.0f}" + (" partial" if j.get("score_partial") else "")
        text += [f"[{score}] {j['title']}", f"  {j['url']}", "  " + " · ".join(meta),
                 f"  keywords: {kws}"] + ([f"  note: {'; '.join(extra)}"] if extra else []) + [""]
        e = html.escape
        rows.append(
            f"<tr><td style='padding:8px;font-size:20px;font-weight:bold;vertical-align:top'>{j['prescore_total']:.0f}"
            + ("<br><span style='font-size:11px;font-weight:normal;color:#a60'>partial</span>" if j.get("score_partial") else "")
            + "</td>"
            f"<td style='padding:8px'><a href='{e(j['url'] or '')}' style='font-size:16px'>{e(j['title'] or '')}</a><br>"
            f"{' · '.join(e(str(m)) for m in meta)}<br>"
            f"<span style='color:#555'>keywords ({j['prescore_keywords']:.0f}) : {e(kws)} · title {j['prescore_title']:.0f}</span>"
            + (f"<br><span style='color:#a60'>{e('; '.join(extra))}</span>" if extra else "") + "</td></tr>")

    footer = [f"Filters off: {', '.join(filters_off) or 'none'}",
              f"Source errors: {len(err_lines) or 'none'}", *[f"  - {m}" for m in err_lines], f"Counts: {counts}"]
    text += ["—"] + footer
    body = (f"<html><body style='font-family:sans-serif'><table>{''.join(rows) or '<tr><td>No new matches.</td></tr>'}</table>"
            f"<hr><p style='color:#555;font-size:12px'>{'<br>'.join(html.escape(f) for f in footer)}</p></body></html>")
    return subject, body, "\n".join(text)


class NotConfigured(RuntimeError):
    """Email was skipped during setup: not an error, the jobs wait on the page."""


def send(subject: str, html_body: str, text_body: str):
    missing = [k for k in ("EMAIL_FROM", "EMAIL_TO", "SMTP_USER", "SMTP_APP_PASSWORD") if not getattr(config, k)]
    if missing:
        raise NotConfigured(f"Email not sent: set {', '.join(missing)} in .env")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, config.EMAIL_FROM, config.EMAIL_TO
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=60) as s:
        s.login(config.SMTP_USER, config.SMTP_APP_PASSWORD)
        s.send_message(msg)
