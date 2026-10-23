"""TrendReq: the pytrends API, talking to Google Trends directly with the standard library.

Differences from pytrends 4.9.2 are listed in README.md ("Moving from pytrends").
"""
from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from itertools import product
from types import SimpleNamespace

from . import hosted
from .exceptions import ResponseError, TooManyRequestsError

try:
    import pandas as pd
except ImportError:  # pandas is optional: without it, methods return lists of dicts
    pd = None

log = logging.getLogger("pytrends_next")

BASE = "https://trends.google.com"
BASE_TRENDS_URL = f"{BASE}/trends"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

TRENDING_CATEGORIES = {
    1: "Autos and Vehicles", 2: "Beauty and Fashion", 3: "Business and Finance", 4: "Entertainment",
    5: "Food and Drink", 6: "Games", 7: "Health", 8: "Hobbies and Leisure", 9: "Jobs and Education",
    10: "Law and Government", 11: "Other", 13: "Pets and Animals", 14: "Politics", 15: "Science",
    16: "Shopping", 17: "Sports", 18: "Technology", 19: "Travel and Transportation", 20: "Climate",
}

# pytrends trending_searches(pn=...) used country names; Trending now uses ISO codes.
PN_GEO = {
    "argentina": "AR", "australia": "AU", "austria": "AT", "belgium": "BE", "brazil": "BR", "canada": "CA",
    "chile": "CL", "colombia": "CO", "czech_republic": "CZ", "denmark": "DK", "egypt": "EG", "finland": "FI",
    "france": "FR", "germany": "DE", "greece": "GR", "hong_kong": "HK", "hungary": "HU", "india": "IN",
    "indonesia": "ID", "ireland": "IE", "israel": "IL", "italy": "IT", "japan": "JP", "kenya": "KE",
    "malaysia": "MY", "mexico": "MX", "netherlands": "NL", "new_zealand": "NZ", "nigeria": "NG", "norway": "NO",
    "peru": "PE", "philippines": "PH", "poland": "PL", "portugal": "PT", "romania": "RO", "russia": "RU",
    "saudi_arabia": "SA", "singapore": "SG", "south_africa": "ZA", "south_korea": "KR", "spain": "ES",
    "sweden": "SE", "switzerland": "CH", "taiwan": "TW", "thailand": "TH", "turkey": "TR", "ukraine": "UA",
    "united_kingdom": "GB", "united_states": "US", "vietnam": "VN",
}


def _json(text):
    """Google prefixes JSON with )]}' to stop JSON hijacking; drop everything before the first brace."""
    i = text.find("{")
    if i < 0:
        raise ValueError("no JSON in response")
    return json.loads(text[i:])


def _ts(seconds):
    return datetime.fromtimestamp(int(seconds), timezone.utc).replace(tzinfo=None)


def _iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None) if s else None


def _frame(rows, index=None):
    """DataFrame like pytrends when pandas is installed, else the rows themselves."""
    if pd is None:
        return rows
    df = pd.DataFrame(rows)
    if index and not df.empty:
        df = df.set_index(index).sort_index()
    return df


def _flat(d, prefix=""):
    """pd.json_normalize(sep='_') for one dict, without pandas."""
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}_"))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _ranked(data, i, flat):
    """rankedList[i] (0 = top, 1 = rising) as pytrends returns it: None when Google sent no list."""
    try:
        rows = data["rankedList"][i]["rankedKeyword"]
    except (KeyError, IndexError, TypeError):
        return None
    if flat:
        return _frame([_flat(r) for r in rows])
    if not rows:
        return None  # pytrends: empty DataFrame[['query','value']] raised KeyError -> None
    return _frame([{"query": r.get("query"), "value": r.get("value")} for r in rows])


class TrendReq:
    """Google Trends client with the pytrends TrendReq interface.

    fallback="apify": when Google answers HTTP 429 after `retries`, run the same request through the
    hosted Google Trends Scraper Actor on Apify. Paid per result on your own Apify account
    (apify_token=... or the APIFY_TOKEN environment variable). Off by default.
    """

    GENERAL_URL = f"{BASE_TRENDS_URL}/api/explore"
    INTEREST_OVER_TIME_URL = f"{BASE_TRENDS_URL}/api/widgetdata/multiline"
    INTEREST_BY_REGION_URL = f"{BASE_TRENDS_URL}/api/widgetdata/comparedgeo"
    RELATED_QUERIES_URL = f"{BASE_TRENDS_URL}/api/widgetdata/relatedsearches"
    SUGGESTIONS_URL = f"{BASE_TRENDS_URL}/api/autocomplete/"
    CATEGORIES_URL = f"{BASE_TRENDS_URL}/api/explore/pickers/category"
    TRENDING_NOW_URL = f"{BASE}/_/TrendsUi/data/batchexecute"
    TRENDING_RSS_URL = f"{BASE}/trending/rss"

    def __init__(self, hl="en-US", tz=360, geo="", timeout=(2, 5), proxies="", retries=3, backoff_factor=0,
                 requests_args=None, fallback=None, apify_token=None):
        if fallback not in (None, "apify"):
            raise ValueError('fallback must be None or "apify"')
        self.hl = hl
        self.tz = tz
        self.geo = geo
        self.kw_list = []
        # urllib has one timeout; pytrends passed (connect, read) to requests.
        self.timeout = max(timeout) if isinstance(timeout, (tuple, list)) else timeout
        self.proxies = list(proxies) if proxies else []
        self.proxy_index = 0
        self.retries = retries
        self.backoff_factor = backoff_factor
        args = dict(requests_args or {})
        self.headers = {"accept-language": hl, "user-agent": USER_AGENT}
        self.headers.update(args.pop("headers", {}))
        self._proxy_map = args.pop("proxies", None)
        self._ssl = None if args.pop("verify", True) else ssl._create_unverified_context()
        if args:
            warnings.warn(f"pytrends_next ignores these requests_args: {sorted(args)}")
        self.fallback = fallback
        self.apify_token = apify_token or os.environ.get("APIFY_TOKEN")
        if fallback and not self.apify_token:
            raise ValueError('fallback="apify" needs apify_token=... or the APIFY_TOKEN environment variable')
        self.cookies = {}
        self._opener = None
        self._hosted = False
        self._query = {}
        self.token_payload = {}
        self.interest_over_time_widget = {}
        self.interest_by_region_widget = {}
        self.related_topics_widget_list = []
        self.related_queries_widget_list = []

    # HTTP -------------------------------------------------------------------------------------------

    def _fetch(self, url, data=None, headers=None):
        """One HTTP request, returns (status, text). Unit tests replace this method."""
        req = urllib.request.Request(url, data=data, headers={**self.headers, **(headers or {})})
        try:
            with self._opener.open(req, timeout=self.timeout) as res:
                return res.status, res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _new_session(self):
        """Fresh cookie jar (and the next proxy), then fetch the NID cookie a browser visitor would have.
        Google rate-limits cookieless clients much sooner."""
        jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(jar), urllib.request.HTTPSHandler(context=self._ssl)]
        proxy = self._proxy_map or ({"https": self.proxies[self.proxy_index]} if self.proxies else None)
        if proxy:
            handlers.append(urllib.request.ProxyHandler(proxy))
        if self.proxies:
            self.proxy_index = (self.proxy_index + 1) % len(self.proxies)
        self._opener = urllib.request.build_opener(*handlers)
        for url in (f"{BASE_TRENDS_URL}/explore?geo={self.hl[-2:]}&hl={self.hl}", "https://www.google.com/?hl=en"):
            try:
                self._fetch(url)
            except OSError:
                pass  # best effort
            if any(c.name == "NID" for c in jar):
                break
        self.cookies = {c.name: c.value for c in jar if c.name == "NID"}

    def _backoff(self, attempt):
        base = self.backoff_factor * 2 ** attempt if self.backoff_factor else min(30.0, 1.5 * 2 ** attempt)
        return base / 2 + random.random() * base / 2

    def _get_data(self, url, params=None, parse=_json, data=None, headers=None):
        """Request with retries. 429, 403 and 5xx: new session, exponential backoff with jitter.
        400 and 404 mean bad input and are raised at once."""
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        err, parse_fails = None, 0
        for attempt in range(self.retries + 1):
            if self._opener is None:
                self._new_session()
            try:
                status, text = self._fetch(url, data=data, headers=headers)
            except OSError as e:
                err = ResponseError(f"Network error: {e}")
            else:
                resp = SimpleNamespace(status_code=status, text=text[:2000], url=url)
                if status == 200:
                    try:
                        return parse(text)
                    except (ValueError, KeyError, IndexError, TypeError) as e:
                        # A 200 without data is usually deterministic (bad geo, unsupported country).
                        err = ResponseError(f"Unexpected response from Google: {e}", resp)
                        parse_fails += 1
                        if parse_fails >= 3:
                            raise err from None
                elif status in (400, 404):
                    raise ResponseError.from_response(resp)
                elif status == 429:
                    err = TooManyRequestsError.from_response(resp)
                else:
                    err = ResponseError.from_response(resp)
            if attempt < self.retries:
                wait = self._backoff(attempt)
                log.debug("%s; new session, retry in %.1fs", err, wait)
                self._opener = None
                time.sleep(wait)
        raise err

    def _widget(self, url, widget):
        return self._get_data(url, {"hl": self.hl, "tz": self.tz, "req": json.dumps(widget["request"]),
                                    "token": widget["token"]})["default"]

    def _either(self, local, remote):
        """Run locally; on HTTP 429 use the hosted Actor if fallback="apify"."""
        if not self._hosted:
            try:
                return local()
            except TooManyRequestsError:
                if not self.fallback:
                    raise
        log.warning("Google rate-limited this client (HTTP 429). Using the hosted Apify Actor %s "
                    "(billed per result to your Apify account).", hosted.ACTOR_URL)
        return remote()

    # Explore ----------------------------------------------------------------------------------------

    def build_payload(self, kw_list, cat=0, timeframe="today 5-y", geo="", gprop=""):
        """Create the payload for interest over time, by region and related queries/topics."""
        if gprop not in ("", "images", "news", "youtube", "froogle"):
            raise ValueError("gprop must be empty (to indicate web), images, news, youtube, or froogle")
        self.kw_list = [kw_list] if isinstance(kw_list, str) else list(kw_list)
        self.geo = geo or self.geo
        if not isinstance(self.geo, list):
            self.geo = [self.geo]
        self._query = {"cat": cat, "timeframe": timeframe, "gprop": gprop}
        self.token_payload = self._payload(self.kw_list)
        self._hosted = False
        try:
            self._tokens(self._get_data(self.GENERAL_URL, self.token_payload)["widgets"])
        except TooManyRequestsError:
            if not self.fallback:
                raise
            self._hosted = True  # every call on this payload goes to the hosted Actor

    def _payload(self, kws):
        q = self._query
        times = q["timeframe"] if isinstance(q["timeframe"], list) else None
        items = [{"keyword": kw, "time": times[i] if times else q["timeframe"], "geo": g}
                 for i, (kw, g) in enumerate(product(kws, self.geo))]
        return {"hl": self.hl, "tz": self.tz,
                "req": json.dumps({"comparisonItem": items, "category": q["cat"], "property": q["gprop"]})}

    def _tokens(self, widgets):
        self.interest_over_time_widget = {}
        self.interest_by_region_widget = {}
        self.related_queries_widget_list = []
        self.related_topics_widget_list = []
        for w in widgets:
            if w["id"] == "TIMESERIES":
                self.interest_over_time_widget = w
            if w["id"] == "GEO_MAP" and not self.interest_by_region_widget:
                self.interest_by_region_widget = w
            if "RELATED_TOPICS" in w["id"]:
                self.related_topics_widget_list.append(w)
            if "RELATED_QUERIES" in w["id"]:
                self.related_queries_widget_list.append(w)

    def _columns(self):
        return [kw if len(self.geo) == 1 else (kw, g) for kw, g in product(self.kw_list, self.geo)]

    def interest_over_time(self):
        """DataFrame indexed by date: one int column per keyword (0-100) and isPartial."""
        def local():
            data = self._widget(self.INTEREST_OVER_TIME_URL, self.interest_over_time_widget)
            cols = self._columns()
            rows = [{"date": _ts(p["time"]), **{c: int(v) for c, v in zip(cols, p["value"])},
                     "isPartial": bool(p.get("isPartial", False))} for p in data["timelineData"]]
            return self._over_time_frame(rows)

        def remote():
            points = {}
            for item in self._hosted_items("interest_over_time", includeInterestOverTime=True):
                for p in item.get("timeline", []):
                    row = points.setdefault(p["timestamp"], {"date": _ts(p["timestamp"])})
                    row[item["term"]] = int(p["value"])
                    row["isPartial"] = bool(p.get("isPartial", False))
            rows = [{"date": r["date"], **{k: r.get(k, 0) for k in self.kw_list}, "isPartial": r["isPartial"]}
                    for _, r in sorted(points.items())]
            return self._over_time_frame(rows)

        return self._either(local, remote)

    def _over_time_frame(self, rows):
        df = _frame(rows, "date")
        if pd is not None and len(self.geo) > 1 and not df.empty:
            df.columns = pd.MultiIndex.from_tuples([c if isinstance(c, tuple) else (c,) for c in df],
                                                   names=["keyword", "region"])
        return df

    def _region_resolution(self, resolution):
        # pytrends meant to apply `resolution` only worldwide or for US sub-levels (its check never matched).
        geo = self.geo[0] if self.geo else ""
        return resolution if (not geo or resolution != "COUNTRY") else None

    def interest_by_region(self, resolution="COUNTRY", inc_low_vol=False, inc_geo_code=False):
        """DataFrame indexed by geoName: one int column per keyword (plus geoCode if inc_geo_code)."""
        res = self._region_resolution(resolution)

        def local():
            w = self.interest_by_region_widget
            req = {**w["request"], "includeLowSearchVolumeGeos": inc_low_vol, **({"resolution": res} if res else {})}
            data = self._widget(self.INTEREST_BY_REGION_URL, {"request": req, "token": w["token"]})
            rows = []
            for g in data["geoMapData"]:
                row = {"geoName": g["geoName"]}
                if inc_geo_code:
                    row["geoCode"] = g.get("geoCode", g.get("coordinates"))
                row.update({kw: int(v) for kw, v in zip(self.kw_list, g["value"])})
                rows.append(row)
            return _frame(rows, "geoName")

        def remote():
            # ponytail: the Actor scales each term on its own map; locally pytrends' shared scale is used.
            merged = {}
            items = self._hosted_items("interest_by_region", includeInterestByRegion=True,
                                       regionResolution=res or "auto", includeLowVolumeRegions=inc_low_vol)
            for item in items:
                for g in item.get("regions", []):
                    row = merged.setdefault(g["geoName"], {"geoName": g["geoName"],
                                                           **({"geoCode": g.get("geoCode")} if inc_geo_code else {}),
                                                           **{kw: 0 for kw in self.kw_list}})
                    row[item["term"]] = int(g.get("value", 0))
            return _frame(list(merged.values()), "geoName")

        return self._either(local, remote)

    def _related(self, widgets, flat):
        by_kw = {}
        for w in widgets:
            try:
                by_kw[w["request"]["restriction"]["complexKeywordsRestriction"]["keyword"][0]["value"]] = w
            except (KeyError, IndexError):
                by_kw[""] = w
        prefix = "RELATED_TOPICS" if flat else "RELATED_QUERIES"
        for kw in self.kw_list:
            if kw not in by_kw and len(self.geo) == 1:
                # Google now sends related topics only for single-term explores: ask for this term alone.
                widgets = self._get_data(self.GENERAL_URL, self._payload([kw]))["widgets"]
                w = next((x for x in widgets if x["id"] == prefix), None)
                if w:
                    by_kw[kw] = w
        result = {}
        for kw, w in by_kw.items():
            data = self._widget(self.RELATED_QUERIES_URL, w)
            result[kw] = {"top": _ranked(data, 0, flat), "rising": _ranked(data, 1, flat)}
        return result

    def related_queries(self):
        """{keyword: {'top': DataFrame[query, value] or None, 'rising': ...}}"""
        def remote():
            out = {kw: {"top": None, "rising": None} for kw in self.kw_list}
            for item in self._hosted_items("related_queries", includeRelatedQueries=True):
                out[item["term"]] = {k: _frame([{"query": r["query"], "value": r["value"]} for r in item.get(k, [])])
                                     if item.get(k) else None for k in ("top", "rising")}
            return out

        return self._either(lambda: self._related(self.related_queries_widget_list, False), remote)

    def related_topics(self):
        """{keyword: {'top': DataFrame or None, 'rising': ...}}. Google often hides topics from scripts."""
        def remote():
            out = {kw: {"top": None, "rising": None} for kw in self.kw_list}
            for item in self._hosted_items("related_topics", includeRelatedTopics=True):
                out[item["term"]] = {k: _frame([{"value": r.get("value"), "formattedValue": r.get("formattedValue"),
                                                 "link": r.get("link"), "topic_mid": r.get("topicId"),
                                                 "topic_title": r.get("topic"), "topic_type": r.get("topicType")}
                                                for r in item.get(k, [])]) for k in ("top", "rising")}
            return out

        return self._either(lambda: self._related(self.related_topics_widget_list, True), remote)

    def _hosted_items(self, kind, **flags):
        if len(self.geo) != 1 or isinstance(self._query.get("timeframe"), list):
            raise ValueError("The hosted fallback supports one geo and one timeframe per build_payload")
        if any("," in kw for kw in self.kw_list):
            raise ValueError("The hosted fallback does not support commas inside a keyword")
        actor_input = {"searchTerms": [", ".join(self.kw_list)], "geo": self.geo[0],
                       **hosted.timeframe(self._query["timeframe"]), "category": self._query["cat"],
                       "gprop": self._query["gprop"] or "web", "language": self.hl, "timezoneOffset": self.tz,
                       "includeInterestOverTime": False, **flags}
        return [i for i in hosted.run(actor_input, self.apify_token) if i.get("type") == kind]

    # Trending now -----------------------------------------------------------------------------------

    def trending_now(self, geo="US", hours=24):
        """Google Trends 'Trending now' for one country (hours: 4, 24, 48 or 168).
        Rows: title, volume, increase_percentage, started, ended, active, categories, related_queries.
        Falls back to the RSS feed (24 hours, with news) when the main endpoint changes shape."""
        def local():
            try:
                return self._trending_batch(geo, hours)
            except TooManyRequestsError:
                raise
            except ResponseError as e:
                log.info("Trending now endpoint failed (%s); using the RSS feed", e)
                return self._trending_rss(geo)

        def remote():
            items = hosted.run({"searchTerms": [], "trendingNowCountries": [geo], "trendingNowHours": str(hours),
                                "language": self.hl}, self.apify_token)
            return [{"title": t["title"], "volume": t.get("searchVolume"),
                     "increase_percentage": t.get("increasePercentage"), "started": _iso(t.get("startedAt")),
                     "ended": _iso(t.get("endedAt")), "active": t.get("active"), "categories": t.get("categories", []),
                     "related_queries": t.get("relatedQueries", [])} for t in items if t.get("type") == "trending_now"]

        return self._either(local, remote)

    def _trending_batch(self, geo, hours):
        f_req = json.dumps([[["i0OFE", json.dumps([None, None, geo, 0, self.hl, hours, 1]), None, "generic"]]])

        def parse(text):
            line = next(ln for ln in text.split("\n") if ln.startswith('[["wrb.fr"'))
            trends = json.loads(json.loads(line)[0][2])[1]
            if not isinstance(trends, list):
                raise ValueError("no trends list")
            return trends

        url = f"{self.TRENDING_NOW_URL}?rpcids=i0OFE&source-path=%2Ftrending&hl={self.hl}&rt=c"
        rows = self._get_data(url, parse=lambda t: _guard(parse, t),
                              data=urllib.parse.urlencode({"f.req": f_req}).encode(),
                              headers={"content-type": "application/x-www-form-urlencoded;charset=UTF-8"})
        return [{"title": t[0], "volume": t[6], "increase_percentage": t[8],
                 "started": _ts(t[3][0]) if t[3] else None, "ended": _ts(t[4][0]) if t[4] else None,
                 "active": not t[4], "categories": [TRENDING_CATEGORIES.get(c, str(c)) for c in (t[10] or [])],
                 "related_queries": t[9] if isinstance(t[9], list) else []} for t in rows]

    def _trending_rss(self, geo):
        def parse(text):
            rows = []
            for it in ET.fromstring(text).iter("item"):
                traffic = it.findtext("{*}approx_traffic") or ""
                pub = it.findtext("pubDate")
                rows.append({"title": it.findtext("title"), "volume": int("".join(filter(str.isdigit, traffic)) or 0) or None,
                             "increase_percentage": None,
                             "started": parsedate_to_datetime(pub).astimezone(timezone.utc).replace(tzinfo=None) if pub else None,
                             "ended": None, "active": None, "categories": [], "related_queries": [],
                             "news": [{"title": n.findtext("{*}news_item_title"), "url": n.findtext("{*}news_item_url"),
                                       "source": n.findtext("{*}news_item_source")} for n in it.iter("{*}news_item")]})
            return rows

        return self._get_data(self.TRENDING_RSS_URL, {"geo": geo}, parse=lambda t: _guard(parse, t))

    def _trending(self, pn, hours=24):
        geo = PN_GEO.get(pn, pn).upper()
        return [t["title"] for t in self.trending_now(geo, hours)]

    def trending_searches(self, pn="united_states"):
        """DataFrame with one column (0) of trending titles. pn: pytrends country name or ISO code."""
        titles = self._trending(pn)
        return pd.DataFrame(titles) if pd is not None else titles

    def today_searches(self, pn="US"):
        """Series of today's trending titles (past 24 hours)."""
        titles = self._trending(pn)
        return pd.Series(titles) if pd is not None else titles

    def realtime_trending_searches(self, pn="US", cat="all", count=300):
        """DataFrame[entityNames, title]: trends started in the past 4 hours. `cat` is not supported."""
        if cat != "all":
            warnings.warn("realtime_trending_searches(cat=...) is ignored: Trending now has no category filter")
        rows = self.trending_now(PN_GEO.get(pn, pn).upper(), 4)[:count]
        return _frame([{"entityNames": t["related_queries"], "title": t["title"]} for t in rows])

    # Other ------------------------------------------------------------------------------------------

    def suggestions(self, keyword):
        """Google's keyword suggestions: list of {'mid', 'title', 'type'}."""
        return self._get_data(self.SUGGESTIONS_URL + urllib.parse.quote(keyword), {"hl": self.hl})["default"]["topics"]

    def categories(self):
        """Category tree: {'name', 'id', 'children': [...]}"""
        return self._get_data(self.CATEGORIES_URL, {"hl": self.hl})

    def multirange_interest_over_time(self):
        raise NotImplementedError("multirange_interest_over_time is not ported yet. Call build_payload once per range.")

    def top_charts(self, *args, **kwargs):
        raise NotImplementedError("Google removed the Top Charts endpoint. Use trending_now() for current trends.")

    def get_historical_interest(self, *args, **kwargs):
        raise NotImplementedError("Removed in pytrends for incorrectness (pytrends PR #542).")


def _guard(parse, text):
    """Turn StopIteration from a missing line into the ValueError _get_data retries on."""
    try:
        return parse(text)
    except StopIteration:
        raise ValueError("payload not found") from None
    except ET.ParseError as e:
        raise ValueError(f"not RSS: {e}") from None
