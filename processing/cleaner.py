"""
Cleaning step - runs once on the merged output of all three agents.

Rates:
  - drop rows with no price, or a price outside a sane per-gram range (catches parser mistakes,
    e.g. a 10-gram price read as a 1-gram price)
  - de-duplicate (metal, purity, date, source); a "today" row wins over a "history" row
  - flag a source that is more than 15% away from the median of the other sources that day

Articles:
  - the same story often reaches several agents -> merge into one article with a list of metals
  - strip HTML, split "Headline - Publisher", normalise whitespace, drop junk (quote pages, tickers)
  - for full-text publishers, download the article body with trafilatura (headline feeds stay short)
"""

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from statistics import median

import trafilatura

from agents.scrapers import fetch

log = logging.getLogger("cleaner")

PRICE_RANGE = {"gold": (2_000, 60_000), "silver": (20, 2_000), "platinum": (1_000, 40_000)}
DEVIATION_FLAG = 0.15
JUNK_TITLE = re.compile(r"stock price, news, quote|quote and history|tradingview|price to earnings|"
                        r"studios inc|live updates?:?$", re.I)
FULLTEXT_HOSTS = ("economictimes.indiatimes.com", "thehindubusinessline.com", "business-standard.com",
                  "livemint.com")
MAX_FULLTEXT = 40          # per run - keeps a run under a couple of minutes


def clean_rates(rows):
    kept, dropped = {}, 0
    for r in rows:
        lo, hi = PRICE_RANGE[r["metal"]]
        if not r.get("price_per_gram") or not lo <= r["price_per_gram"] <= hi:
            dropped += 1
            continue
        key = (r["metal"], r["purity"], r["date"], r["source"])
        if key not in kept or r["kind"] == "today":
            kept[key] = r
    rates = list(kept.values())

    # cross-source sanity check, per metal/purity/day
    groups = {}
    for r in rates:
        groups.setdefault((r["metal"], r["purity"], r["date"]), []).append(r)
    flagged = 0
    for group in groups.values():
        for r in group:
            others = [o["price_per_gram"] for o in group if o is not r]
            r["flag"] = ""
            if others:
                gap = abs(r["price_per_gram"] - median(others)) / median(others)
                if gap > DEVIATION_FLAG:
                    r["flag"] = f"{gap:.0%} away from other sources"
                    flagged += 1
    return rates, {"rates_in": len(rows), "rates_kept": len(rates), "rates_dropped": dropped,
                   "rates_flagged": flagged}


def _strip_html(text):
    text = unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _split_publisher(title, publisher):
    """Google News titles end with ' - Publisher'."""
    if publisher and title.endswith(f" - {publisher}"):
        return title[: -len(publisher) - 3].strip()
    return re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip() if " - " in title else title.strip()


def _norm_title(title):
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:90]


def _full_text(url):
    try:
        return trafilatura.extract(fetch(url, "article", retries=1), include_comments=False) or ""
    except Exception as e:
        log.info("full text failed for %s: %s", url, e)
        return ""


def clean_articles(raw):
    merged = {}
    for a in raw:
        title = _split_publisher(_strip_html(a["title"]), a["publisher"])
        if len(title) < 25 or JUNK_TITLE.search(title):
            continue
        key = _norm_title(title)
        if key in merged:                                 # same story from another metal's agent
            if a["metal"] not in merged[key]["metals"]:
                merged[key]["metals"].append(a["metal"])
            continue
        summary = _strip_html(a["summary_html"])
        if summary.startswith(title[:40]):                # Google News repeats the headline
            summary = ""
        published = datetime.fromisoformat(a["published"])
        merged[key] = {
            "id": hashlib.sha1(a["url"].encode()).hexdigest()[:16],
            "title": title,
            "summary": summary,
            "text": "",
            "url": a["url"],
            "publisher": a["publisher"].replace(".com", "").strip(),
            "published": a["published"],
            "date": published.date().isoformat(),
            "metals": [a["metal"]],
        }
    articles = list(merged.values())

    to_fetch = [a for a in articles if any(h in a["url"] for h in FULLTEXT_HOSTS)][:MAX_FULLTEXT]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for a, text in zip(to_fetch, pool.map(lambda x: _full_text(x["url"]), to_fetch)):
            a["text"] = re.sub(r"\n{2,}", "\n", text).strip()
    for a in articles:
        if not a["text"]:
            a["text"] = a["summary"] or a["title"]
    return articles, {"articles_in": len(raw), "articles_kept": len(articles),
                      "full_text": sum(1 for a in to_fetch if len(a["text"]) > 300)}
