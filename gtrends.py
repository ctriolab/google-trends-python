"""
gtrends: a small Python client for Google Trends data that keeps working when pytrends hits 429.

It calls the hosted "Google Trends Scraper" Actor on Apify through Apify's public REST API, which
handles Google's rate limits (session rotation, proxies, retries) for you. Standard library only;
pandas is optional and used only by the *_df helpers.

    export APIFY_TOKEN=...   # https://console.apify.com/settings/integrations
    python gtrends.py "coffee, tea" --geo US --timeframe "today 12-m"

MIT License, (c) Ctrio Lab. https://github.com/ctriolab/google-trends-python
Disclosure: Ctrio Lab also builds and sells the Apify Actor this client calls (pay per result).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ACTOR = "ctriolab~google-trends-scraper"
API = f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items"


class TrendsError(RuntimeError):
    pass


def _run(actor_input: dict, token: str | None = None, timeout: int = 300) -> list[dict]:
    token = token or os.environ.get("APIFY_TOKEN")
    if not token:
        raise TrendsError("Set the APIFY_TOKEN environment variable or pass token=...")
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
        raise TrendsError(f"Apify API returned HTTP {e.code}: {body}") from None


def _terms_line(terms) -> str:
    if isinstance(terms, str):
        return terms
    if len(terms) > 5:
        raise ValueError("Google Trends compares at most 5 terms at once")
    return ", ".join(terms)


def interest_over_time(terms, geo: str = "", timeframe: str = "today 12-m", gprop: str = "web",
                       category: int = 0, token: str | None = None) -> list[dict]:
    """One item per term: {'term', 'average', 'timeline': [{'date', 'value', 'isPartial'}, ...], ...}"""
    items = _run({
        "searchTerms": [_terms_line(terms)], "geo": geo, "timeframe": timeframe, "gprop": gprop,
        "category": category, "includeInterestOverTime": True,
    }, token)
    return [i for i in items if i.get("type") == "interest_over_time"]


def interest_by_region(terms, geo: str = "", timeframe: str = "today 12-m", resolution: str = "auto",
                       token: str | None = None) -> list[dict]:
    """One item per term: {'term', 'resolution', 'regions': [{'geoCode', 'geoName', 'value'}, ...]}"""
    items = _run({
        "searchTerms": [_terms_line(terms)], "geo": geo, "timeframe": timeframe,
        "includeInterestOverTime": False, "includeInterestByRegion": True, "regionResolution": resolution,
    }, token)
    return [i for i in items if i.get("type") == "interest_by_region"]


def related_queries(terms, geo: str = "", timeframe: str = "today 12-m", token: str | None = None) -> dict:
    """{term: {'top': [{'query', 'value'}...], 'rising': [...]}} (rising values can be 'Breakout')."""
    items = _run({
        "searchTerms": [_terms_line(terms)], "geo": geo, "timeframe": timeframe,
        "includeInterestOverTime": False, "includeRelatedQueries": True,
    }, token)
    return {i["term"]: {"top": i.get("top", []), "rising": i.get("rising", [])}
            for i in items if i.get("type") == "related_queries"}


def trending_now(countries=("US",), hours: int = 24, limit: int = 0, token: str | None = None) -> list[dict]:
    """What is spiking in Google Search right now: title, searchVolume, increasePercentage, startedAt, ..."""
    items = _run({
        "searchTerms": [], "trendingNowCountries": list(countries), "trendingNowHours": str(hours),
        "maxTrendingPerCountry": limit,
    }, token)
    return [i for i in items if i.get("type") == "trending_now"]


def interest_over_time_df(terms, **kwargs):
    """Same shape as pytrends' interest_over_time(): index = date, one column per term, plus isPartial."""
    import pandas as pd  # optional dependency

    items = interest_over_time(terms, **kwargs)
    if not items:
        return pd.DataFrame()
    frame = {}
    partial = {}
    for item in items:
        for point in item["timeline"]:
            frame.setdefault(point["date"], {})[item["term"]] = point["value"]
            partial[point["date"]] = point.get("isPartial", False)
    df = pd.DataFrame.from_dict(frame, orient="index").sort_index()
    df.index = pd.to_datetime(df.index)
    df.index.name = "date"
    df["isPartial"] = [partial[d] for d in sorted(partial)]
    return df


def _main() -> None:
    p = argparse.ArgumentParser(description="Google Trends from the command line (prints JSON).")
    p.add_argument("terms", nargs="?", help='Up to 5 comma-separated terms, e.g. "coffee, tea"')
    p.add_argument("--geo", default="", help="US, GB, KR, US-CA ... (empty = worldwide)")
    p.add_argument("--timeframe", default="today 12-m", help='"now 7-d", "today 3-m", "today 5-y", "all" ...')
    p.add_argument("--regions", action="store_true", help="interest by region instead of over time")
    p.add_argument("--related", action="store_true", help="related queries (top and rising)")
    p.add_argument("--trending", metavar="CC", nargs="+", help="Trending now for country codes, e.g. US KR")
    a = p.parse_args()
    try:
        if a.trending:
            out = trending_now(a.trending)
        elif not a.terms:
            p.error("give terms or --trending")
        elif a.regions:
            out = interest_by_region(a.terms, geo=a.geo, timeframe=a.timeframe)
        elif a.related:
            out = related_queries(a.terms, geo=a.geo, timeframe=a.timeframe)
        else:
            out = interest_over_time(a.terms, geo=a.geo, timeframe=a.timeframe)
    except TrendsError as e:
        sys.exit(str(e))
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    _main()
