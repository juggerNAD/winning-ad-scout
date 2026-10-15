"""
scout.py — "Winning Ad Scout", a product research agent built on the Claude Agent SDK.

Give it a niche. Claude plans its own keyword searches, drives a real browser
through the Meta Ad Library (via a custom in-process MCP tool), weighs the
evidence, and saves a ranked product shortlist.

    python scout.py "home fitness"
    python scout.py "pet grooming" --country GB --searches 3
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from claude_agent_sdk import (
    AssistantMessage,
    CLINotFoundError,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

from ad_library import Ad, search_ads

load_dotenv()

OUTPUT_ROOT = Path(__file__).parent / "outputs"

# Every ad the agent has seen this run, keyed by Library ID. The report tool
# checks picks against this so the agent can't cite ads it never scraped.
SEEN_ADS: dict[str, Ad] = {}
RUN_STATE: dict[str, object] = {"country": "US", "searches_left": 4, "report_dir": None}


def _text(payload, is_error: bool = False) -> dict:
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)
    result = {"content": [{"type": "text", "text": payload}]}
    if is_error:
        result["is_error"] = True
    return result


# ---------------------------------------------------------------- tools

@tool(
    "search_meta_ads",
    "Search the public Meta (Facebook/Instagram) Ad Library for ACTIVE ads matching a "
    "keyword. Opens a real browser, scrolls the results, and returns each ad's Library ID, "
    "advertiser, start date, days running, number of ad variants using the same creative, "
    "and a snippet of the ad copy, sorted longest-running first.",
    {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Product-level search phrase, e.g. 'posture corrector'."},
            "max_ads": {"type": "integer", "description": "How many ads to collect (10-40). Default 30."},
        },
        "required": ["keyword"],
    },
)
async def search_meta_ads(args: dict) -> dict:
    if RUN_STATE["searches_left"] <= 0:
        return _text("Not run: this run's search limit is reached (a limit, not a failure). "
                     "Analyse what you have and call save_report.")
    RUN_STATE["searches_left"] -= 1

    keyword = str(args["keyword"]).strip()
    max_ads = max(10, min(int(args.get("max_ads", 30)), 40))
    try:
        ads = await search_ads(keyword, country=str(RUN_STATE["country"]), max_ads=max_ads)
    except Exception as exc:  # surface browser failures to the agent instead of crashing
        return _text(f"Browser error while searching '{keyword}': {exc}", is_error=True)

    if not ads:
        return _text(f"No active ads found for '{keyword}' (or the page did not load). Try another phrasing.")

    for ad in ads:
        SEEN_ADS[ad.library_id] = ad
    ads.sort(key=lambda a: a.days_running or 0, reverse=True)
    compact = [
        {
            "id": a.library_id,
            "advertiser": a.advertiser,
            "days_running": a.days_running,
            "variants": a.variant_count,
            "copy": a.ad_text[:160],
        }
        for a in ads
    ]
    return _text({
        "keyword": keyword,
        "ads_found": len(ads),
        "searches_left": RUN_STATE["searches_left"],
        "ads": compact,
    })


@tool(
    "save_report",
    "Save the final product shortlist. Call exactly once, after your research is done. "
    "picks_json must be a JSON array of objects with keys: rank (int), product (str), "
    "score (int 1-10), evidence_ids (array of Library IDs you actually saw), "
    "why (str, 1-3 sentences grounded in the ad data), angle (str, the marketing hook "
    "competitors use), risks (str).",
    {
        "type": "object",
        "properties": {
            "niche": {"type": "string"},
            "summary_markdown": {"type": "string", "description": "Short market overview in Markdown."},
            "picks_json": {"type": "string"},
        },
        "required": ["niche", "summary_markdown", "picks_json"],
    },
)
async def save_report(args: dict) -> dict:
    try:
        picks = json.loads(args["picks_json"])
        assert isinstance(picks, list) and picks
    except Exception:
        return _text("picks_json is not a non-empty JSON array. Fix it and call save_report again.", is_error=True)

    # Guardrail: every cited ad must be one we actually scraped.
    unknown = sorted({
        str(i) for p in picks for i in p.get("evidence_ids", []) if str(i) not in SEEN_ADS
    })
    if unknown:
        return _text(
            f"These evidence_ids were never returned by search_meta_ads: {unknown}. "
            "Only cite ads you saw. Fix the picks and call save_report again.",
            is_error=True,
        )

    picks.sort(key=lambda p: p.get("rank", 99))
    slug = re.sub(r"[^a-z0-9]+", "-", args["niche"].lower()).strip("-")[:40] or "niche"
    out_dir = OUTPUT_ROOT / f"{datetime.now():%Y%m%d-%H%M}_{slug}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # report.md
    lines = [
        f"# Product shortlist: {args['niche']}",
        f"_Meta Ad Library, country {RUN_STATE['country']}, generated {datetime.now():%Y-%m-%d %H:%M}_",
        "",
        args["summary_markdown"].strip(),
        "",
        "## Ranked picks",
        "",
    ]
    for p in picks:
        lines += [
            f"### {p.get('rank')}. {p.get('product')} (score {p.get('score')}/10)",
            f"**Why:** {p.get('why', '')}",
            "",
            f"**Angle competitors use:** {p.get('angle', '')}",
            "",
            f"**Risks:** {p.get('risks', '')}",
            "",
            "**Evidence:**",
        ]
        for ad_id in p.get("evidence_ids", []):
            ad = SEEN_ADS[str(ad_id)]
            lines.append(
                f"- [{ad.advertiser or 'Unknown advertiser'}]({ad.url}) — "
                f"{ad.days_running} days running, {ad.variant_count} variant(s)"
            )
        lines.append("")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")

    # shortlist.csv
    with open(out_dir / "shortlist.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "product", "score", "why", "angle", "risks",
                    "longest_running_days", "evidence_urls"])
        for p in picks:
            ev = [SEEN_ADS[str(i)] for i in p.get("evidence_ids", [])]
            w.writerow([
                p.get("rank"), p.get("product"), p.get("score"), p.get("why"),
                p.get("angle"), p.get("risks"),
                max((a.days_running or 0 for a in ev), default=""),
                " | ".join(a.url for a in ev),
            ])

    # raw_ads.json, so every claim can be audited
    (out_dir / "raw_ads.json").write_text(
        json.dumps([asdict(a) for a in SEEN_ADS.values()], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    RUN_STATE["report_dir"] = str(out_dir)
    return _text(f"Saved report.md, shortlist.csv and raw_ads.json to {out_dir}")


# ---------------------------------------------------------------- agent

SYSTEM_PROMPT = """You are a product research analyst for a team that runs Shopify e-commerce brands.
Your job: find physical products in a niche that look profitable to sell, using evidence from
the Meta Ad Library.

How to read the evidence:
- Days running is the strongest signal. Advertisers kill unprofitable ads fast, so an ad that
  has run 30+ days is probably making money; 90+ days is a strong winner.
- Many variants of the same creative means the advertiser is scaling it.
- Several different small advertisers selling the same product means proven demand.
- Ignore big household brands, marketplaces, apps, services, courses and non-physical offers.
  The team wants products a small Shopify brand could source and sell.

Process:
1. Turn the niche into 2-4 specific product-level search phrases (not the bare niche word).
2. Call search_meta_ads for each. Adjust phrasing if a search returns little.
3. Group ads by the underlying product, then compare products on the signals above.
4. Call save_report once with 3-7 ranked picks. Every pick must cite evidence_ids you saw.
   Be honest when evidence is thin.
5. Finish with a 3-5 line plain-text summary of the top picks.

Narrate briefly what you're doing between tool calls so a viewer can follow along."""


def _save_partial() -> None:
    """If the agent stops before save_report, still keep the scraped data you paid for."""
    if RUN_STATE["report_dir"] or not SEEN_ADS:
        return
    out_dir = OUTPUT_ROOT / f"{datetime.now():%Y%m%d-%H%M}_partial"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw_ads.json").write_text(
        json.dumps([asdict(a) for a in SEEN_ADS.values()], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    RUN_STATE["report_dir"] = str(out_dir)
    print(f"Agent didn't finish its report; scraped ads saved to {out_dir}")


async def preflight(niche: str, country: str) -> bool:
    """Free check (no AI calls) that the Ad Library loads before we spend credits."""
    print(f"Preflight: checking the Ad Library loads for '{niche}' (free, no AI)...")
    try:
        ads = await search_ads(niche, country=country, max_ads=10)
    except Exception as exc:
        print(f"Preflight failed: browser error: {exc}")
        return False
    if not ads:
        print("Preflight failed: no ads parsed. Run  python ad_library.py \"<keyword>\"  "
              "with HEADLESS=0 to see what the page shows. Nothing was spent.")
        return False
    print(f"Preflight OK: {len(ads)} ads parsed. Starting the agent.")
    return True


async def run(niche: str, country: str, searches: int, model: str, budget: float) -> None:
    RUN_STATE.update(country=country.upper(), searches_left=searches)

    server = create_sdk_mcp_server(name="adscout", version="1.0.0",
                                   tools=[search_meta_ads, save_report])
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        mcp_servers={"adscout": server},
        tools=[],  # no built-in tools (Bash, file edits, web): only our two tools
        allowed_tools=["mcp__adscout__search_meta_ads", "mcp__adscout__save_report"],
        max_turns=20,
        max_budget_usd=budget,
        setting_sources=[],  # ignore any local Claude Code settings
    )

    print(f"\n=== Winning Ad Scout | niche: {niche} | country: {country.upper()} | "
          f"model: {model} | budget: ${budget:.2f} ===\n")
    result: ResultMessage | None = None
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(
                f"Niche to research: {niche}\n"
                f"You have exactly {searches} searches this run. Run them one at a time, "
                "and use what each search shows to choose the next phrasing."
            )
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "error", None):
                        print(f"\n[api error] {msg.error}")
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            print(f"\n[agent] {block.text.strip()}")
                        elif isinstance(block, ToolUseBlock):
                            name = block.name.split("__")[-1]
                            shown = ({"niche": block.input.get("niche")} if name == "save_report"
                                     else block.input)
                            print(f"\n  -> {name}({json.dumps(shown, ensure_ascii=False)})")
                elif isinstance(msg, ResultMessage):
                    result = msg
    except CLINotFoundError:
        print("The Claude Code CLI bundled with claude-agent-sdk wasn't found. "
              "Run: pip install --upgrade claude-agent-sdk")
    except Exception as exc:
        print(f"\nAgent stopped: {exc}")
    finally:
        _save_partial()

    if result:
        cost = result.total_cost_usd or 0
        print(f"\n=== Done: {result.num_turns} turns, ${cost:.4f}, "
              f"{len(SEEN_ADS)} ads analysed ({result.subtype}) ===")
        if result.is_error:
            hint = {
                "error_max_budget_usd": "Hit the budget cap. Raise --budget or lower --searches.",
                "error_max_turns": "Hit the turn limit before finishing.",
            }.get(result.subtype, "Check the [api error] lines above (e.g. invalid API key or model name).")
            print(hint)
        if RUN_STATE["report_dir"]:
            report = Path(str(RUN_STATE["report_dir"])) / "report.md"
            if report.exists():
                with open(report, "a", encoding="utf-8") as f:
                    f.write(f"\n---\n_Run stats: {result.num_turns} agent turns, "
                            f"{len(SEEN_ADS)} ads analysed, ${cost:.4f} API cost, model {model}._\n")
    if RUN_STATE["report_dir"]:
        print(f"Output: {RUN_STATE['report_dir']}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows console safety

    ap = argparse.ArgumentParser(description="Product research agent (Claude Agent SDK + Meta Ad Library)")
    ap.add_argument("niche", help='e.g. "home fitness", "pet grooming"')
    ap.add_argument("--country", default="US", help="Two-letter Ad Library country code")
    ap.add_argument("--searches", type=int, default=3, help="Max Ad Library searches (cost/time cap)")
    ap.add_argument("--model", default=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"))
    ap.add_argument("--budget", type=float, default=float(os.getenv("MAX_BUDGET_USD", "0.50")),
                    help="Hard USD spend cap for this run")
    ap.add_argument("--no-preflight", action="store_true", help="Skip the free Ad Library check")
    args = ap.parse_args()

    if not os.getenv("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
        raise SystemExit("ANTHROPIC_API_KEY is missing or malformed. Copy .env.example to .env "
                         "and paste your key from console.anthropic.com.")

    async def _go() -> None:
        if not args.no_preflight and not await preflight(args.niche, args.country.upper()):
            return
        await run(args.niche, args.country, args.searches, args.model, args.budget)

    asyncio.run(_go())


if __name__ == "__main__":
    main()
