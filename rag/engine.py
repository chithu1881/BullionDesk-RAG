"""
RAG engine - turns a question into a grounded, cited, date-aware answer.

    answer("What is the highest gold price this week and how did markets react?")

Steps
  1. understand   which metals? which purity? which date window ("this week" -> Mon..today, IST)?
  2. numbers      SQL over the rates table for that window: high / low / first / last / change per source
                  (prices are computed, never left to the LLM to remember or invent)
  3. news         ChromaDB semantic search filtered to the same window and metals
  4. generate     the LLM writes the answer from steps 2+3 only, citing [n]; without an LLM key
                  a template builds the answer from the same facts ("extractive mode")
"""

import re
from datetime import date, datetime, timedelta

from kb import store
from rag import llm

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
NUM_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "ten": 10, "fourteen": 14, "thirty": 30}


# ---- 1. understand the question ---------------------------------------------------

def parse_window(q, today):
    """Returns (start, end, label). Default: the last 7 days."""
    q = q.lower()
    monday = today - timedelta(days=today.weekday())
    if "today" in q or "right now" in q or "current" in q or "latest" in q:
        return today, today, "today"
    if "yesterday" in q:
        d = today - timedelta(days=1)
        return d, d, "yesterday"
    if re.search(r"\b(last|previous|past) week\b", q) and "past week" not in q:
        return monday - timedelta(days=7), monday - timedelta(days=1), "last week"
    if "this week" in q or "past week" in q or "week so far" in q:
        if (today - monday).days < 2:                   # Mon/Tue: a 1-2 day "week" says little
            return today - timedelta(days=6), today, "the past 7 days (this week has just started)"
        return monday, today, "this week"
    if "this month" in q:
        return today.replace(day=1), today, "this month"
    if re.search(r"\b(last|previous) month\b", q):
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end, "last month"
    m = re.search(r"\b(?:last|past)\s+(\d+|\w+)\s+days?\b", q)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS.get(m.group(1), 7)
        return today - timedelta(days=n - 1), today, f"the last {n} days"
    d = _explicit_date(q, today)
    if d:
        return d, d, d.strftime("%d %b %Y")
    return today - timedelta(days=6), today, "the last 7 days"


def _explicit_date(q, today):
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", q)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for pattern in (r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3})[a-z]*\b",   # 2 Oct, 2nd of October
                    r"\b([a-z]{3})[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b"):          # Oct 2, October 2nd
        for m in re.finditer(pattern, q):
            a, b = m.groups()
            day, mon = (a, b) if a.isdigit() else (b, a)
            if mon in MONTHS and 1 <= int(day) <= 31:
                d = date(today.year, MONTHS[mon], int(day))
                return d if d <= today else d.replace(year=today.year - 1)
    return None


def parse_question(q, today):
    ql = q.lower()
    metals = [m for m in store.METALS if m in ql] or list(store.METALS)
    purity = "24K" if re.search(r"24\s*(k|kt|carat|karat)", ql) else \
             "18K" if re.search(r"18\s*(k|kt|carat|karat)", ql) else None
    start, end, label = parse_window(q, today)
    return {"metals": metals, "gold_purity": purity, "start": start, "end": end, "label": label}


# ---- 2. numbers from SQL -----------------------------------------------------------

def price_facts(p):
    rows = store.rates_between(p["start"].isoformat(), p["end"].isoformat(), p["metals"])
    if not rows:                                            # e.g. "today" before the first run of the day
        rows = store.rates_between((p["start"] - timedelta(days=3)).isoformat(), p["end"].isoformat(),
                                   p["metals"])
        p["window_note"] = "no rates stored for that exact window, so the nearest earlier days are shown"
    gold_purities = [p["gold_purity"]] if p["gold_purity"] else ["22K", "24K"]
    series = {}
    for r in rows:
        if r["metal"] == "gold" and r["purity"] not in gold_purities:
            continue
        series.setdefault((r["metal"], r["purity"], r["source"]), []).append(r)
    facts = []
    for (metal, purity, source), pts in series.items():
        hi = max(pts, key=lambda x: x["price_per_gram"])
        lo = min(pts, key=lambda x: x["price_per_gram"])
        first, last = pts[0], pts[-1]
        change = last["price_per_gram"] - first["price_per_gram"]
        facts.append({
            "metal": metal, "purity": purity, "source": source, "url": last["url"], "days": len(pts),
            "high": hi["price_per_gram"], "high_date": hi["date"], "low": lo["price_per_gram"],
            "low_date": lo["date"], "first": first["price_per_gram"], "first_date": first["date"],
            "last": last["price_per_gram"], "last_date": last["date"], "change": change,
            "change_pct": 100 * change / first["price_per_gram"] if first["price_per_gram"] else 0,
            "flag": last.get("flag") or "",
        })
    order = {m: i for i, m in enumerate(store.METALS)}
    facts.sort(key=lambda f: (order[f["metal"]], f["purity"] != "22K", f["source"] != "goodreturns", f["source"]))
    return facts


# ---- 3. news from ChromaDB -----------------------------------------------------------

def news_hits(question, p, k=8):
    start = (p["start"] - timedelta(days=1)).isoformat()      # include the evening before the window
    hits = store.search(question, start, p["end"].isoformat(), p["metals"], k=k * 2, doc_type="news")
    if len(hits) < 3:                                         # quiet day: widen, and say so
        wider = (p["end"] - timedelta(days=14)).isoformat()
        hits = store.search(question, wider, p["end"].isoformat(), p["metals"], k=k * 2, doc_type="news")
        p["news_note"] = "few stories in the window, so news from the last 14 days is included"
    best, seen = [], set()
    for h in sorted(hits, key=lambda h: h["distance"]):
        if h["url"] in seen:
            continue
        seen.add(h["url"])
        best.append(h)
        if len(best) == k:
            break
    return best


# ---- 4. generate -----------------------------------------------------------------------

SYSTEM = """You are a precious-metals market analyst for Indian users (gold, silver, platinum).
Answer ONLY from the CONTEXT. Rules:
- Every number and every claim about news must carry a citation like [1] or [2][5], using the numbers in CONTEXT.
- Prices are INR per gram unless you convert; say which purity (22K/24K) and which source.
- Always state the dates you are talking about (e.g. "on 2 Oct 2026"). Today's date is given.
- Sources can disagree (a jeweller's retail rate differs from a portal's). Mention a disagreement ONLY for a
  PRICE FACTS line that contains "flag:"; never invent one.
- Give each metal's highest price exactly as written in PRICE FACTS (value, purity, date, source).
- "How did markets react": use the news items - direction, the drivers they mention (dollar, Fed, rupee,
  geopolitics, festive demand...), MCX/COMEX and equity moves - and cite them.
- If the CONTEXT does not contain something, say "I don't have data on that" - never use outside knowledge.
- Be concise: a direct answer first, then 2-5 bullet points. No investment advice."""


def _fmt_fact(f):
    label = f"{f['metal'].title()}{' ' + f['purity'] if f['metal'] == 'gold' else ''} ({f['source']})"
    if f["days"] == 1:
        return f"{label}: ₹{f['last']:,.0f}/g on {f['last_date']}"
    return (f"{label}: high ₹{f['high']:,.0f} on {f['high_date']}, low ₹{f['low']:,.0f} on {f['low_date']}, "
            f"{f['first_date']} ₹{f['first']:,.0f} -> {f['last_date']} ₹{f['last']:,.0f} "
            f"({f['change']:+,.0f}, {f['change_pct']:+.2f}%) over {f['days']} days")


def build_sources(facts, news):
    sources, by_page = [], {}
    for f in facts:                                   # one citation number per rate page
        key = (f["source"], f["url"])
        if key not in by_page:
            by_page[key] = len(sources) + 1
            sources.append({"n": by_page[key], "kind": "rates", "title": f"{f['source']} rate page",
                            "publisher": f["source"], "url": f["url"], "date": f["last_date"]})
        f["n"] = by_page[key]
    for h in news:
        h["n"] = len(sources) + 1
        sources.append({"n": h["n"], "kind": "news", "title": h["title"], "publisher": h["publisher"],
                        "url": h["url"], "date": h["date"], "direction": h["direction"],
                        "drivers": h["drivers"], "snippet": h["text"][:500]})
    return sources


def context_text(p, facts, news, today, news_chars=1200):
    lines = [f"TODAY: {today.isoformat()} ({today.strftime('%A')}), India time",
             f"QUESTION WINDOW: {p['label']} = {p['start']} to {p['end']}"]
    for note in ("window_note", "news_note"):
        if p.get(note):
            lines.append(f"NOTE: {p[note]}")
    lines.append("\nPRICE FACTS (computed from stored daily rates, INR per gram):")
    lines += [f"[{f['n']}] {_fmt_fact(f)}{' - flag: ' + f['flag'] if f['flag'] else ''}" for f in facts] or \
             ["(no stored rates for this window)"]
    lines.append("\nNEWS:")
    for h in news:
        tags = f"direction={h['direction']}; drivers={h['drivers'] or '-'}; markets={h['markets'] or '-'}"
        lines.append(f"[{h['n']}] {h['date']} | {h['publisher']} | {h['title']} | {tags}\n{h['text'][:news_chars]}")
    if not news:
        lines.append("(no news stored for this window)")
    return "\n".join(lines)


def extractive_answer(p, facts, news):
    """Default answer (no LLM): plain, instant, still cited, built from the same facts."""
    out = [f"**Window: {p['label']} ({p['start']:%d %b} – {p['end']:%d %b %Y})**"]
    if p.get("window_note"):
        out.append(f"_Note: {p['window_note']}._")
    for metal in p["metals"]:
        fs = [f for f in facts if f["metal"] == metal]
        if not fs:
            out.append(f"\n**{metal.title()}** – I don't have rates for this window.")
            continue
        top = max(fs, key=lambda f: f["high"])
        out.append(f"\n**{metal.title()}** – highest: ₹{top['high']:,.0f}/g"
                   f"{' (' + top['purity'] + ')' if metal == 'gold' else ''} on {top['high_date']} "
                   f"per {top['source']} [{top['n']}].")
        out += [f"- {_fmt_fact(f)} [{f['n']}]" for f in fs]
    if news:
        out.append("\n**How the market reacted (from the news):**")
        for h in news[:5]:
            arrow = {"up": "▲", "down": "▼", "mixed": "↕"}.get(h["direction"], "•")
            why = f" — drivers: {h['drivers'].replace(',', ', ')}" if h["drivers"] else ""
            out.append(f"- {arrow} {h['date']}: {h['title']} ({h['publisher']}){why} [{h['n']}]")
    else:
        out.append("\nI don't have news on this window.")
    out.append("\n_Built directly from the stored rates and news. Turn on 'Write with local LLM' for a written analysis._")
    return "\n".join(out)


def answer(question, now=None, history=None, use_llm=False):
    """use_llm=True asks the local Ollama model to write the answer (slower on CPU, ~1-4 min);
    otherwise the extractive answer is returned instantly from the same facts."""
    today = (now or datetime.now(store_tz())).date()
    p = parse_question(question, today)
    facts = price_facts(p)
    news = news_hits(question, p, k=8 if not use_llm else 6)
    sources = build_sources(facts, news)
    ctx = context_text(p, facts, news, today, news_chars=600)
    mode = "extractive"
    text = ""
    if use_llm and llm.available() and (facts or news):
        convo = ""
        if history:
            convo = "EARLIER IN THIS CHAT:\n" + "\n".join(f"{m['role']}: {m['content'][:400]}" for m in history[-4:]) + "\n\n"
        text = llm.chat(SYSTEM, f"{convo}CONTEXT:\n{ctx}\n\nQUESTION: {question}", max_tokens=1200)
        mode = llm.describe() if text else mode
    if not text:
        text = extractive_answer(p, facts, news)
    used = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    return {"answer": text, "sources": [s for s in sources if s["n"] in used] or sources,
            "facts": facts, "window": {"label": p["label"], "start": p["start"].isoformat(),
                                       "end": p["end"].isoformat()},
            "metals": p["metals"], "mode": mode, "context": ctx}


def store_tz():
    from agents.scrapers import IST
    return IST
