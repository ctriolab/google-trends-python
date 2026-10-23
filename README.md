# pytrends-next

A maintained drop-in replacement for [pytrends](https://github.com/GeneralMills/pytrends), which was archived
(read-only) in April 2025. Same `TrendReq` API, same DataFrames, talks to Google Trends directly.

- Standard library only. pandas is optional: with pandas you get the same DataFrames as pytrends,
  without it you get lists of dicts.
- Python 3.9+.
- Built-in handling of HTTP 429: new cookie session and exponential backoff with jitter.
- Optional, paid, off by default: route a call through a hosted scraper when Google keeps answering 429.

```bash
pip install "pytrends-next[pandas]"     # or: pip install pytrends-next   (no pandas)
```

```python
from pytrends_next.request import TrendReq   # was: from pytrends.request import TrendReq

pytrends = TrendReq(hl="en-US", tz=360)
pytrends.build_payload(["coffee", "tea"], timeframe="today 3-m", geo="US")
print(pytrends.interest_over_time().head(3))
#             coffee  tea  isPartial
# date
# 2026-07-23      75   35      False
# 2026-07-24      81   38      False
# 2026-07-25      94   40      False
```

For most code the migration is one search and replace: `pytrends.` to `pytrends_next.` in imports.
`pytrends_next.exceptions` has the same `ResponseError` and `TooManyRequestsError`.

## Moving from pytrends

| pytrends 4.9.2 | pytrends-next | Notes |
|---|---|---|
| `TrendReq(hl, tz, geo, timeout, proxies, retries, backoff_factor, requests_args)` | same | `timeout` tuple: the larger value is used (urllib has one timeout). `requests_args`: `headers`, `proxies` and `verify` are used, other keys are ignored with a warning. `retries` defaults to 3 (pytrends: 0). |
| `build_payload(kw_list, cat, timeframe, geo, gprop)` | same | Lists of geos and timeframes work as in pytrends. |
| `interest_over_time()` | same | Index `date`, one int column per keyword, `isPartial`. MultiIndex columns for several geos. |
| `interest_by_region(resolution, inc_low_vol, inc_geo_code)` | same | pytrends' check meant to apply `resolution` worldwide and for sub-levels never matched; here it does. |
| `related_queries()` | same | `{kw: {"top": DataFrame[query, value] or None, "rising": ...}}` |
| `related_topics()` | same | Google now returns topics only for single-term requests, so each term is asked alone. Often empty, see Limits. |
| `trending_searches(pn="united_states")` | same | The old endpoint is gone. Now served from Google's "Trending now" list. `pn` takes the pytrends country name or an ISO code (`"US"`). |
| `today_searches(pn="US")` | same | Series of titles, past 24 hours. |
| `realtime_trending_searches(pn, cat, count)` | same | Trends started in the past 4 hours, columns `entityNames`, `title`. `cat` is ignored. |
| `suggestions(keyword)` | same | |
| `categories()` | same | |
| `top_charts()` | raises `NotImplementedError` | Google removed the endpoint. |
| `multirange_interest_over_time()` | raises `NotImplementedError` | Not ported yet. |
| (none) | `trending_now(geo="US", hours=24)` | New. Rows with `title`, `volume`, `increase_percentage`, `started`, `ended`, `active`, `categories`, `related_queries`. `hours`: 4, 24, 48 or 168. Falls back to the Trending RSS feed if the main endpoint changes. |

## HTTP 429 (TooManyRequestsError)

Google Trends has no public API. It rate-limits by IP and by session, and a laptop or a single
server often gets HTTP 429 after a few dozen requests. pytrends raised `TooManyRequestsError` at once.

pytrends-next does what our hosted scraper does on every request:

1. Visits trends.google.com first to get the `NID` cookie a browser visitor has (cookieless clients are limited sooner).
2. On 429, 403 or 5xx: drops the session, takes the next proxy from `proxies=[...]` if you gave any,
   waits about 0.75 s, 1.5 s, 3 s ... (capped at 30 s, with jitter) and tries again, `retries` times.
3. 400 and 404 are not retried (bad geo, category or timeframe).

If you still get `TooManyRequestsError`: send fewer requests (batch up to 5 terms per `build_payload`),
wait a few minutes, use your own proxies, or use the hosted fallback below.

## Hosted fallback (optional, paid)

```python
pytrends = TrendReq(fallback="apify", apify_token="apify_api_...")   # or set APIFY_TOKEN
```

When Google still answers 429 after the local retries, the same call is sent to the
[Google Trends Scraper Actor on Apify](https://apify.com/ctriolab/google-trends-scraper?utm_source=github&utm_medium=readme&utm_campaign=pytrends-next),
which rotates IPs on Apify's proxy network. The result comes back in the same shape.

- **It costs money.** The Actor is billed per result to your own Apify account (about $1.50 per 1,000
  results; one result is one term's timeline, one term's region list, one term's related list or one trending topic).
  Apify's free plan includes a small monthly credit.
- It is never on by default. Setting `APIFY_TOKEN` alone does nothing; you must pass `fallback="apify"`.
- A warning is logged (logger `pytrends_next`) every time a call goes to the Actor.
- If `build_payload` itself gets 429, every method on that payload goes to the Actor.
- Supported: `interest_over_time`, `interest_by_region`, `related_queries`, `related_topics`, `trending_now`
  and the pytrends trending methods. Not supported: several geos or timeframes in one payload,
  commas inside a keyword, `suggestions`, `categories`.
- Difference: in hosted mode each term's region map is scaled on its own (locally, the terms share one scale).
- Timeframes: the named ranges (`now 1-H` to `today 5-y`, `all`) and `YYYY-MM-DD YYYY-MM-DD`. Others are rejected before a run starts.

Disclosure: Ctrio Lab maintains this library and also builds and sells that Actor. The library is MIT
licensed and fully works without it.

## Limits

- Values are relative (0 to 100) within one payload, as on trends.google.com. Separate calls are not on the same scale.
- Related topics are usually empty: Google hides them from clients it classifies as automated. Empty lists come back as `None`, like pytrends.
- Very small search volumes come back as all zeros (Google's own behaviour).
- This is an unofficial client for undocumented endpoints. Google can change them at any time.

## Legacy single-file client

`gtrends.py` (the earlier version of this repo) is still here and unchanged. It calls the hosted Actor for
every request. New code should use `pytrends_next.request.TrendReq`: `gtrends.interest_over_time_df(terms, ...)`
becomes `build_payload(terms, ...)` plus `interest_over_time()`.

## Development

```bash
python -m unittest discover -s tests     # no network, recorded responses in tests/fixtures
python tests/live_smoke.py               # about 12 live requests to Google
```

## License

MIT. See [LICENSE](LICENSE).
