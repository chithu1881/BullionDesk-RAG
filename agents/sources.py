"""
Where each metal agent looks. One entry per metal; every metal has the same three rate sources
(goodreturns, moneycontrol, a jeweller website) plus a few news feeds.

To add a source: add its URL here and (for a rate page) a parser in agents/scrapers.py.
"""

# Full-text business feeds shared by all agents; each agent keeps only items that mention its metal.
# (Google News feeds give only headlines, so these carry the "how did markets react" detail.)
COMMON_FEEDS = [
    "https://economictimes.indiatimes.com/markets/commodities/rssfeeds/1808152121.cms",
    "https://www.thehindubusinessline.com/markets/gold/feeder/default.rss",
    "https://www.business-standard.com/rss/markets/commodities-10608.rss",
    "https://www.livemint.com/rss/markets",
]

# Prices are always stored per gram in INR. "purity" is 24K/22K/18K for gold, 999 for silver/platinum.
METALS = {
    "gold": {
        "rate_pages": {
            "goodreturns": "https://www.goodreturns.in/gold-rates/",
            "moneycontrol": "https://www.moneycontrol.com/news/gold-rates-today/",
            "thangamayil": "https://www.thangamayil.com/",
        },
        "news_feeds": [
            "https://news.google.com/rss/search?q=gold+price+site:moneycontrol.com+when:7d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=gold+rate+site:goodreturns.in+when:7d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=gold+price+India+when:2d&hl=en-IN&gl=IN&ceid=IN:en",
        ] + COMMON_FEEDS,
        "keywords": ["gold", "bullion", "sovereign gold bond", "gold etf", "mcx gold"],
    },
    "silver": {
        "rate_pages": {
            "goodreturns": "https://www.goodreturns.in/silver-rates/",
            "moneycontrol": "https://www.moneycontrol.com/news/silver-rates-today/",
            "thangamayil": "https://www.thangamayil.com/",
        },
        "news_feeds": [
            "https://news.google.com/rss/search?q=silver+price+site:moneycontrol.com+when:7d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=silver+rate+site:goodreturns.in+when:7d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=silver+price+India+when:2d&hl=en-IN&gl=IN&ceid=IN:en",
        ] + COMMON_FEEDS,
        "keywords": ["silver", "mcx silver", "silver etf"],
    },
    "platinum": {
        "rate_pages": {
            "goodreturns": "https://www.goodreturns.in/platinum-price.html",
            "moneycontrol": "https://www.moneycontrol.com/news/platinum-rates-today/",
            "thangamayil": "https://www.thangamayil.com/",
        },
        "news_feeds": [
            "https://news.google.com/rss/search?q=platinum+price+site:moneycontrol.com+when:14d&hl=en-IN&gl=IN&ceid=IN:en",
            "https://news.google.com/rss/search?q=platinum+price+when:7d&hl=en-IN&gl=IN&ceid=IN:en",
        ] + COMMON_FEEDS,
        "keywords": ["platinum", "palladium", "pgm"],
        # "platinum" is also a brand word: credit cards, fund names, Platinum Industries shares...
        "exclude": ["industries", "fund", "card", "ipo", "shares list", "plan", "membership", "jubilee"],
    },
}

# Some sites block browser-looking clients but allow plain ones (moneycontrol), others the reverse.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128 Safari/537.36")
USER_AGENT_FOR = {"moneycontrol": "curl/8.0"}

MAX_NEWS_PER_FEED = 15
