"""
The shared knowledge base. Two stores, one folder (data/):

  data/metals.db  (SQLite)   exact numbers - every rate row, plus article list and run reports.
                             "Highest price this week" is answered from here with SQL, never guessed.
  data/chroma     (ChromaDB) text for semantic search - news chunks and one "daily rates" summary
                             per metal per day, each with date + metal + tag metadata for filtering.

Embeddings: Chroma's built-in all-MiniLM-L6-v2 (open-source, runs locally on CPU, no API key).

On Streamlit Community Cloud the disk is temporary, so GitHub stores the knowledge base instead:
a GitHub Actions job (.github/workflows/collect.yml) runs the agents twice a day and saves data/ as
kb.zip on the repo's "kb-data" branch. The cloud app downloads it (sync_from_github) and re-checks
every 10 minutes. KB_SOURCE=local|github overrides the automatic choice.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import time
import zipfile
from contextlib import closing
from pathlib import Path

import chromadb

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
KB_URL = os.getenv("KB_URL", "https://raw.githubusercontent.com/chithu1881/BullionDesk-RAG/kb-data/kb.zip")
METALS = ("gold", "silver", "platinum")
MAIN_PURITY = {"gold": "22K", "silver": "999", "platinum": "999"}   # what "the gold price" means in India
CHUNK_CHARS, CHUNK_OVERLAP = 900, 150

SCHEMA = """
CREATE TABLE IF NOT EXISTS rates (
    metal TEXT, purity TEXT, date TEXT, source TEXT, price_per_gram REAL, url TEXT,
    kind TEXT, flag TEXT, fetched_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (metal, purity, date, source));
CREATE TABLE IF NOT EXISTS articles (
    id TEXT PRIMARY KEY, title TEXT, url TEXT, publisher TEXT, published TEXT, date TEXT,
    metals TEXT, direction TEXT, drivers TEXT, markets TEXT, type TEXT, chars INTEGER);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, started TEXT, report TEXT);
"""

# The folder in use. Locally always data/; on the cloud a fresh folder per downloaded kb.zip version,
# so an open Chroma client never sees its files replaced underneath it.
_active = {"dir": DATA_DIR, "etag": None, "checked": 0.0, "updated": None}
_collection = None


def source():
    wanted = os.getenv("KB_SOURCE", "").lower()
    if wanted in ("local", "github"):
        return wanted
    return "github" if Path("/mount/src").exists() else "local"      # /mount/src = Streamlit Cloud


def sync_from_github(every_seconds=600):
    """Cloud only: download kb.zip if GitHub has a newer one. Returns True if the data changed."""
    global _collection
    if source() != "github" or time.time() - _active["checked"] < every_seconds:
        return False
    _active["checked"] = time.time()
    import requests
    try:
        head = requests.head(KB_URL, timeout=20, allow_redirects=True)
        etag = head.headers.get("ETag", "").strip('"') or str(int(time.time()))
        if head.status_code != 200 or etag == _active["etag"]:
            return False
        r = requests.get(KB_URL, timeout=300)
        r.raise_for_status()
    except Exception as e:
        print(f"kb sync failed: {e}")
        return False
    version = hashlib.sha1(etag.encode()).hexdigest()[:12]   # ETags can look like W/"abc" - not a folder name
    target = DATA_DIR / "github" / version
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    zip_path = target.parent / f"{version}.zip"
    zip_path.write_bytes(r.content)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(target)
    zip_path.unlink()
    _active.update(dir=target, etag=etag, updated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    _collection = None
    return True


def active_dir():
    if source() == "github" and _active["etag"] is None:
        sync_from_github(every_seconds=0)          # first use on the cloud: download before opening
    return _active["dir"]


def db():
    folder = active_dir()
    folder.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(folder / "metals.db")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def collection():
    global _collection
    if _collection is None:
        client = chromadb.PersistentClient(path=str(active_dir() / "chroma"))
        _collection = client.get_or_create_collection("knowledge", metadata={"hnsw:space": "cosine"})
    return _collection


def date_int(iso):
    return int(iso.replace("-", ""))


# ---- writes ----------------------------------------------------------------------

def save_rates(rates):
    with closing(db()) as con, con:
        for r in rates:
            # history rows never overwrite a value we captured as "today" on that day
            con.execute("""
                INSERT INTO rates (metal, purity, date, source, price_per_gram, url, kind, flag)
                VALUES (:metal, :purity, :date, :source, :price_per_gram, :url, :kind, :flag)
                ON CONFLICT (metal, purity, date, source) DO UPDATE SET
                    price_per_gram = excluded.price_per_gram, url = excluded.url, flag = excluded.flag,
                    kind = excluded.kind, fetched_at = datetime('now')
                WHERE excluded.kind = 'today' OR rates.kind = 'history'""", r)
    return len(rates)


def _chunks(text):
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        cut = text.rfind(". ", start + CHUNK_CHARS // 2, end)      # prefer to end on a sentence
        end = cut + 1 if cut != -1 and end < len(text) else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = end - CHUNK_OVERLAP
    return out


def _metal_flags(metals):
    return {f"metal_{m}": m in metals for m in METALS}


def save_articles(articles):
    """Upserts articles into SQLite + Chroma. Returns how many were new."""
    with closing(db()) as con:
        known = {row[0] for row in con.execute("SELECT id FROM articles")}
    new = [a for a in articles if a["id"] not in known]
    ids, docs, metas = [], [], []
    for a in new:
        for i, chunk in enumerate(_chunks(a["text"])):
            ids.append(f"{a['id']}-{i}")
            docs.append(f"{a['title']}\n{chunk}" if i == 0 or a["title"] not in chunk else chunk)
            metas.append({
                "doc_type": "news", "title": a["title"], "url": a["url"], "publisher": a["publisher"],
                "date": a["date"], "date_int": date_int(a["date"]), "published": a["published"],
                "metals": ",".join(a["metals"]), **_metal_flags(a["metals"]),
                "direction": a["direction"], "drivers": ",".join(a["drivers"]),
                "markets": ",".join(a["markets"]), "type": a["type"],
            })
    for start in range(0, len(ids), 200):
        collection().upsert(ids=ids[start:start + 200], documents=docs[start:start + 200],
                            metadatas=metas[start:start + 200])
    with closing(db()) as con, con:
        for a in new:
            con.execute("INSERT OR REPLACE INTO articles VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (a["id"], a["title"], a["url"], a["publisher"], a["published"], a["date"],
                         ",".join(a["metals"]), a["direction"], ",".join(a["drivers"]),
                         ",".join(a["markets"]), a["type"], len(a["text"])))
    return {"articles_new": len(new), "chunks_added": len(ids)}


def refresh_rate_docs(days):
    """One searchable text doc per metal per day, e.g. 'Gold rates on 2026-10-05 (per gram) ...'.
    Lets the chat cite a rate the same way it cites a news story."""
    ids, docs, metas = [], [], []
    with closing(db()) as con:
        for metal in METALS:
            for day in days:
                rows = con.execute("SELECT * FROM rates WHERE metal=? AND date=? ORDER BY purity, source",
                                   (metal, day)).fetchall()
                if not rows:
                    continue
                lines = [f"{metal.title()} rates in India on {day} (INR per gram):"]
                for r in rows:
                    prev = con.execute("""SELECT price_per_gram, date FROM rates WHERE metal=? AND purity=?
                                          AND source=? AND date<? ORDER BY date DESC LIMIT 1""",
                                       (metal, r["purity"], r["source"], day)).fetchone()
                    change = ""
                    if prev:
                        diff = r["price_per_gram"] - prev["price_per_gram"]
                        change = f" ({diff:+,.0f} vs {prev['date']})"
                    label = f"{r['purity']} " if metal == "gold" else ""
                    lines.append(f"- {label}{r['source']}: ₹{r['price_per_gram']:,.0f}{change}")
                ids.append(f"rate-{metal}-{day}")
                docs.append("\n".join(lines))
                metas.append({"doc_type": "rate", "title": f"{metal.title()} rates on {day}",
                              "url": rows[0]["url"], "publisher": ", ".join(sorted({r['source'] for r in rows})),
                              "date": day, "date_int": date_int(day), "published": day,
                              "metals": metal, **_metal_flags([metal]), "direction": "", "drivers": "",
                              "markets": "", "type": "rates"})
    if ids:
        collection().upsert(ids=ids, documents=docs, metadatas=metas)
    return len(ids)


def save_run(run_id, started, report):
    with closing(db()) as con, con:
        con.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)", (run_id, started, json.dumps(report)))


# ---- reads -----------------------------------------------------------------------

def rates_between(start, end, metals=METALS):
    q = f"""SELECT metal, purity, date, source, price_per_gram, url, flag FROM rates
            WHERE date BETWEEN ? AND ? AND metal IN ({','.join('?' * len(metals))})
            ORDER BY metal, purity, source, date"""
    with closing(db()) as con:
        return [dict(r) for r in con.execute(q, (start, end, *metals))]


def latest_rates():
    with closing(db()) as con:
        return [dict(r) for r in con.execute("""
            SELECT r.* FROM rates r JOIN (SELECT metal, purity, source, MAX(date) d FROM rates
                                          GROUP BY metal, purity, source) m
            ON r.metal=m.metal AND r.purity=m.purity AND r.source=m.source AND r.date=m.d
            ORDER BY r.metal, r.purity, r.source""")]


def recent_articles(limit=200):
    with closing(db()) as con:
        return [dict(r) for r in con.execute("SELECT * FROM articles ORDER BY published DESC LIMIT ?", (limit,))]


def runs(limit=30):
    with closing(db()) as con:
        return [{"run_id": r["run_id"], "started": r["started"], **json.loads(r["report"])}
                for r in con.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,))]


def search(query, start, end, metals, k=8, doc_type=None):
    """Semantic search restricted to a date window and the asked-about metals."""
    where = [{"date_int": {"$gte": date_int(start)}}, {"date_int": {"$lte": date_int(end)}}]
    if metals and len(metals) < len(METALS):
        flags = [{f"metal_{m}": True} for m in metals]
        where.append(flags[0] if len(flags) == 1 else {"$or": flags})
    if doc_type:
        where.append({"doc_type": doc_type})
    if collection().count() == 0:
        return []
    res = collection().query(query_texts=[query], n_results=k, where={"$and": where})
    return [{"id": i, "text": d, "distance": dist, **m}
            for i, d, dist, m in zip(res["ids"][0], res["documents"][0], res["distances"][0], res["metadatas"][0])]


def stats():
    with closing(db()) as con:
        r = con.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM rates").fetchone()
        a = con.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"rate_rows": r[0], "first_date": r[1], "last_date": r[2], "articles": a,
            "chunks": collection().count()}


def today_iso():
    from agents.scrapers import today_ist
    return today_ist().isoformat()

