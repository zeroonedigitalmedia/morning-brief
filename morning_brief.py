#!/usr/bin/env python3
"""
Morning Brief — daily RSS digest for LinkedIn content ideas.
Fetches Google News articles, scores them via Claude, posts top 3 to Slack.

Cron: 0 8 * * * /usr/bin/env python3 /path/to/morning_brief.py
"""

import os
import sys
import json
import html
import logging
from datetime import datetime
from urllib.parse import quote_plus

import feedparser
import anthropic
import requests

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
}

ARTICLES_PER_FEED = 10   # how many articles to pull per topic
TOP_N = 3                # how many to surface to Slack

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


def fetch_articles(topics: dict[str, str], per_feed: int) -> list[dict]:
    articles = []
    seen_urls: set[str] = set()

    for label, query in topics.items():
        url = google_news_url(query)
        log.info("Fetching: %s", label)
        feed = feedparser.parse(url)

        if feed.bozo:
            log.warning("Feed parse warning for '%s': %s", label, feed.bozo_exception)

        for entry in feed.entries[:per_feed]:
            link = entry.get("link", "")
            if link in seen_urls:
                continue
            seen_urls.add(link)

            articles.append({
                "topic": label,
                "title": html.unescape(entry.get("title", "(no title)")),
                "url": link,
                "published": entry.get("published", ""),
                "summary": html.unescape(entry.get("summary", "")),
            })

    log.info("Collected %d unique articles across %d topics", len(articles), len(topics))
    return articles


# ---------------------------------------------------------------------------
# Score & rank via Claude
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are a content strategist who curates industry news for LinkedIn.
Your audience: {AUDIENCE_CONTEXT}
You pick stories that will spark engagement — news they haven't seen yet,
trend signals, gear/tech reveals, business shifts, or local SF/LA/NYC angles."""


def build_ranking_prompt(articles: list[dict]) -> str:
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
Pick the {TOP_N} most valuable for my LinkedIn audience.

ARTICLES:
{article_block}

Respond with a JSON array of exactly {TOP_N} objects. No prose, no markdown fences.
Each object:
{{
  "index": <original index>,
  "title": "<article title>",
  "url": "<article url>",
  "reason": "<one sentence: why my audience will care>",
  "angle": "<one of: agree | disagree | add insider context> — with a one-sentence prompt for my commentary>"
}}"""


def rank_articles(articles: list[dict]) -> list[dict]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY is not set")

    client = anthropic.Anthropic(api_key=api_key)

    log.info("Sending %d articles to Claude for ranking…", len(articles))
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_ranking_prompt(articles)}],
    )

    raw = message.content[0].text.strip()

    # Strip accidental markdown fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    picks = json.loads(raw)
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
    today = datetime.now().strftime("%A, %B %-d")
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🎬 Morning Brief — {today}",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    for i, pick in enumerate(picks, 1):
        angle_raw = pick.get("angle", "")
        # Angle field is "agree — commentary prompt" or similar
        angle_key = angle_raw.split("—")[0].strip().lower()
        emoji = ANGLE_EMOJI.get(angle_key, "💬")

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{i}. <{pick['url']}|{pick['title']}>*\n"
                        f"_{pick['reason']}_\n"
                        f"{emoji} *{angle_raw}*"
                    ),
                },
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
