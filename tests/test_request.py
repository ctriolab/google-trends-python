"""Unit tests without network: Google responses recorded by tests/live_smoke.py --record on 2026-10-23.

The two explore responses for "coffee,tea" and "coffee" were rebuilt from the recorded tokens (the first
recorder keyed explore calls by path only and kept just the "tea" one). Every widget, Trending now, RSS,
autocomplete and category response is the raw recording.
Run: python -m unittest discover -s tests
"""
import datetime as dt
import json
import os
import pathlib
import sys
import unittest
import urllib.parse
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from pytrends_next import ResponseError, TooManyRequestsError, TrendReq, hosted, request  # noqa: E402

FIX = json.loads((HERE / "fixtures" / "google.json").read_text(encoding="utf-8"))


def fixture_key(url):
    p = urllib.parse.urlparse(url)
    q = urllib.parse.parse_qs(p.query)
    if p.path == "/trends/api/explore":
        return f"{p.path}|{','.join(i['keyword'] for i in json.loads(q['req'][0])['comparisonItem'])}"
    return f"{p.path}|{q.get('token', [''])[0]}"


class Fake:
    """Stands in for TrendReq._fetch. overrides: {path: (status, text) or [responses in order]}."""

    def __init__(self, **overrides):
        self.calls, self.overrides = [], overrides

    def __call__(self, url, data=None, headers=None):
        key = fixture_key(url)
        path = key.split("|")[0]
        self.calls.append(key)
        if path in self.overrides:
            v = self.overrides[path]
            return v.pop(0) if isinstance(v, list) else v
        if path in ("/trends/explore", "/"):
            return 200, ""  # session bootstrap
        return 200, FIX[key]

    def count(self, path):
        return sum(1 for c in self.calls if c.split("|")[0] == path)


def rows(x):
    """Same assertions with and without pandas."""
    if request.pd is not None and isinstance(x, request.pd.DataFrame):
        return (x.reset_index() if x.index.name else x).to_dict("records")
    return x


def client(fake=None, **kw):
    tr = TrendReq(tz=0, **kw)
    tr._fetch = fake or Fake()
    return tr


@mock.patch("pytrends_next.request.time.sleep", lambda s: None)
class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tr = client()
        self.tr.build_payload(["coffee", "tea"], timeframe="today 3-m", geo="US")

    def test_interest_over_time(self):
        r = rows(self.tr.interest_over_time())
        self.assertEqual(len(r), 93)
        self.assertEqual(r[0], {"date": dt.datetime(2026, 7, 23), "coffee": 75, "tea": 35, "isPartial": False})

    @unittest.skipIf(request.pd is None, "pandas not installed")
    def test_dataframe_shape_matches_pytrends(self):
        df = self.tr.interest_over_time()
        self.assertEqual(df.index.name, "date")
        self.assertEqual(list(df.columns), ["coffee", "tea", "isPartial"])
        self.assertEqual(str(df["coffee"].dtype), "int64")
        self.assertEqual(str(df["isPartial"].dtype), "bool")
        reg = self.tr.interest_by_region(inc_geo_code=True)
        self.assertEqual(reg.index.name, "geoName")
        self.assertEqual(list(reg.columns), ["geoCode", "coffee", "tea"])
        self.assertTrue(reg.index.is_monotonic_increasing)

    def test_interest_by_region(self):
        r = rows(self.tr.interest_by_region(inc_geo_code=True))
        self.assertEqual(len(r), 51)
        self.assertIn({"geoName": "Hawaii", "geoCode": "US-HI", "coffee": 73, "tea": 27}, r)

    def test_region_resolution(self):
        self.assertIsNone(self.tr._region_resolution("COUNTRY"))  # geo US: keep Google's REGION default
        self.assertEqual(self.tr._region_resolution("DMA"), "DMA")
        self.tr.geo = [""]
        self.assertEqual(self.tr._region_resolution("COUNTRY"), "COUNTRY")

    def test_related_queries(self):
        rq = self.tr.related_queries()
        self.assertEqual(set(rq), {"coffee", "tea"})
        self.assertEqual(rows(rq["coffee"]["top"])[0], {"query": "coffee near me", "value": 100})
        self.assertEqual(rows(rq["tea"]["top"])[0]["query"], "green tea")
        self.assertTrue(rows(rq["coffee"]["rising"]))

    def test_related_topics_asks_single_terms_and_keeps_empty_as_none(self):
        rt = self.tr.related_topics()
        self.assertEqual(rt, {"coffee": {"top": None, "rising": None}, "tea": {"top": None, "rising": None}})
        self.assertIn("/trends/api/explore|coffee", self.tr._fetch.calls)
        self.assertIn("/trends/api/explore|tea", self.tr._fetch.calls)

    def test_trending_now(self):
        r = self.tr.trending_now("US", 24)
        self.assertEqual(len(r), 542)
        self.assertEqual((r[0]["title"], r[0]["volume"], r[0]["categories"], r[0]["active"]),
                         ("patriots vs bears", 1000000, ["Sports"], True))
        self.assertIsInstance(r[0]["started"], dt.datetime)

    def test_trending_falls_back_to_rss(self):
        tr = client(Fake(**{"/_/TrendsUi/data/batchexecute": (200, "not the payload")}))
        r = tr.trending_now("US")
        self.assertEqual(len(r), 10)
        self.assertEqual(r[0]["title"], "isaiah hartenstein")
        self.assertEqual(tr._fetch.count("/trending/rss"), 1)

    def test_pytrends_trending_methods(self):
        self.assertEqual(len(rows(self.tr.trending_searches(pn="united_states"))), 542)
        today = self.tr.today_searches()
        self.assertEqual(list(today)[0], "patriots vs bears")
        rt = rows(self.tr.realtime_trending_searches(pn="US", count=5))
        self.assertEqual(len(rt), 5)
        self.assertEqual(set(rt[0]), {"entityNames", "title"})

    def test_suggestions_and_categories(self):
        s = self.tr.suggestions("python")
        self.assertEqual(s[0]["title"], "Python")
        self.assertTrue(self.tr.categories()["children"])

    def test_payload_matches_pytrends(self):
        req = json.loads(self.tr.token_payload["req"])
        self.assertEqual(req["comparisonItem"][0], {"keyword": "coffee", "time": "today 3-m", "geo": "US"})
        self.assertEqual(self.tr.geo, ["US"])
        with self.assertRaises(ValueError):
            self.tr.build_payload(["x"], gprop="web")


@mock.patch("pytrends_next.request.time.sleep", lambda s: None)
class RetryTests(unittest.TestCase):
    def test_429_rotates_session_then_raises(self):
        fake = Fake(**{"/trends/api/explore": (429, "rate limited")})
        tr = client(fake, retries=2)
        with self.assertRaises(TooManyRequestsError) as cm:
            tr.build_payload(["coffee"])
        self.assertEqual(cm.exception.response.status_code, 429)
        self.assertEqual(fake.count("/trends/api/explore"), 3)
        self.assertEqual(fake.count("/trends/explore"), 3)  # a new session (cookies) before each attempt

    def test_429_then_ok(self):
        ok = (200, FIX["/trends/api/explore|coffee,tea"])
        tr = client(Fake(**{"/trends/api/explore": [(429, ""), ok]}))
        tr.build_payload(["coffee", "tea"], timeframe="today 3-m", geo="US")
        self.assertEqual(len(rows(tr.interest_over_time())), 93)

    def test_400_is_not_retried(self):
        fake = Fake(**{"/trends/api/explore": (400, "bad")})
        with self.assertRaises(ResponseError):
            client(fake).build_payload(["coffee"], geo="ZZ")
        self.assertEqual(fake.count("/trends/api/explore"), 1)

    def test_backoff_factor(self):
        tr = TrendReq(backoff_factor=1)
        self.assertTrue(2 <= tr._backoff(2) <= 4)


ACTOR_TIMELINE = [
    {"type": "interest_over_time", "term": t, "timeline": [
        {"date": "2026-07-23", "timestamp": 1784764800, "value": v, "isPartial": False},
        {"date": "2026-07-24", "timestamp": 1784851200, "value": v + 1, "isPartial": True}]}
    for t, v in (("coffee", 75), ("tea", 35))]


@mock.patch("pytrends_next.request.time.sleep", lambda s: None)
class FallbackTests(unittest.TestCase):
    def test_off_by_default_even_with_env_token(self):
        with mock.patch.dict(os.environ, {"APIFY_TOKEN": "t"}), mock.patch.object(hosted, "run") as run:
            tr = client(Fake(**{"/trends/api/explore": (429, "")}), retries=0)
            self.assertIsNone(tr.fallback)
            with self.assertRaises(TooManyRequestsError):
                tr.build_payload(["coffee"])
            run.assert_not_called()

    def test_needs_token(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            TrendReq(fallback="apify")

    def test_build_payload_429_routes_to_actor(self):
        tr = client(Fake(**{"/trends/api/explore": (429, "")}), retries=0, fallback="apify", apify_token="tok")
        tr.build_payload(["coffee", "tea"], timeframe="today 3-m", geo="US")
        with mock.patch.object(hosted, "run", return_value=ACTOR_TIMELINE) as run:
            r = rows(tr.interest_over_time())
        run.assert_called_once_with({
            "searchTerms": ["coffee, tea"], "geo": "US", "timeframe": "today 3-m", "category": 0, "gprop": "web",
            "language": "en-US", "timezoneOffset": 0, "includeInterestOverTime": True}, "tok")
        self.assertEqual(r[0], {"date": dt.datetime(2026, 7, 23), "coffee": 75, "tea": 35, "isPartial": False})
        self.assertEqual(r[1]["isPartial"], True)

    def test_widget_429_routes_one_call(self):
        tr = client(Fake(**{"/trends/api/widgetdata/comparedgeo": (429, "")}), retries=0,
                    fallback="apify", apify_token="tok")
        tr.build_payload(["coffee", "tea"], timeframe="2024-01-01 2024-12-31", geo="US")
        items = [{"type": "interest_by_region", "term": "coffee", "regions": [{"geoCode": "US-HI", "geoName": "Hawaii", "value": 73}]},
                 {"type": "interest_by_region", "term": "tea", "regions": [{"geoCode": "US-HI", "geoName": "Hawaii", "value": 27}]}]
        with mock.patch.object(hosted, "run", return_value=items) as run:
            r = rows(tr.interest_by_region(inc_geo_code=True))
        inp = run.call_args[0][0]
        self.assertEqual((inp["timeframe"], inp["customTimeRange"]), ("custom", "2024-01-01 2024-12-31"))
        self.assertEqual((inp["includeInterestByRegion"], inp["regionResolution"]), (True, "auto"))
        self.assertEqual(r, [{"geoName": "Hawaii", "geoCode": "US-HI", "coffee": 73, "tea": 27}])

    def test_related_and_trending_mapping(self):
        tr = client(Fake(**{"/trends/api/explore": (429, ""), "/_/TrendsUi/data/batchexecute": (429, ""),
                            "/trending/rss": (429, "")}), retries=0, fallback="apify", apify_token="tok")
        tr.build_payload(["coffee"], timeframe="today 12-m", geo="")
        rq = [{"type": "related_queries", "term": "coffee", "top": [{"rank": 1, "query": "coffee near me", "value": 100}], "rising": []}]
        with mock.patch.object(hosted, "run", return_value=rq) as run:
            out = tr.related_queries()
        self.assertTrue(run.call_args[0][0]["includeRelatedQueries"])
        self.assertEqual(rows(out["coffee"]["top"]), [{"query": "coffee near me", "value": 100}])
        self.assertIsNone(out["coffee"]["rising"])
        tn = [{"type": "trending_now", "title": "x", "searchVolume": 500, "increasePercentage": 200,
               "startedAt": "2026-10-23T01:00:00.000Z", "endedAt": None, "active": True, "categories": ["Sports"],
               "relatedQueries": ["y"]}]
        with mock.patch.object(hosted, "run", return_value=tn) as run:
            t = tr.trending_now("KR", 4)
        self.assertEqual(run.call_args[0][0]["trendingNowCountries"], ["KR"])
        self.assertEqual(t[0]["started"], dt.datetime(2026, 10, 23, 1))

    def test_timeframe_mapping(self):
        self.assertEqual(hosted.timeframe("today 5-y"), {"timeframe": "today 5-y"})
        with self.assertRaises(ValueError):
            hosted.timeframe("today 2-m")  # rejected before a paid run

    def test_run_request(self):
        res = mock.MagicMock()
        res.__enter__.return_value.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=res) as op:
            self.assertEqual(hosted.run({"a": 1}, "tok"), [])
        req = op.call_args[0][0]
        self.assertIn("ctriolab~google-trends-scraper/run-sync-get-dataset-items", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")


if __name__ == "__main__":
    unittest.main()
