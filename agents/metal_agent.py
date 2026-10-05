"""
MetalAgent - one agent per metal (gold / silver / platinum).

Its job, for its own metal only:
  1. collect today's rate (and the last-10-days table) from goodreturns, moneycontrol and a jeweller site
  2. collect recent news from its feeds, keeping only items that mention its metal
It returns raw records plus a small report. Cleaning, tagging and storing happen in processing/,
so all three agents feed one shared pipeline.

A source that fails is logged in the report and skipped; the other sources still count.
"""

import hashlib
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from agents.scrapers import IST, PARSERS, fetch
from agents.sources import MAX_NEWS_PER_FEED, METALS

log = logging.getLogger("agents")


def _published(entry):
    for key in ("published", "updated"):
        if entry.get(key):
            try:
                return parsedate_to_datetime(entry[key]).astimezone(IST)
            except (TypeError, ValueError):
                pass
    return datetime.now(IST)


class MetalAgent:
    def __init__(self, metal):
        self.metal = metal
        self.cfg = METALS[metal]

    def collect_rates(self, report):
        rates = []
        for source, url in self.cfg["rate_pages"].items():
            try:
                got = [r for r in PARSERS[source](self.metal, url) if r["price_per_gram"]]
                rates += got
                report["rate_sources"][source] = len(got)
                if not got:
                    report["errors"].append(f"{source}: page fetched but no {self.metal} rate found")
            except Exception as e:
                log.warning("%s/%s failed: %s", self.metal, source, e)
                report["rate_sources"][source] = 0
                report["errors"].append(f"{source}: {e}")
        return rates

    def collect_news(self, report):
        articles, seen = [], set()
        keywords = self.cfg["keywords"]
        exclude = self.cfg.get("exclude", [])
        for feed_url in self.cfg["news_feeds"]:
            try:
                parsed = feedparser.parse(fetch(feed_url, "rss").encode("utf-8"))
            except Exception as e:
                report["errors"].append(f"feed {feed_url[:60]}: {e}")
                continue
            kept = 0
            for e in parsed.entries:
                title = e.get("title", "")
                summary = e.get("summary", "")
                if not any(k in f"{title} {summary}".lower() for k in keywords):
                    continue                            # e.g. crude-oil stories in the shared ET feed
                if any(x in title.lower() for x in exclude):
                    continue
                url = e.get("link", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                publisher = (e.get("source") or {}).get("title") or _publisher_from(feed_url)
                articles.append({
                    "id": hashlib.sha1(url.encode()).hexdigest()[:16],
                    "metal": self.metal,
                    "title": title,
                    "summary_html": summary,
                    "url": url,
                    "publisher": publisher,
                    "published": _published(e).isoformat(),
                    "feed": feed_url,
                })
                kept += 1
                if kept >= MAX_NEWS_PER_FEED:
                    break
        report["news"] = len(articles)
        return articles

    def run(self):
        started = datetime.now(timezone.utc)
        report = {"agent": f"{self.metal}_agent", "rate_sources": {}, "errors": []}
        rates = self.collect_rates(report)
        articles = self.collect_news(report)
        report["rates"] = len(rates)
        report["seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        log.info("%s agent: %d rate rows, %d news items, %d errors",
                 self.metal, len(rates), len(articles), len(report["errors"]))
        return rates, articles, report


PUBLISHERS = {"economictimes": "The Economic Times", "thehindubusinessline": "BusinessLine",
              "business-standard": "Business Standard", "livemint": "Mint"}


def _publisher_from(feed_url):
    return next((name for key, name in PUBLISHERS.items() if key in feed_url), "Google News")
