# News Monitoring Bot

Scans Google News RSS for tracked companies, classifies articles with Claude AI,
and sends Slack DMs to the right salesperson when a relevant commercial event is detected.

**Detected event types:** fundraising · new CEO/CFO · M&A · restructuring · new market · strategic partnership

---

## Quick start

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `ANTHROPIC_API_KEY` | https://console.anthropic.com → API Keys |
| `SLACK_BOT_TOKEN` | Slack app dashboard → OAuth & Permissions (needs `chat:write` scope) |
| `GOOGLE_SHEETS_ID` | *(optional)* URL of your sheet: `docs.google.com/spreadsheets/d/**<ID>**/edit` |
| `GOOGLE_CREDENTIALS_FILE` | *(optional)* Path to your Google service account JSON |

### 3. Configure your companies

**Option A — CSV (default, no setup needed)**

Edit `companies.csv`:

```
company,salesperson,slack_user_id
TotalEnergies,Marc Dupont,U0123456
Airbus,Marc Dupont,U0123456
...
```

To find a Slack user's ID: open their profile → ⋮ → *Copy member ID*.

**Option B — Google Sheets**

1. Create a service account in [Google Cloud Console](https://console.cloud.google.com)
2. Enable the **Google Sheets API** for your project
3. Download the JSON key → save as `google_credentials.json`
4. Share your Google Sheet with the service account email
5. Set `GOOGLE_SHEETS_ID` in `.env`

Your sheet must have this structure (row 1 = headers):

| A: company | B: salesperson | C: slack_user_id |
|---|---|---|
| TotalEnergies | Marc Dupont | U0123456 |

### 4. Run the bot

```bash
python main.py
```

---

## Run on a schedule (cron)

Add to your crontab (`crontab -e`) to run every morning at 8 AM:

```
0 8 * * 1-5 /path/to/.venv/bin/python /path/to/company_news/main.py >> /path/to/company_news/bot.log 2>&1
```

---

## How it works

```
companies.csv / Google Sheets
        │
        ▼
For each company
        │
        ▼
Google News RSS (last 5 articles)
        │
        ▼
Deduplication check (seen_articles.json)
        │
        ▼
Claude API — relevance classification
        │ pertinent: true
        ▼
Slack DM → salesperson responsible for that company
```

### Claude response format

```json
{
  "pertinent": true,
  "type_evenement": "Levée de fonds",
  "resume": "Airbus lève 500M€ pour financer son programme hydrogène.",
  "urgence": "haute"
}
```

### Slack message format

```
🏢 *Airbus* — Levée de fonds
📰 Airbus raises €500M for hydrogen programme
💡 Airbus lève 500M€ pour financer son programme hydrogène.
🔗 https://...
🔴 Urgence : *haute*
```

---

## File structure

```
company_news/
├── main.py              # Main bot logic
├── companies.csv        # Fallback company → salesperson mapping
├── seen_articles.json   # Deduplication store (auto-managed)
├── requirements.txt
├── .env.example
└── README.md
```

---

## Slack setup note

The bot uses `chat.postMessage` with the user's Slack ID as the channel,
which opens a direct message. Your Slack app needs the **`chat:write`** OAuth scope.
Install the app to your workspace and copy the **Bot Token** (`xoxb-…`).
