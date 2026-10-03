# google-trends-python

Google Trends data in Python **without the 429 errors**: interest over time, interest by region,
related queries and Trending now. One file, standard library only (pandas optional).

```python
import gtrends

df = gtrends.interest_over_time_df(["bitcoin", "ethereum"], timeframe="today 1-m")
print(df.tail(3))
#             bitcoin  ethereum  isPartial
# date
# 2026-10-01       38         6      False
# 2026-10-02       41         6      False
# 2026-10-03       34         6       True
```

## Why this exists

[pytrends](https://github.com/GeneralMills/pytrends) is unofficial, currently looking for maintainers, and its own
README notes that Google rate-limits it ("60 seconds of sleep between requests ... once you reach the limit").
From a laptop or a single server IP you quickly run into `TooManyRequestsError` / HTTP 429.

Google has no public Trends API for most users, so the reliable fix is to rotate sessions and IPs and back off.
This client sends your query to a hosted scraper that does exactly that, and gives you plain Python objects back.

**Disclosure:** the hosted part is our [Google Trends Scraper Actor on Apify](https://apify.com/ctriolab/google-trends-scraper?utm_source=github&utm_medium=readme&utm_campaign=google-trends-python).
It is pay per result (about $1.50 per 1,000 results, lower on paid Apify plans) and Apify's free plan includes
monthly credit, which covers a lot of small queries. This client is MIT licensed.

## Setup

1. Create a free Apify account and copy your API token from **Settings > API & Integrations**.
2. `export APIFY_TOKEN=your_token` (Windows PowerShell: `$env:APIFY_TOKEN="your_token"`)
3. Download `gtrends.py` (or clone this repo). No `pip install` needed; install `pandas` only if you want DataFrames.

## Usage

```python
import gtrends

# Interest over time: one item per term, 0-100 scale shared across the terms (max 5)
for item in gtrends.interest_over_time(["coffee", "tea"], geo="US", timeframe="today 3-m"):
    print(item["term"], item["average"], item["timeline"][-1])

# Interest by region (auto: worldwide -> countries, a country -> regions/states)
regions = gtrends.interest_by_region("coffee", geo="US")[0]["regions"]
print(regions[:3])  # [{'geoCode': 'US-WY', 'geoName': 'Wyoming', 'value': 100, ...}, ...]

# Related queries, top and rising ("Breakout" included)
rq = gtrends.related_queries("coffee", geo="US")
print(rq["coffee"]["rising"][:5])

# Trending now (what is spiking in Google Search right now), per country
for t in gtrends.trending_now(["US", "GB"], hours=24, limit=10):
    print(t["geo"], t["title"], t["searchVolume"], t["increasePercentage"])
```

Command line:

```bash
python gtrends.py "coffee, tea" --geo US --timeframe "today 12-m" > coffee_vs_tea.json
python gtrends.py "coffee" --geo US --regions
python gtrends.py "coffee" --geo US --related
python gtrends.py --trending US KR JP
```

## Moving from pytrends

| pytrends | gtrends |
|---|---|
| `TrendReq(); build_payload(kw_list, timeframe=..., geo=...)` | pass `terms, timeframe=..., geo=...` to each call |
| `interest_over_time()` | `interest_over_time_df(terms, ...)` (same DataFrame shape) or `interest_over_time(terms, ...)` |
| `interest_by_region(resolution='COUNTRY')` | `interest_by_region(terms, geo="", resolution="COUNTRY")` |
| `related_queries()` | `related_queries(terms, ...)` returns `{term: {"top": [...], "rising": [...]}}` |
| `trending_searches(pn='united_states')` / `realtime_trending_searches(pn='US')` | `trending_now(["US"])` |
| `timeframe='today 5-y'`, `'now 7-d'`, `'2024-01-01 2024-12-31'` | same strings (`"custom"` ranges: use the Actor input `customTimeRange`) |
| `gprop='youtube'` | `gprop="youtube"` on `interest_over_time` |

Each call is one Actor run, so batch your terms (up to 5 per comparison) instead of calling in a tight loop.
For many comparisons, schedules or Google Sheets exports, run the Actor directly with a list of `searchTerms`.

## Notes

- Values are relative (0-100) within a comparison, exactly like trends.google.com. Different calls are not on the same scale.
- Very small search volumes come back as all zeros, which is Google's own behaviour.
- Related topics are often hidden by Google for automated clients, so this client does not expose them.

## License

MIT. See [LICENSE](LICENSE).
