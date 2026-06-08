"""
News Monitoring Bot
-------------------
Scans Google News RSS for Tier 1 companies, classifies articles with Claude,
ranks the top 15 most valuable per BDR, and sends those as Slack DMs.
Each BDR has their own sent history to avoid duplicate notifications.
"""

import os
import json
import csv
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import feedparser
import anthropic
import requests
from dotenv import load_dotenv

# ── Environment ──────────────────────────────────────────────────────────────

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
GOOGLE_SHEET_CSV_URL = os.getenv("GOOGLE_SHEET_CSV_URL")

DATA_DIR = os.getenv("DATA_DIR", ".")
SEEN_ARTICLES_FILE = os.path.join(DATA_DIR, "seen_articles.json")
SENT_ARTICLES_FILE = os.path.join(DATA_DIR, "sent_articles.json")
FALLBACK_CSV = "companies.csv"
SUPERVISOR_SLACK_ID = "U0B6KQE5UMA"
MAX_ARTICLES_PER_COMPANY = 5
MAX_NOTIFICATIONS_PER_BDR = 15

# ── Deduplication — classification cache (global) ─────────────────────────────

def load_seen_articles() -> set:
    """URLs already classified — never re-process."""
    if os.path.exists(SEEN_ARTICLES_FILE):
        with open(SEEN_ARTICLES_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_articles(seen: set) -> None:
    with open(SEEN_ARTICLES_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2, ensure_ascii=False)


# ── Deduplication — sent history (per BDR) ───────────────────────────────────

def load_sent_articles() -> dict:
    """URLs already sent, keyed by slack_user_id."""
    if os.path.exists(SENT_ARTICLES_FILE):
        with open(SENT_ARTICLES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_sent_articles(sent: dict) -> None:
    with open(SENT_ARTICLES_FILE, "w", encoding="utf-8") as f:
        json.dump(sent, f, indent=2, ensure_ascii=False)


# ── Company → Salesperson mapping ─────────────────────────────────────────────

def load_companies_from_csv() -> list:
    companies = []
    with open(FALLBACK_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("tier", "Tier 1").strip() != "Tier 1":
                continue
            companies.append({
                "company": row["company"].strip(),
                "salesperson": row["salesperson"].strip(),
                "slack_user_id": row["slack_user_id"].strip(),
            })
    return companies


def load_companies_from_sheets() -> list:
    response = requests.get(GOOGLE_SHEET_CSV_URL, timeout=10)
    response.raise_for_status()

    lines = response.content.decode("utf-8").splitlines()
    reader = csv.DictReader(lines)
    reader.fieldnames = [f.strip() for f in reader.fieldnames]

    companies = []
    for row in reader:
        if not row.get("company", "").strip():
            continue
        if row.get("Tier", "").strip() != "Tier 1":
            continue
        companies.append({
            "company": row["company"].strip(),
            "salesperson": row["salesperson"].strip(),
            "slack_user_id": row["slack_user_id"].strip(),
        })
    return companies


def load_companies() -> list:
    if GOOGLE_SHEET_CSV_URL:
        try:
            print("📊 Loading Tier 1 companies from Google Sheets...")
            companies = load_companies_from_sheets()
            print(f"   ✓ {len(companies)} Tier 1 companies loaded")
            return companies
        except Exception as e:
            print(f"   ⚠ Google Sheets error: {e} — falling back to CSV")

    print(f"📄 Loading companies from {FALLBACK_CSV}...")
    companies = load_companies_from_csv()
    print(f"   ✓ {len(companies)} Tier 1 companies loaded")
    return companies


# ── Google News RSS ───────────────────────────────────────────────────────────

MAX_ARTICLE_AGE_DAYS = 7


def fetch_news(company: str) -> list:
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(company)}&hl=fr&gl=FR&ceid=FR:fr"
    )
    feed = feedparser.parse(url)
    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_ARTICLE_AGE_DAYS)
    articles = []
    for entry in feed.entries[:MAX_ARTICLES_PER_COMPANY]:
        # Filter out articles older than 7 days
        published_parsed = entry.get("published_parsed")
        if published_parsed:
            pub_date = datetime(*published_parsed[:6], tzinfo=timezone.utc)
            if pub_date < cutoff:
                continue
        articles.append({
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "published": entry.get("published", ""),
            "summary": entry.get("summary", ""),
        })
    return articles


# ── Claude classification ─────────────────────────────────────────────────────

CLASSIFICATION_PROMPT = """You are a B2B sales intelligence assistant. \
Analyze this press article about the company "{company}".

Title: {title}
Summary: {summary}
Date: {published}

Be VERY selective. The vast majority of articles should be rejected (pertinent: false).
Only flag an article if it matches exactly one of these three cases:

1. E-COMMERCE: major launch or overhaul of an e-commerce site, new online sales digital strategy, significant e-commerce partnership, acquisition of a digital platform. Exclude: passing mention of digital, general cybersecurity, internal tools.

2. TOP MANAGEMENT CHANGE: appointment or departure of a CEO, MD, CFO, COO, President. Ignore division directors or middle managers.

3. EXCEPTIONAL EVENT: only if the information is absolutely critical for the company's future (bankruptcy, transformational acquisition, major scandal). When in doubt, reject.

Everything else is not relevant: standard financial results, stock price movements, minor products, minor partnerships, sports/cultural events, etc.

Reply ONLY in valid JSON, no text before or after:
{{
  "pertinent": true or false,
  "type_evenement": "event category in English or null",
  "title_en": "English translation of the article title, or null if not relevant",
  "resume": "2 sentences maximum in English: what is happening and why it matters for a salesperson. null if not relevant"
}}"""


def classify_article(client: anthropic.Anthropic, company: str, article: dict) -> dict:
    prompt = CLASSIFICATION_PROMPT.format(
        company=company,
        title=article["title"],
        summary=article["summary"],
        published=article["published"],
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return json.loads(raw.strip())


# ── Ranking — top 15 per BDR ──────────────────────────────────────────────────

RANKING_PROMPT = """You are a sales strategy expert at Mirakl, a B2B and B2C marketplace SaaS vendor.

Here is a list of relevant press articles detected today for one BDR's portfolio. \
Select and rank the {max_count} most valuable articles \
for a BDR (Business Development Representative) at Mirakl, \
i.e. those representing the best commercial outreach opportunity.

Value criteria (descending):
1. Strong e-commerce signal (marketplace launch, site overhaul, omnichannel strategy) → direct Mirakl opportunity
2. CEO/MD change at a retailer or brand → new leadership = opening window
3. Acquisition or expansion in digital retail → budget available and growth ambition
4. Major event (restructuring, bankruptcy) → urgent transformation need

Available articles:
{articles_json}

Reply ONLY in valid JSON:
{{
  "top": [list of (0-based) indices of the {max_count} best articles, from most to least valuable]
}}"""


def rank_candidates(client: anthropic.Anthropic, candidates: list) -> list:
    """Pick and rank top MAX_NOTIFICATIONS_PER_BDR candidates for one BDR."""
    if len(candidates) <= MAX_NOTIFICATIONS_PER_BDR:
        return candidates

    articles_summary = [
        {
            "index": i,
            "company": c["company"],
            "type_evenement": c["classification"]["type_evenement"],
            "title": c["article"]["title"],
            "resume": c["classification"]["resume"],
        }
        for i, c in enumerate(candidates)
    ]

    prompt = RANKING_PROMPT.format(
        max_count=MAX_NOTIFICATIONS_PER_BDR,
        articles_json=json.dumps(articles_summary, ensure_ascii=False, indent=2),
    )

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw.strip())
    return [candidates[i] for i in result["top"][:MAX_NOTIFICATIONS_PER_BDR]]


# ── Slack notification ────────────────────────────────────────────────────────

def _post_slack_message(channel: str, text: str) -> None:
    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"channel": channel, "text": text},
        timeout=10,
    )
    result_json = response.json()
    if result_json.get("ok"):
        print(f"   ✓ DM sent to {channel}")
    else:
        print(f"   ⚠ Slack error ({channel}): {result_json.get('error')}")


def send_slack_dm(slack_user_id: str, company: str, article: dict, result: dict) -> None:
    title = result.get("title_en") or article["title"]
    text = (
        f"🏢 *{company}* — {result['type_evenement']}\n"
        f"📰 {title}\n"
        f"💡 {result['resume']}\n"
        f"🔗 <{article['url']}|Read article>"
    )

    _post_slack_message(slack_user_id, text)

    if slack_user_id != SUPERVISOR_SLACK_ID:
        _post_slack_message(SUPERVISOR_SLACK_ID, text)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  News Monitoring Bot — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    seen_articles = load_seen_articles()
    sent_articles = load_sent_articles()
    companies = load_companies()
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Phase 1 : scan and classify all articles ──────────────────────────────
    print("⏳ Phase 1 — Scanning articles...\n")
    candidates = []

    for entry in companies:
        company = entry["company"]
        salesperson = entry["salesperson"]
        slack_user_id = entry["slack_user_id"]

        print(f"🔍 {company}  →  {salesperson}")

        articles = fetch_news(company)
        if not articles:
            print("   No articles found.")
            continue

        for article in articles:
            url = article["url"]

            if url in seen_articles:
                print(f"   ⏭ Already seen: {article['title'][:55]}…")
                continue

            print(f"   📄 {article['title'][:55]}…")

            try:
                classification = classify_article(claude, company, article)
            except Exception as e:
                print(f"   ⚠ Classification error: {e}")
                seen_articles.add(url)
                continue

            seen_articles.add(url)

            if classification.get("pertinent"):
                print(f"   ✅ {classification['type_evenement']}")
                candidates.append({
                    "company": company,
                    "salesperson": salesperson,
                    "slack_user_id": slack_user_id,
                    "article": article,
                    "classification": classification,
                })
            else:
                print("   ✗ Not relevant")

            time.sleep(0.3)

    print(f"\n📋 {len(candidates)} relevant article(s) found.")

    # ── Phase 2 : group by BDR, filter already sent, rank top 15 each ─────────
    by_bdr = defaultdict(list)
    for c in candidates:
        by_bdr[c["slack_user_id"]].append(c)

    total_sent = 0

    for slack_user_id, bdr_candidates in by_bdr.items():
        already_sent = set(sent_articles.get(slack_user_id, []))
        fresh = [c for c in bdr_candidates if c["article"]["url"] not in already_sent]

        if not fresh:
            print(f"\n👤 {slack_user_id} — nothing new to send.")
            continue

        print(f"\n⚡ Phase 2 — Ranking top {MAX_NOTIFICATIONS_PER_BDR} for {slack_user_id} ({len(fresh)} candidates)...")
        top = rank_candidates(claude, fresh)

        print(f"📨 Sending {len(top)} notification(s) to {slack_user_id}...\n")
        for item in top:
            send_slack_dm(
                slack_user_id,
                item["company"],
                item["article"],
                item["classification"],
            )
            # Record as sent for this BDR
            sent_articles.setdefault(slack_user_id, [])
            sent_articles[slack_user_id].append(item["article"]["url"])
            total_sent += 1

    save_seen_articles(seen_articles)
    save_sent_articles(sent_articles)

    print(f"\n{'='*60}")
    print(f"  Done — {total_sent} notification(s) sent across all BDRs.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
