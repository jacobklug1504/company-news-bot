"""
News Monitoring Bot
-------------------
Scans Google News RSS for Tier 1 companies, classifies articles with Claude,
ranks the top 15 most valuable for a Mirakl BDR, and sends those as Slack DMs.
"""

import os
import json
import csv
import time
import feedparser
import anthropic
import requests
from dotenv import load_dotenv
from datetime import datetime

# ── Environment ──────────────────────────────────────────────────────────────

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
GOOGLE_SHEET_CSV_URL = os.getenv("GOOGLE_SHEET_CSV_URL")

DATA_DIR = os.getenv("DATA_DIR", ".")
SEEN_ARTICLES_FILE = os.path.join(DATA_DIR, "seen_articles.json")
FALLBACK_CSV = "companies.csv"
MAX_ARTICLES_PER_COMPANY = 5
MAX_DAILY_NOTIFICATIONS = 15

# ── Deduplication ─────────────────────────────────────────────────────────────

def load_seen_articles() -> set:
    if os.path.exists(SEEN_ARTICLES_FILE):
        with open(SEEN_ARTICLES_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_articles(seen: set) -> None:
    with open(SEEN_ARTICLES_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2, ensure_ascii=False)


# ── Company → Salesperson mapping ─────────────────────────────────────────────

def load_companies_from_csv() -> list:
    """Fallback CSV — include tier column if present, default to Tier 1."""
    companies = []
    with open(FALLBACK_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tier = row.get("tier", "Tier 1").strip()
            if tier != "Tier 1":
                continue
            companies.append({
                "company": row["company"].strip(),
                "salesperson": row["salesperson"].strip(),
                "slack_user_id": row["slack_user_id"].strip(),
            })
    return companies


def load_companies_from_sheets() -> list:
    """
    Fetch the Google Sheet as CSV.
    Reads column D (Tier) and keeps only Tier 1 companies.
    """
    response = requests.get(GOOGLE_SHEET_CSV_URL, timeout=10)
    response.raise_for_status()

    lines = response.content.decode("utf-8").splitlines()
    reader = csv.DictReader(lines)
    reader.fieldnames = [f.strip() for f in reader.fieldnames]

    companies = []
    for row in reader:
        if not row.get("company", "").strip():
            continue
        tier = row.get("Tier", "").strip()
        if tier != "Tier 1":
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

def fetch_news(company: str) -> list:
    url = (
        f"https://news.google.com/rss/search"
        f"?q={requests.utils.quote(company)}&hl=fr&gl=FR&ceid=FR:fr"
    )
    feed = feedparser.parse(url)
    articles = []
    for entry in feed.entries[:MAX_ARTICLES_PER_COMPANY]:
        articles.append({
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "published": entry.get("published", ""),
            "summary": entry.get("summary", ""),
        })
    return articles


# ── Claude classification ─────────────────────────────────────────────────────

CLASSIFICATION_PROMPT = """Tu es un assistant d'intelligence commerciale B2B. \
Analyse cet article de presse concernant l'entreprise "{company}".

Titre : {title}
Résumé : {summary}
Date : {published}

Sois TRÈS sélectif. La grande majorité des articles doivent être rejetés (pertinent: false).
N'envoie une alerte QUE si l'article correspond exactement à l'un de ces trois cas :

1. E-COMMERCE : lancement ou refonte majeure d'un site e-commerce, nouvelle stratégie digitale vente en ligne, partenariat e-commerce significatif, acquisition d'une plateforme digitale. Exclure : simple mention du digital, cybersécurité générale, outils internes.

2. CHANGEMENT TOP MANAGEMENT : nomination ou départ d'un CEO, DG, CFO, COO, Président. Ignorer les directeurs de division ou managers intermédiaires.

3. ÉVÉNEMENT EXCEPTIONNEL : uniquement si l'information est absolument majeure pour l'avenir de l'entreprise (faillite, acquisition transformationnelle, scandale critique). En cas de doute, rejeter.

Tout le reste est non pertinent : résultats financiers classiques, variations boursières, produits lambda, partenariats mineurs, événements sportifs/culturels, etc.

Réponds UNIQUEMENT en JSON valide, sans texte avant ni après :
{{
  "pertinent": true ou false,
  "type_evenement": "catégorie de l'événement ou null",
  "resume": "2 phrases maximum : ce qui se passe et pourquoi c'est important pour un commercial. null si non pertinent"
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


# ── Ranking — top 15 for a Mirakl BDR ────────────────────────────────────────

RANKING_PROMPT = """Tu es un expert en stratégie commerciale chez Mirakl, éditeur SaaS de marketplace B2B et B2C.

Voici une liste d'articles de presse pertinents détectés aujourd'hui. \
Tu dois sélectionner et classer les {max_count} articles les plus précieux \
pour un BDR (Business Development Representative) chez Mirakl, \
c'est-à-dire ceux qui représentent la meilleure opportunité de prise de contact commerciale.

Critères de valeur décroissante :
1. Signal e-commerce fort (lancement marketplace, refonte site, stratégie omnicanal) → opportunité directe Mirakl
2. Changement de DG/CEO dans un retailer ou une marque → nouvelle direction = fenêtre d'ouverture
3. Acquisition ou expansion dans le retail digital → budget disponible et ambition de croissance
4. Événement majeur (restructuration, faillite) → besoin urgent de transformation

Articles disponibles :
{articles_json}

Réponds UNIQUEMENT en JSON valide :
{{
  "top": [liste des indices (0-based) des {max_count} meilleurs articles, du plus au moins précieux]
}}"""


def rank_candidates(client: anthropic.Anthropic, candidates: list) -> list:
    """Ask Claude to pick and rank the top MAX_DAILY_NOTIFICATIONS candidates for a Mirakl BDR."""
    if len(candidates) <= MAX_DAILY_NOTIFICATIONS:
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
        max_count=MAX_DAILY_NOTIFICATIONS,
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
    top_indices = result["top"][:MAX_DAILY_NOTIFICATIONS]
    return [candidates[i] for i in top_indices]


# ── Slack notification ────────────────────────────────────────────────────────

def send_slack_dm(slack_user_id: str, company: str, article: dict, result: dict) -> None:
    text = (
        f"🏢 *{company}* — {result['type_evenement']}\n"
        f"📰 {article['title']}\n"
        f"💡 {result['resume']}\n"
        f"🔗 <{article['url']}|Voir l'article>"
    )

    response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={
            "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"channel": slack_user_id, "text": text},
        timeout=10,
    )

    result_json = response.json()
    if result_json.get("ok"):
        print(f"   ✓ DM sent to {slack_user_id}")
    else:
        print(f"   ⚠ Slack error ({slack_user_id}): {result_json.get('error')}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"  News Monitoring Bot — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    seen_articles = load_seen_articles()
    companies = load_companies()
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # ── Phase 1 : collect all relevant articles ───────────────────────────────
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

    print(f"\n📋 {len(candidates)} relevant article(s) found across all Tier 1 companies.")

    # ── Phase 2 : rank top 15 for Mirakl BDR ─────────────────────────────────
    if not candidates:
        print("   Nothing to send today.")
    else:
        if len(candidates) > MAX_DAILY_NOTIFICATIONS:
            print(f"⚡ Phase 2 — Ranking top {MAX_DAILY_NOTIFICATIONS} for Mirakl BDR...")
            top = rank_candidates(claude, candidates)
        else:
            top = candidates

        # ── Phase 3 : send notifications ──────────────────────────────────────
        print(f"\n📨 Phase 3 — Sending {len(top)} notification(s)...\n")
        for item in top:
            send_slack_dm(
                item["slack_user_id"],
                item["company"],
                item["article"],
                item["classification"],
            )

    save_seen_articles(seen_articles)

    print(f"\n{'='*60}")
    print(f"  Done — {min(len(candidates), MAX_DAILY_NOTIFICATIONS)} alert(s) sent.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
