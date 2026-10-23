"""Live smoke test against Google Trends (about 12 requests). Run it sparingly: Google rate-limits.

    python tests/live_smoke.py            # print what came back
    python tests/live_smoke.py --record   # also save the raw responses to tests/fixtures/google.json
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from pytrends_next.request import TrendReq  # noqa: E402
from test_request import fixture_key  # noqa: E402

saved = {}
tr = TrendReq(hl="en-US", tz=0, timeout=(10, 25), retries=1)
real_fetch = tr._fetch


def fetch(url, data=None, headers=None):
    status, text = real_fetch(url, data, headers)
    print(f"  HTTP {status} {url[:100]}")
    key = fixture_key(url)
    if status == 200 and key.split("|")[0] not in ("/trends/explore", "/"):
        saved[key] = text
    return status, text


tr._fetch = fetch


def show(name, value):
    print(f"{name}: {type(value).__name__} {getattr(value, 'shape', len(value) if hasattr(value, '__len__') else '')}")
    print(str(value)[:400])


tr.build_payload(["coffee", "tea"], timeframe="today 3-m", geo="US")
show("interest_over_time", tr.interest_over_time())
show("interest_by_region", tr.interest_by_region(inc_geo_code=True))
show("related_queries", tr.related_queries())
show("related_topics", tr.related_topics())
show("trending_now", tr.trending_now("US", 24))
show("trending rss", tr._trending_rss("US"))
show("suggestions", tr.suggestions("python"))
show("categories", tr.categories().get("children", [])[:2])

if "--record" in sys.argv:
    out = HERE / "fixtures" / "google.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(saved, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"saved {len(saved)} responses to {out}")
