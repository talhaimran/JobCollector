# Contributing to JobCollector

Thanks for helping. Bug reports, new job sources and filter fixes are all welcome.

## Set up

```bash
git clone https://github.com/talhaimran/JobCollector.git
cd JobCollector
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q
```

The tests never touch the network: a fixture in `tests/conftest.py` fails any test that tries. Sources are tested against saved responses in `tests/fixtures/`.

## Making a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Branch from `main`: `git checkout -b my-change`.
3. Add or update a test for the behaviour you changed, and run `.venv/bin/pytest -q`.
4. Keep the diff small and in the style of the code around it. No new dependency for something a few lines can do.
5. Update `README.md` if a command, setting or default changes.
6. Open a pull request and fill in the template.

## Adding a job source

- Put it in `collector/sources/` and return jobs in the shape `collector/normalize.py` produces.
- **No logins, no CAPTCHA solving, no bypassing blocks.** Use public pages or public APIs only.
- Give it a request budget and a pause between requests, like `linkedin.py`. One failing source must never stop a run.
- Save a real (trimmed) response in `tests/fixtures/` and test the parser against it.

## Never commit

`.env`, `candidate-profile.md`, `data/` or `logs/`. They hold your email, app password and job history, and `.gitignore` already excludes them.
