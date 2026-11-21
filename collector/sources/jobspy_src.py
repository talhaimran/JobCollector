"""JobSpy: Indeed, Glassdoor (python-jobspy 1.4.1). LinkedIn has its own client (linkedin.py)."""
import logging
import random
import time

from .. import config, normalize

log = logging.getLogger(__name__)


class _Capture(logging.Handler):
    """JobSpy logs 429/403/'no job data' as ERROR and returns empty results instead of raising."""
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def search_titles(targets: list[str]) -> list[str]:
    """Drop titles that contain another target ('Senior Project Manager' ⊃ 'Project Manager'):
    the broader search already returns them, and every extra query risks rate limits."""
    return [t for t in targets
            if not any(o.lower() != t.lower() and normalize.phrase_in(o, t) for o in targets)]


def build_queries(settings) -> list[dict]:
    """One query per search title x search location, plus one remote query per title x 'Remote in' country."""
    titles = search_titles(settings.target_titles)
    qs = [{"title": t, "location": loc, "remote": False, "distance": settings.radius()}
          for t in titles for loc in settings.search_where()]
    qs += [{"title": t, "location": c, "remote": True} for t in titles for c in settings.remote_in]
    return qs


def scrape_kwargs(site: str, q: dict) -> dict:
    kw = dict(site_name=[site], search_term=q["title"], location=q["location"],
              results_wanted=config.RESULTS_WANTED, hours_old=config.HOURS_OLD,
              country_indeed=config.COUNTRY_INDEED, verbose=0)
    if q.get("distance"):
        kw["distance"] = q["distance"]  # miles, for Indeed and Glassdoor
    if site == "indeed":
        # Indeed allows only one of hours_old / is_remote / job_type: keep hours_old (freshness)
        if q["remote"]:
            kw["search_term"] = f"{q['title']} remote"
    elif q["remote"]:
        kw["is_remote"] = True
    return kw


def fetch(settings, scrape=None, sleep=time.sleep):
    """Returns (jobs, found_counts, errors). Never raises for a single site/query failure."""
    if scrape is None:
        from jobspy import scrape_jobs as scrape  # heavy import, only when actually scraping
    jobs, found, errors = [], {s: 0 for s in config.JOBSPY_SITES}, {}
    fails = {s: 0 for s in config.JOBSPY_SITES}
    queries = build_queries(settings)
    first = True
    for q in queries:
        for site in config.JOBSPY_SITES:
            if fails[site] >= config.MAX_SITE_ERRORS:
                continue
            if not first:
                sleep(random.uniform(*config.DELAY_SECONDS))
            first = False
            label = f"{site}: {q['title']} @ {q['location']}{' (remote)' if q['remote'] else ''}"
            rows, err = [], None
            cap = _Capture()
            logging.getLogger("jobspy").addHandler(cap)
            try:
                df = scrape(**scrape_kwargs(site, q))
                rows = df.to_dict("records") if df is not None else []
                if cap.messages:  # partial rows are kept, but it still counts as an error
                    err = "; ".join(dict.fromkeys(cap.messages))
            except Exception as e:  # one site's failure must not stop the run
                err = f"{type(e).__name__}: {e}"
            finally:
                logging.getLogger("jobspy").removeHandler(cap)
            if err:
                fails[site] += 1
                msg = f"{label}: {err[:200]}"
                log.warning(msg)
                errors.setdefault(site, []).append(msg)
                if fails[site] >= config.MAX_SITE_ERRORS:
                    errors[site].append(f"{site}: skipped for rest of run after {fails[site]} errors in a row")
                else:
                    sleep(config.BACKOFF_BASE_SECONDS * 2 ** (fails[site] - 1))
            else:
                fails[site] = 0
            log.info("%s -> %d", label, len(rows))
            for row in rows:
                row.setdefault("site", site)
                try:
                    jobs.append(normalize.from_jobspy(row))
                    found[site] += 1
                except Exception as e:
                    errors.setdefault(site, []).append(f"{label}: bad row: {e}")
    return jobs, found, errors
