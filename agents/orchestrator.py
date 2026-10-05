"""
Orchestrator - a LangGraph graph that runs the whole collection pipeline.

                 START
       ┌───────────┼────────────┐
       ▼           ▼            ▼
  gold_agent  silver_agent  platinum_agent     run in parallel; each scrapes goodreturns,
       └───────────┼────────────┘              moneycontrol, Thangamayil + its news feeds
                   ▼
                 clean                          validate prices, merge duplicate stories, full text
                   ▼
                  tag                           direction / drivers / markets / type tags
                   ▼
                 store                          SQLite (numbers) + ChromaDB (text) = shared KB
                   ▼
                report                          run report -> runs table (shown in the app)
                   ▼
                  END

A failing source is skipped inside its agent; a crashing agent is logged and the run continues.

    venv\\Scripts\\python -m agents.orchestrator              # run once now
    venv\\Scripts\\python -m agents.orchestrator --schedule   # daily at RUN_TIMES (IST), via APScheduler
"""

import argparse
import logging
import operator
import os
import uuid
from datetime import datetime, timedelta
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from agents.metal_agent import MetalAgent
from agents.scrapers import IST
from kb import store
from processing.cleaner import clean_articles, clean_rates
from processing.tagger import tag_all

load_dotenv()
RUN_TIMES = os.getenv("RUN_TIMES", "10:15,17:30")      # jewellers publish around 9:30-10:00 IST

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)
log = logging.getLogger("orchestrator")


class RunState(TypedDict, total=False):
    run_id: str
    started: str
    rates: Annotated[list, operator.add]          # every agent appends its raw rows here...
    articles: Annotated[list, operator.add]       # ...and its raw news items here
    agent_reports: Annotated[list, operator.add]
    clean_rates: list                             # after the clean node
    clean_articles: list                          # after clean + tag
    clean_report: dict
    store_report: dict
    run_report: dict


def make_agent_node(metal):
    def node(state: RunState):
        try:
            rates, articles, report = MetalAgent(metal).run()
            return {"rates": rates, "articles": articles, "agent_reports": [report]}
        except Exception as e:                    # one agent failing must not stop the others
            log.exception("%s agent crashed", metal)
            return {"agent_reports": [{"agent": f"{metal}_agent", "rates": 0, "news": 0, "errors": [str(e)]}]}
    node.__name__ = f"{metal}_agent"
    return node


def clean(state: RunState):
    rates, r1 = clean_rates(state.get("rates", []))
    articles, r2 = clean_articles(state.get("articles", []))
    return {"clean_rates": rates, "clean_articles": articles, "clean_report": {**r1, **r2}}


def tag(state: RunState):
    return {"clean_articles": tag_all(state["clean_articles"])}


def store_kb(state: RunState):
    saved = store.save_rates(state["clean_rates"])
    result = store.save_articles(state["clean_articles"])
    days = sorted({r["date"] for r in state["clean_rates"]})
    result.update(rates_saved=saved, rate_docs=store.refresh_rate_docs(days))
    return {"store_report": result}


def report(state: RunState):
    started = datetime.fromisoformat(state["started"])
    result = {
        "agents": state.get("agent_reports", []),
        "clean": state.get("clean_report", {}),
        "store": state.get("store_report", {}),
        "seconds": round((datetime.now(IST) - started).total_seconds(), 1),
        "kb": store.stats(),
    }
    store.save_run(state["run_id"], state["started"], result)
    return {"run_report": result}


def build_graph():
    g = StateGraph(RunState)
    for metal in store.METALS:
        g.add_node(f"{metal}_agent", make_agent_node(metal))
        g.add_edge(START, f"{metal}_agent")
    g.add_node("clean", clean)
    g.add_node("tag", tag)
    g.add_node("store", store_kb, retry_policy=RetryPolicy(max_attempts=3))
    g.add_node("report", report)
    g.add_edge([f"{m}_agent" for m in store.METALS], "clean")   # waits for all three agents
    g.add_edge("clean", "tag")
    g.add_edge("tag", "store")
    g.add_edge("store", "report")
    g.add_edge("report", END)
    return g.compile()


def run_once():
    started = datetime.now(IST)
    run_id = started.strftime("%Y%m%d-%H%M") + "-" + uuid.uuid4().hex[:4]
    log.info("run %s started", run_id)
    final = build_graph().invoke({"run_id": run_id, "started": started.isoformat(timespec="seconds")})
    result = final["run_report"]
    log.info("run %s done in %ss: %s | %s", run_id, result["seconds"], result["clean"], result["store"])
    return result


def schedule():
    from apscheduler.schedulers.blocking import BlockingScheduler
    sched = BlockingScheduler(timezone=IST)
    for hhmm in RUN_TIMES.split(","):
        h, m = hhmm.strip().split(":")
        sched.add_job(run_once, "cron", hour=int(h), minute=int(m), misfire_grace_time=3600,
                      id=f"collect-{h}{m}")
    log.info("scheduler started - daily runs at %s IST (Ctrl+C to stop)", RUN_TIMES)
    sched.add_job(run_once, "date", run_date=datetime.now(IST) + timedelta(seconds=5))   # one run now
    sched.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--schedule", action="store_true", help="keep running, collect daily at RUN_TIMES")
    args = parser.parse_args()
    schedule() if args.schedule else run_once()
