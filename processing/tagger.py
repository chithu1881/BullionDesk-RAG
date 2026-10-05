"""
Tagging step - rule-based, so it is free, fast and every tag can be explained.

Each article gets:
  metals     gold / silver / platinum mentioned in the text (added to what the agent found)
  direction  up / down / mixed / none    - did the story say prices rose or fell?
  drivers    why prices moved: fed_rates, us_dollar, rupee, geopolitics, festive_demand, ...
  markets    which markets are discussed: mcx, comex, equities, etf, crude
  type       rates_update / analysis_outlook / news

Tags are stored as metadata in the knowledge base, so the chat can filter on them and show them.
"""

import re

DIRECTION = {
    "up": r"\b(ris(e|es|ing)|rose|gain(s|ed)?|jump(s|ed)?|surg(e|es|ed)|rall(y|ies|ied)|climb(s|ed)?|"
          r"soar(s|ed)?|up \d|record high|hike[sd]?|rebound(s|ed)?|recover(s|ed|y))\b",
    "down": r"\b(fall(s|ing)?|fell|drop(s|ped)?|slip(s|ped)?|declin(e|es|ed)|plung(e|es|ed)|slump(s|ed)?|"
            r"tumbl(e|es|ed)|sink(s)?|sank|down \d|cheaper|crash(es|ed)?|dip(s|ped)?|eas(e|es|ed))\b",
}

DRIVERS = {
    "fed_rates": r"\b(fed|federal reserve|rate cut|rate hike|interest rate|fomc|powell)\b",
    "us_dollar": r"\b(dollar|dxy|greenback)\b",
    "rupee": r"\b(rupee|inr)\b",
    "inflation": r"\binflation|cpi\b",
    "geopolitics": r"\b(war|conflict|tension|geopolitic\w*|iran|israel|russia|ukraine|middle east|sanction\w*)\b",
    "safe_haven": r"\bsafe[- ]haven\b",
    "festive_demand": r"\b(diwali|dhanteras|festive|festival|wedding|navratri|akshaya tritiya)\b",
    "central_banks": r"\b(central bank\w*|rbi|pboc|reserve bank)\b",
    "tariffs_trade": r"\b(tariff\w*|trade war|import duty|customs duty)\b",
    "jobs_data": r"\b(jobs data|payroll\w*|unemployment|jobless)\b",
    "profit_booking": r"\b(profit[- ]booking|profit[- ]taking)\b",
    "supply": r"\b(supply|mine|mining|shortage|deficit|squeeze)\b",
    "industrial_demand": r"\b(industrial demand|solar|ev\b|electric vehicle|auto\w* demand|catalytic)\b",
}

MARKETS = {
    "mcx": r"\bmcx\b",
    "comex": r"\b(comex|spot gold|spot silver|ounce|oz)\b",
    "equities": r"\b(sensex|nifty|stock market|equities|equity market|shares)\b",
    "etf": r"\betf\w*\b",
    "crude": r"\b(crude|brent|oil price\w*)\b",
}

METAL_WORDS = {"gold": r"\bgold\b", "silver": r"\bsilver\b", "platinum": r"\bplatinum\b"}


def _hits(patterns, text):
    return [name for name, p in patterns.items() if re.search(p, text, re.I)]


def tag(article):
    text = f"{article['title']} {article['text'][:4000]}"
    head = f"{article['title']} {article['text'][:400]}"      # direction comes from the lead only
    up = bool(re.search(DIRECTION["up"], head, re.I))
    down = bool(re.search(DIRECTION["down"], head, re.I))
    article["direction"] = "mixed" if up and down else "up" if up else "down" if down else "none"
    article["metals"] = sorted(set(article["metals"]) | set(_hits(METAL_WORDS, text)))
    article["drivers"] = _hits(DRIVERS, text)
    article["markets"] = _hits(MARKETS, text)
    title = article["title"].lower()
    if re.search(r"(price|rate)s? today|check (the )?(latest )?rates?|rate in \w+ today", title):
        article["type"] = "rates_update"
    elif re.search(r"outlook|forecast|predict|should you|where should|target|analyst|experts?", title):
        article["type"] = "analysis_outlook"
    else:
        article["type"] = "news"
    return article


def tag_all(articles):
    return [tag(a) for a in articles]
