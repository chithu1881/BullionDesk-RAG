"""
Rate-page parsers. Every parser returns a list of rate records, all priced per gram in INR:

    {"metal": "gold", "purity": "22K", "date": "2026-10-05", "price_per_gram": 13675.0,
     "source": "goodreturns", "url": "...", "kind": "today" | "history"}

"history" rows come from the "last 10 days" tables, so the knowledge base has a week of prices
from the very first run.
"""

import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

from agents.sources import BROWSER_UA, USER_AGENT_FOR

IST = timezone(timedelta(hours=5, minutes=30))   # fixed offset: zoneinfo needs tzdata on Windows
log = logging.getLogger("scrapers")

UNIT_GRAMS = {"1 gram": 1, "gram": 1, "1 gm": 1, "10 gram": 10, "100 gram": 100, "1 kg": 1000, "1kg": 1000}


def today_ist():
    return datetime.now(IST).date()


def fetch(url, source, retries=3):
    headers = {"User-Agent": USER_AGENT_FOR.get(source, BROWSER_UA), "Accept-Language": "en-IN,en;q=0.9"}
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=25)
            r.raise_for_status()
            return r.text
        except Exception as e:
            last = e
            log.warning("%s attempt %d failed: %s", url, attempt, e)
            time.sleep(2 * attempt)
    raise RuntimeError(f"could not fetch {url}: {last}")


def money(text):
    """'₹1,36,750 (+95)' -> 136750.0 (first rupee amount only, Indian digit grouping handled)."""
    m = re.search(r"₹\s*([\d,]+(?:\.\d+)?)", text) or re.search(r"([\d,]+(?:\.\d+)?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def parse_date(text):
    for fmt in ("%b %d, %Y", "%d %B %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def table_rows(table):
    rows = []
    for tr in table.find_all("tr"):
        cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    return rows


def soup_of(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):   # JSON-LD scripts repeat the headings
        tag.decompose()
    return soup


def table_after(soup, heading_pattern):
    """The first <table> that follows a heading whose text matches heading_pattern."""
    for el in soup.find_all(string=re.compile(heading_pattern, re.I)):
        if el.parent.name not in ("h1", "h2", "h3", "h4", "p", "div", "span"):
            continue                                     # skip <title>, <option> etc.
        table = el.find_next("table")
        if table:
            return table
    return None


def history_from_table(table, metal, purities, source, url):
    """Parses a 'last 10 days' table. Header is Date + one column per purity (gold) or per unit."""
    rows = table_rows(table)
    if not rows:
        return []
    header = [h.lower() for h in rows[0]]
    out = []
    for cells in rows[1:]:
        day = parse_date(cells[0]) if cells else None
        if not day:
            continue
        for col, name in enumerate(header[1:], start=1):
            if col >= len(cells):
                break
            price = money(cells[col])
            if price is None:
                continue
            purity, grams = None, None
            k = re.search(r"(\d+)\s*k\b", name)
            if k and f"{k.group(1)}K" in purities:          # gold: 24K / 22K columns are per gram
                purity, grams = f"{k.group(1)}K", 1
            elif name.strip() in UNIT_GRAMS and len(purities) == 1:
                purity, grams = purities[0], UNIT_GRAMS[name.strip()]
            if purity and not any(r["purity"] == purity and r["date"] == day.isoformat() for r in out):
                out.append(_rec(metal, purity, day, price / grams, source, url, "history"))
    return out


def _rec(metal, purity, day, price, source, url, kind):
    return {"metal": metal, "purity": purity, "date": day.isoformat(), "price_per_gram": round(price, 2),
            "source": source, "url": url, "kind": kind}


PURITIES = {"gold": ["24K", "22K", "18K"], "silver": ["999"], "platinum": ["999"]}


# ---- goodreturns ---------------------------------------------------------------

def goodreturns(metal, url):
    soup = soup_of(fetch(url, "goodreturns"))
    title = soup.title.get_text() if soup.title else ""
    m = re.search(r"\((\d{1,2} \w+ \d{4})\)", title)
    page_day = parse_date(m.group(1)) if m else today_ist()

    out = []
    first = soup.find("table")
    rows = table_rows(first) if first else []
    if rows:
        header = [h.upper() for h in rows[0]]
        for cells in rows[1:]:
            if cells[0].strip() != "1":                 # the "1 gram" row
                continue
            if metal == "gold":                         # Gram | 24K | 22K | 18K
                for col, name in enumerate(header[1:], start=1):
                    if name in PURITIES["gold"] and col < len(cells):
                        out.append(_rec(metal, name, page_day, money(cells[col]), "goodreturns", url, "today"))
            else:                                       # Gram | Today | Yesterday | Change
                out.append(_rec(metal, "999", page_day, money(cells[1]), "goodreturns", url, "today"))

    hist = table_after(soup, r"Last 10 Days")
    if hist:
        out += history_from_table(hist, metal, PURITIES[metal], "goodreturns", url)
    return out


# ---- moneycontrol --------------------------------------------------------------

def moneycontrol(metal, url):
    soup = soup_of(fetch(url, "moneycontrol"))
    text = soup.get_text(" ", strip=True)
    m = re.search(r"As on (\d{1,2} \w+ \d{4})", text)
    page_day = parse_date(m.group(1)) if m else today_ist()

    out = []
    tables = []
    if metal == "gold":
        for purity in ("22", "24"):
            t = table_after(soup, rf"^\s*{purity}\s+Carat\s+Rate")
            if t:
                tables.append((f"{purity}K", t))
    else:
        t = next((t for t in soup.find_all("table") if re.search(r"today", t.get_text(), re.I)), None)
        if t:
            tables.append(("999", t))
    for purity, t in tables:
        for cells in table_rows(t):
            if cells and re.fullmatch(r"1\s*gram", cells[0].strip(), re.I) and len(cells) > 1:
                out.append(_rec(metal, purity, page_day, money(cells[1]), "moneycontrol", url, "today"))
                break

    hist = table_after(soup, r"Last 10 Days")
    if hist:
        out += history_from_table(hist, metal, PURITIES[metal] if metal != "gold" else ["24K", "22K"],
                                  "moneycontrol", url)
    return out


# ---- jeweller: Thangamayil Jewellery (one page lists all three metals) -------------

def thangamayil(metal, url):
    # The rate list has malformed attributes, so a regex on the raw HTML is more reliable than the DOM:
    #   <span class="left">Gold 22k</span><span><span class="price">₹13675</span>
    html = fetch(url, "thangamayil")
    wanted = {"gold": {"gold 24k": "24K", "gold 22k": "22K", "gold 18k": "18K"},
              "silver": {"silver": "999"}, "platinum": {"platinum": "999"}}[metal]
    out, day = [], today_ist()
    pattern = r'<span class="left">\s*([^<]+?)\s*</span>\s*<span>\s*<span class="price">([^<]+)</span>'
    for name, price in re.findall(pattern, html):
        purity = wanted.get(name.strip().lower())
        if purity and not any(r["purity"] == purity for r in out):
            out.append(_rec(metal, purity, day, money(price), "thangamayil", url, "today"))
    return out


PARSERS = {"goodreturns": goodreturns, "moneycontrol": moneycontrol, "thangamayil": thangamayil}
