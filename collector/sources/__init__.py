"""Shared HTTP helpers: retrying JSON getter for the company feeds, polite page client for the boards."""
import logging
import random
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


class PoliteClient:
    """Browser-like GETs with a random pause between requests. Any HTTP error (403/429 included) raises:
    the source stops for this run and the next run tries again."""
    BROWSER = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/130.0.0.0 Safari/537.36",
    }

    def __init__(self, delay, get=None, sleep=time.sleep):
        self._delay, self._get, self._sleep, self.used = delay, get or requests.Session().get, sleep, 0

    def get(self, url):
        if self.used:
            self._sleep(random.uniform(*self._delay))
        self.used += 1
        r = self._get(url, headers=self.BROWSER, timeout=config.HTTP_TIMEOUT)
        r.raise_for_status()
        return r
