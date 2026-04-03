#!/usr/bin/env python3
"""
Morning Brief — daily RSS digest for LinkedIn content ideas.
Fetches Google News articles, scores them via Claude, posts top 3 to Slack.

Cron: 0 8 * * * /usr/bin/env python3 /path/to/morning_brief.py
"""

import os
import sys
import re
import json
import html
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import feedparser
import anthropic
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOPICS = {
    "Video Production & Advertising": "video production advertising",
    "Business of Filmmaking": "business of filmmaking",
    "AI in Filmmaking": "AI filmmaking",
    "Filmmaking Tools & Gear": "filmmaking tools gear",
    "SF Film Scene": "San Francisco film scene",
    "Venture Capital & PE": "venture capital private equity tech deal",
    "Tech Brand Marketing": "tech brand content marketing campaign",
    "Silicon Valley Business": "Silicon Valley startup founder",
    "Creator Economy": "creator economy brand deal content monetization",
}

# Direct RSS feeds from trusted outlets
DIRECT_FEEDS = {
    "Ad Age": "https://adage.com/rss/rss.xml",
    "The Economist": "https://www.economist.com/rss/the_world_this_week_rss.xml",
    "Fast Company": "https://www.fastcompany.com/latest/rss",
    "Wired": "https://www.wired.com/feed/rss",
    "Vice": "https://www.vice.com/en/rss",
}

ARTICLES_PER_FEED = 15   # how many articles to pull per topic
TOP_N = 6                # how many to surface to Slack
MAX_AGE_DAYS = 2         # ignore articles older than this

AUDIENCE_CONTEXT = (
    "Creative directors, video producers, independent filmmakers, and founders "
    "based in San Francisco, Los Angeles, and New York City. They care about "
    "the business and craft of filmmaking, emerging production technology, "
    "AI tools, gear, and the creative economy. "
    "Also: marketing and growth leaders at AI startups who are investing in "
    "video content, exploring AI-generated media, and looking for angles that "
    "connect their product narratives to the future of creative production."
)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def google_news_url(query: str) -> str:
    return (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )


def resolve_url(google_url: str) -> str:
    """Follow Google News redirect to get the real article URL."""
    try:
        resp = requests.get(
            google_url,
            allow_redirects=True,
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        return resp.url
    except Exception:
        return google_url  # fall back to original if resolution fails


def fetch_articles(topics: dict[str, str], per_feed: int) -> list[dict]:
    raw_articles = []
    seen_google_urls: set[str] = set()

    for label, query in topics.items():
        url = google_news_url(query)
        log.info("Fetching: %s", label)
        feed = feedparser.parse(url)

        if feed.bozo:
            log.warning("Feed parse warning for '%s': %s", label, feed.bozo_exception)

        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

        for entry in feed.entries[:per_feed]:
            link = entry.get("link", "")
            if link in seen_google_urls:
                continue

            # Filter by age if published_parsed is available
            published_parsed = entry.get("published_parsed")
            if published_parsed:
                published_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                if published_dt < cutoff:
                    continue

            seen_google_urls.add(link)
            raw_articles.append({
                "topic": label,
                "title": html.unescape(entry.get("title", "(no title)")),
                "url": link,
                "published": entry.get("published", ""),
                "summary": html.unescape(entry.get("summary", "")),
            })

    # Pull direct RSS feeds (no redirect needed)
    for label, feed_url in DIRECT_FEEDS.items():
        log.info("Fetching: %s", label)
        feed = feedparser.parse(feed_url)
        cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)

        for entry in feed.entries[:ARTICLES_PER_FEED]:
            link = entry.get("link", "")
            if not link or link in seen_google_urls:
                continue

            published_parsed = entry.get("published_parsed")
            if published_parsed:
                published_dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
                if published_dt < cutoff:
                    continue

            seen_google_urls.add(link)
            raw_articles.append({
                "topic": label,
                "title": html.unescape(entry.get("title", "(no title)")),
                "url": link,
                "published": entry.get("published", ""),
                "summary": html.unescape(entry.get("summary", "")),
            })

    # Resolve all Google redirect URLs in parallel
    log.info("Resolving %d URLs…", len(raw_articles))
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(resolve_url, a["url"]): i for i, a in enumerate(raw_articles)}
        for future in as_completed(futures):
            idx = futures[future]
            raw_articles[idx]["url"] = future.result()

    # Deduplicate by resolved URL
    articles = []
    seen_resolved: set[str] = set()
    for a in raw_articles:
        if a["url"] not in seen_resolved:
            seen_resolved.add(a["url"])
            articles.append(a)

    log.info("Collected %d unique articles across %d topics", len(articles), len(topics))
    return articles


# ---------------------------------------------------------------------------
# Score & rank via Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are a content strategist helping Colin McAuliffe — a filmmaker and founder
who has run Zero One Digital Media for 22 years, working with clients like Adobe, Niantic,
Bessemer Venture Partners, MTV, and Comedy Central. He is based in Sausalito, CA.
His LinkedIn voice is casual, direct, and insider — no jargon, no corporate polish, no hype.
Think: someone who has seen everything in this industry and isn't easily impressed.

His audience: {AUDIENCE_CONTEXT}

Pick stories with real signal — gear shifts, business model changes, industry disruption, or local
SF/LA/NYC angles. Skip puff pieces and press releases.
Spread picks across different topics — do not over-index on AI stories."""


def build_ranking_prompt(articles: list[dict], n: int) -> str:
    lines = []
    for i, a in enumerate(articles):
        lines.append(
            f"[{i}] TOPIC: {a['topic']}\n"
            f"    TITLE: {a['title']}\n"
            f"    URL: {a['url']}\n"
            f"    PUBLISHED: {a['published']}\n"
        )

    article_block = "\n".join(lines)

    return f"""Below are {len(articles)} recent articles.
Pick the {n} most valuable for my LinkedIn audience.

ARTICLES:
{article_block}

Respond with a JSON array of exactly {n} objects. No prose, no markdown fences.
Each object:
{{
  "index": <original index>,
  "title": "<article title>",
  "url": "<article url>",
  "reason": "<one sentence: why my audience will care>",
  "angle": "<one of: agree | disagree | add insider context> — with a one-sentence prompt for my commentary>",
  "hook": "<a 1-2 sentence LinkedIn opener in Colin's voice: casual, direct, no BS — like a filmmaker with 22 years of experience reacting to the news. No buzzwords, no hype. Can end with a question but doesn't have to>"
}}"""


def rank_articles(articles: list[dict]) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic(api_key=api_key)

    n = min(TOP_N, len(articles))
    log.info("Sending %d articles to Claude, requesting top %d…", len(articles), n)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_ranking_prompt(articles, n)}],
    )

    raw = message.content[0].text.strip()
    log.info("Claude raw response (first 300 chars): %s", raw[:300])

    # Extract JSON array from anywhere in the response
    match = re.search(r'\[.*\]', raw, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON array found in Claude response: {raw[:500]}")

    picks = json.loads(match.group())
    log.info("Claude selected %d articles", len(picks))
    return picks


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

ANGLE_EMOJI = {
    "agree": "✅",
    "disagree": "🔥",
    "add insider context": "💡",
}


def build_slack_payload(picks: list[dict]) -> dict:
    now = datetime.now()
    today = now.strftime(f"%A, %B {now.day}")
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🎬 Morning Brief — {today}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "<@U067NNBTPL0> here's today's content feed",
            },
        },
        {"type": "divider"},
    ]

    for i, pick in enumerate(picks, 1):
        angle_raw = pick.get("angle", "")
        angle_key = angle_raw.split("—")[0].strip().lower()
        emoji = ANGLE_EMOJI.get(angle_key, "💬")
        hook = pick.get("hook", "")

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{i}. {pick['title']}*\n"
                        f"_{pick['reason']}_\n\n"
                        f"{emoji} *Angle:* {angle_raw}\n\n"
                        f":pencil: *Suggested hook:*\n{hook}"
                    ),
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Read article", "emoji": False},
                        "url": pick["url"],
                    }
                ],
            }
        )
        blocks.append({"type": "divider"})

    return {"blocks": blocks}


def post_to_slack(payload: dict) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        raise EnvironmentError("SLACK_WEBHOOK_URL is not set")

    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()
    log.info("Slack message sent (HTTP %d)", resp.status_code)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Morning Brief starting ===")

    articles = fetch_articles(TOPICS, ARTICLES_PER_FEED)
    if not articles:
        log.error("No articles fetched — aborting")
        sys.exit(1)

    picks = rank_articles(articles)
    payload = build_slack_payload(picks)
    post_to_slack(payload)

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
