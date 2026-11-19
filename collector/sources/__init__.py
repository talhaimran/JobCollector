"""Shared HTTP helper for the public company feeds."""
import logging
import time

import requests

from .. import config

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "job-collector/1.0 (personal job search; low volume)"}


def get_json(url: str, tries: int = 3):
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=config.HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code < 500 and e.response.status_code != 429:
                raise  # 404 etc: wrong board name, retrying won't help
            if attempt == tries - 1:
                raise
        except requests.RequestException:
            if attempt == tries - 1:
                raise
        wait = 5 * 2 ** attempt
        log.warning("%s failed, retrying in %ss", url, wait)
        time.sleep(wait)
