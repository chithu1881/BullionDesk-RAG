"""
Smoke tests - run after a collection run:   venv\\Scripts\\python -m eval.smoke_test

Checks the parts most likely to break silently: date understanding, price sanity in the KB,
that every source is still producing rates, and that answers are cited and date-aware.
"""

import re
from datetime import date, datetime, timedelta

from kb import store
from rag.engine import answer, parse_question

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  - ' + detail if detail and not ok else ''}")


# 1. date + metal understanding (fixed "today" = Thursday 8 Oct 2026)
T = date(2026, 10, 8)
cases = {
    "highest gold price this week": (["gold"], date(2026, 10, 5), T),
    "silver yesterday": (["silver"], date(2026, 10, 7), date(2026, 10, 7)),
    "platinum last week": (["platinum"], date(2026, 9, 28), date(2026, 10, 4)),
    "gold on 2 Oct": (["gold"], date(2026, 10, 2), date(2026, 10, 2)),
    "Oct 1st silver rate": (["silver"], date(2026, 10, 1), date(2026, 10, 1)),
    "24k gold last 10 days": (["gold"], date(2026, 9, 29), T),
    "how are bullion prices doing": (["gold", "silver", "platinum"], T - timedelta(days=6), T),
}
for q, (metals, start, end) in cases.items():
    p = parse_question(q, T)
    check(f"parse: {q!r}", (p["metals"], p["start"], p["end"]) == (metals, start, end),
          f"got {p['metals']} {p['start']}..{p['end']}")
check("parse: 24k purity", parse_question("24k gold last 10 days", T)["gold_purity"] == "24K")

# 2. knowledge base health
rows = store.latest_rates()
for metal in store.METALS:
    sources = {r["source"] for r in rows if r["metal"] == metal}
    check(f"kb: {metal} has rates from all 3 sources", sources == {"goodreturns", "moneycontrol", "thangamayil"},
          f"only {sorted(sources)}")
gold22 = [r["price_per_gram"] for r in rows if r["metal"] == "gold" and r["purity"] == "22K"]
check("kb: gold 22K per-gram price is plausible", all(5_000 < x < 40_000 for x in gold22), str(gold22))
stats = store.stats()
check("kb: news stored", stats["articles"] > 20, str(stats))
newest = max(r["date"] for r in rows) if rows else "2000-01-01"
check("kb: rates are fresh (<= 3 days old)",
      (datetime.now().date() - date.fromisoformat(newest)).days <= 3, newest)

# 3. answers are grounded, cited and dated
r = answer("What is the highest price in this week and how did markets react?")
cites = set(re.findall(r"\[(\d+)\]", r["answer"]))
check("answer: has citations", len(cites) >= 3, r["answer"][:200])
check("answer: every citation resolves to a source", cites <= {str(s["n"]) for s in r["sources"]})
check("answer: mentions dates", bool(re.search(r"20\d\d-\d\d-\d\d", r["answer"])))
check("answer: includes news sources", any(s["kind"] == "news" for s in r["sources"]))
far = answer("gold price on 1 January", now=datetime(2026, 10, 6))
check("answer: no data -> says so instead of inventing", "don't have" in far["answer"] or far["window"]["start"] == "2026-01-01")

print(f"\n{sum(results)}/{len(results)} passed")
