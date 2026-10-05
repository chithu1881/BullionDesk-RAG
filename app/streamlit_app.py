"""
Streamlit front end.   venv\\Scripts\\streamlit run app/streamlit_app.py

Tabs: Ask (RAG chat) | Rates (today + 30-day chart) | News knowledge base | Pipeline runs
Sidebar: knowledge-base stats, LLM mode, "Run the 3 agents now".
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import altair as alt
import pandas as pd
import streamlit as st

from kb import store
from rag import engine, llm

st.set_page_config(page_title="Bullion Desk – Gold, Silver & Platinum", page_icon="🪙", layout="wide")

EXAMPLES = [
    "What is the highest price in this week and how did markets react?",
    "How did the gold 22K rate move in the last 10 days and why?",
    "Compare silver rates across sources today",
    "What happened to platinum on 2 Oct?",
]

store.sync_from_github()          # cloud only: pick up a newer kb.zip (checked at most every 10 min)

# ---- sidebar ------------------------------------------------------------------
with st.sidebar:
    st.header("🪙 Bullion Desk")
    s = store.stats()
    st.caption(f"Rates **{s['first_date'] or '-'} → {s['last_date'] or '-'}**")
    c1, c2 = st.columns(2)
    c1.metric("Rate rows", s["rate_rows"])
    c2.metric("Articles", s["articles"])
    st.caption(f"{s['chunks']} searchable chunks in ChromaDB")
    st.divider()
    use_llm = st.toggle("Write with local LLM", value=False, disabled=not llm.available(),
                        help="Open-source model via Ollama on this PC - no API key. Slower on CPU (1-4 min).")
    st.caption("LLM: " + llm.describe())
    if store.source() == "github":
        st.caption("☁️ Data is collected by GitHub Actions at 10:15 and 17:30 IST "
                   f"([runs](https://github.com/chithu1881/BullionDesk-RAG/actions)). "
                   f"Downloaded: {store._active['updated'] or 'not yet'}")
    elif st.button("▶ Run the 3 agents now", use_container_width=True):
        from agents.orchestrator import run_once
        with st.spinner("Gold, silver and platinum agents are collecting… (~1 min)"):
            result = run_once()
        st.success(f"Done in {result['seconds']}s – {result['store'].get('articles_new', 0)} new articles, "
                   f"{result['store'].get('rates_saved', 0)} rate rows")
        st.rerun()

ask, rates_tab, news_tab, runs_tab = st.tabs(["💬 Ask", "📈 Rates", "📰 News knowledge base", "⚙️ Pipeline runs"])

# ---- Ask ----------------------------------------------------------------------
with ask:
    if "chat" not in st.session_state:
        st.session_state.chat = []

    cols = st.columns(len(EXAMPLES))
    for col, ex in zip(cols, EXAMPLES):
        if col.button(ex, use_container_width=True):
            st.session_state.pending = ex

    for msg in st.session_state.chat:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("result"):
                r = msg["result"]
                st.caption(f"Window: {r['window']['label']} ({r['window']['start']} → {r['window']['end']}) · "
                           f"{r['mode']}")
                with st.expander(f"Sources ({len(r['sources'])})"):
                    for src in r["sources"]:
                        badge = "📊" if src["kind"] == "rates" else "📰"
                        st.markdown(f"**[{src['n']}]** {badge} [{src['title']}]({src['url']}) · "
                                    f"{src['publisher']} · {src['date']}")

    question = st.chat_input("Ask about gold, silver or platinum prices and the news behind them…")
    question = question or st.session_state.pop("pending", None)
    if question:
        history = [{"role": m["role"], "content": m["content"]} for m in st.session_state.chat]
        st.session_state.chat.append({"role": "user", "content": question})
        note = " (the local LLM is writing – this can take a few minutes)" if use_llm else ""
        with st.spinner("Reading the rates and the news…" + note):
            result = engine.answer(question, history=history, use_llm=use_llm)
        st.session_state.chat.append({"role": "assistant", "content": result["answer"], "result": result})
        st.rerun()

# ---- Rates --------------------------------------------------------------------
with rates_tab:
    latest = pd.DataFrame(store.latest_rates())
    if latest.empty:
        st.info("No rates yet – click **Run the 3 agents now** in the sidebar.")
    else:
        for metal in store.METALS:
            st.subheader(metal.title())
            df = latest[latest.metal == metal]
            purity = store.MAIN_PURITY[metal]
            main = df[df.purity == purity]
            cols = st.columns(max(len(main), 1))
            for col, (_, r) in zip(cols, main.iterrows()):
                col.metric(f"{r.source} · {purity} · {r.date}", f"₹{r.price_per_gram:,.0f}/g",
                           help=r.flag or None)
        st.divider()
        metal = st.radio("Trend", store.METALS, horizontal=True, format_func=str.title)
        hist = pd.DataFrame(store.rates_between("2000-01-01", "2100-01-01", (metal,)))
        hist = hist[hist.purity == store.MAIN_PURITY[metal]]
        hist["date"] = pd.to_datetime(hist["date"])
        chart = alt.Chart(hist).mark_line(point=True).encode(
            x=alt.X("date:T", title=None), y=alt.Y("price_per_gram:Q", title="₹ per gram", scale=alt.Scale(zero=False)),
            color=alt.Color("source:N", title="Source"),
            tooltip=["date:T", "source", alt.Tooltip("price_per_gram:Q", format=",.0f")])
        st.altair_chart(chart, use_container_width=True)
        st.caption("Jeweller (Thangamayil) prices are retail board rates; portals show market reference "
                   "rates, so they can differ – rows more than 15% apart are flagged.")

# ---- News KB ------------------------------------------------------------------
with news_tab:
    arts = pd.DataFrame(store.recent_articles(300))
    if arts.empty:
        st.info("No articles yet.")
    else:
        c1, c2, c3 = st.columns(3)
        metal = c1.selectbox("Metal", ["all", *store.METALS])
        direction = c2.selectbox("Direction", ["all", "up", "down", "mixed", "none"])
        kind = c3.selectbox("Type", ["all", "news", "analysis_outlook", "rates_update"])
        view = arts
        if metal != "all":
            view = view[view.metals.str.contains(metal)]
        if direction != "all":
            view = view[view.direction == direction]
        if kind != "all":
            view = view[view.type == kind]
        st.dataframe(view[["date", "title", "publisher", "metals", "direction", "drivers", "markets", "type", "url"]],
                     use_container_width=True, hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("link", display_text="open")})

# ---- Runs ---------------------------------------------------------------------
with runs_tab:
    for run in store.runs():
        with st.expander(f"{run['started']} · {run['seconds']}s · "
                         f"{run['store'].get('articles_new', 0)} new articles · {run['store'].get('rates_saved', 0)} rates"):
            st.dataframe(pd.DataFrame([{"agent": a["agent"], "rates": a.get("rates", 0), "news": a.get("news", 0),
                                        **{f"src:{k}": v for k, v in a.get("rate_sources", {}).items()},
                                        "errors": "; ".join(a.get("errors", []))} for a in run["agents"]]),
                         hide_index=True, use_container_width=True)
            st.json({"clean": run["clean"], "store": run["store"], "kb": run["kb"]}, expanded=False)
