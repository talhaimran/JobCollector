# Security Policy

## Reporting a vulnerability

Please **don't open a public issue**. Report it privately through [GitHub's private vulnerability reporting](https://github.com/talhaimran/JobCollector/security/advisories/new). Expect a first reply within 7 days.

Only the latest commit on `main` gets security fixes.

## What's sensitive in this project

- **`.env`** holds your Gmail **app password** (`SMTP_APP_PASSWORD`). The settings page writes it with owner-only permissions (`0600`) and never sends it back to the browser. Use an app password, never your real Google password, and revoke it at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) if it leaks.
- **`candidate-profile.md`** and **`data/jobs.db`** hold your job search history. Keep them out of commits; `.gitignore` already excludes them.
- **The settings page** (`python -m collector ui`) listens only on `127.0.0.1` and rejects requests from other sites. Don't expose it on a network or behind a tunnel.

In scope: leaking the app password or profile, the settings page being reachable or writable from another origin, and unsafe handling of scraped HTML.
