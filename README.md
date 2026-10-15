# Winning Ad Scout

A product research agent built with the **Claude Agent SDK** and **Playwright**.

Give it a niche. The agent plans its own searches, drives a real browser through the public
Meta Ad Library, groups ads by the underlying product, and ranks the products most likely to
be profitable for a Shopify brand. Every pick links back to the ads that support it.

```
python scout.py "home fitness"
```

## How it works

```
 niche ──► Claude (Agent SDK) ──plans keywords──► search_meta_ads ──► Playwright ──► Meta Ad Library
                 ▲                                       │
                 └──────────── structured ad data ◄──────┘
                 │
                 └──► save_report ──► report.md · shortlist.csv · raw_ads.json
```

- **Custom in-process MCP tools.** `search_meta_ads` and `save_report` are Python functions
  exposed to Claude with `@tool` and `create_sdk_mcp_server`. Built-in tools (shell, file
  edits, web) are switched off, so the agent can only do what these two tools allow.
- **Agent decides the plan.** Claude chooses the keyword phrasings, re-searches when results
  are thin, and stops when it has enough evidence.
- **Evidence guardrail.** `save_report` rejects any pick that cites an ad ID the scraper never
  returned, and tells the agent to fix it. No hallucinated "winners".
- **Cost controls.** A free preflight confirms the Ad Library loads before any AI call, then each
  run has a hard USD budget (`max_budget_usd`), a turn limit, a search cap, and lean tool
  payloads. Scraped data is saved even if a run stops early, and the real cost is written into
  each report.
- **Layout-resilient scraping.** Meta's CSS classes are obfuscated and change often, so the
  scraper parses rendered text around each card's "Library ID" label instead of selectors.

### Signals the agent uses

| Signal | Why it matters |
|---|---|
| Days running | Advertisers kill losing ads fast. 30+ days usually means profitable; 90+ is a strong winner. |
| Creative variants | Many copies of one creative means the advertiser is scaling it. |
| Advertiser count | Several small stores selling the same product means proven demand. |

## Setup (Windows)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env
```

Put your Anthropic API key in `.env`. The Agent SDK bills against API credits from the
[Claude Console](https://console.anthropic.com).

## Usage

Test the scraper alone first. This is free, with no AI calls:

```
python ad_library.py "posture corrector" --max 20
```

Then run the agent. A first run on the cheaper Haiku model is a good smoke test:

```
python scout.py "home fitness" --model claude-haiku-5-5 --searches 2 --budget 0.20
python scout.py "home fitness"
python scout.py "pet grooming" --country GB --searches 3 --budget 0.50
```

Defaults: Sonnet, 3 searches, $0.50 cap per run.

Results land in `outputs/<timestamp>_<niche>/`:

- `report.md` is the ranked shortlist with reasoning, marketing angles, risks, and ad links
- `shortlist.csv` is the same picks, ready for a sheet
- `raw_ads.json` holds every ad scraped, so each claim can be audited

## Project layout

```
scout.py        Agent: system prompt, MCP tools, SDK client loop, report writer
ad_library.py   Playwright scraper + text parser (runnable on its own)
```

## Limitations and next steps

- Meta can change the Ad Library page at any time. If scraping returns nothing, run
  `ad_library.py` with `HEADLESS=0` and check what the page shows.
- The Ad Library doesn't expose spend or engagement for commercial ads, so longevity and
  scaling are proxies, not proof.
- Next steps: add TikTok Creative Center and paid spy tools (Minea, PiPiADS) as extra
  tools, check AliExpress/CJ supplier pricing for margins, and push picks to a Google
  Sheet or Slack.
