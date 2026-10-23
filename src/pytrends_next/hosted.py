"""Opt-in hosted fallback: the Ctrio Lab "Google Trends Scraper" Actor on Apify.

This is a PAID service billed per result to the caller's own Apify account (APIFY_TOKEN).
It is only used when TrendReq(fallback="apify") is set and Google answered HTTP 429.
Disclosure: Ctrio Lab builds both this library and the Actor.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from types import SimpleNamespace

from .exceptions import ResponseError

ACTOR = "ctriolab~google-trends-scraper"
API = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"
ACTOR_URL = "https://apify.com/ctriolab/google-trends-scraper"
TIMEFRAMES = {"now 1-H", "now 4-H", "now 1-d", "now 7-d", "today 1-m", "today 3-m", "today 12-m", "today 5-y", "all"}
_CUSTOM = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2})? \d{4}-\d{2}-\d{2}(T\d{2})?$")


def timeframe(tf: str) -> dict:
    """pytrends timeframe -> Actor input fields. Rejects what the Actor would reject, before a paid run."""
    if tf in TIMEFRAMES:
        return {"timeframe": tf}
    if isinstance(tf, str) and _CUSTOM.match(tf.strip()):
        return {"timeframe": "custom", "customTimeRange": tf.strip()}
    raise ValueError(f'The hosted fallback does not support timeframe "{tf}". '
                     f'Use one of {sorted(TIMEFRAMES)} or "YYYY-MM-DD YYYY-MM-DD".')


def run(actor_input: dict, token: str, timeout: int = 300) -> list:
    """Run the Actor synchronously and return its dataset items."""
    req = urllib.request.Request(
        f"{API}?timeout={timeout}",
        data=json.dumps(actor_input).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout + 30) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise ResponseError(f"Apify API returned HTTP {e.code}: {body}",
                            SimpleNamespace(status_code=e.code, text=body, url=API)) from None
