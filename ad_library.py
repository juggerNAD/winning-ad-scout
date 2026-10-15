"""
ad_library.py — Playwright scraper for the public Meta Ad Library.

Meta's markup uses obfuscated, frequently-changing CSS classes, so instead of
CSS selectors this module reads the rendered page text and splits it on the
"Library ID:" label that every ad card shows. That is far more stable across
layout changes.

Run it on its own to test scraping without spending any API credits:
    python ad_library.py "posture corrector" --country US --max 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from urllib.parse import quote_plus

from playwright.async_api import async_playwright

LIBRARY_URL = (
    "https://www.facebook.com/ads/library/"
    "?active_status=active&ad_type=all&country={country}"
    "&q={query}&search_type=keyword_unordered&media_type=all"
)
AD_URL = "https://www.facebook.com/ads/library/?id={library_id}"

ID_SPLIT = re.compile(r"Library ID:\s*(\d+)")
STARTED = re.compile(
    r"Started running on\s+("
    r"[A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4}"   # Sep 15, 2026
    r"|\d{1,2}\s+[A-Z][a-z]{2,8}\.?\s+\d{4}"     # 15 Sep 2026
    r")"
)
VARIANTS = re.compile(r"(\d[\d,]*)\s+ads?\s+use(?:s)?\s+this\s+creative", re.I)

# Card chrome we don't want in the ad copy.
NOISE = {
    "Active", "Inactive", "Platforms", "See ad details", "See summary details",
    "This ad has multiple versions", "Sponsored", "Shop now", "Shop Now",
    "Learn more", "Learn More", "Order now", "Order Now", "Buy now", "Buy Now",
    "Sign up", "Sign Up", "Open dropdown", "Categories",
}


@dataclass
class Ad:
    library_id: str
    advertiser: str
    started: str | None
    days_running: int | None
    variant_count: int
    ad_text: str
    url: str
    keyword: str


def _parse_date(raw: str) -> date | None:
    raw = raw.replace(".", "").replace("Sept ", "Sep ")
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_page_text(text: str, keyword: str, today: date | None = None) -> list[Ad]:
    """Turn the Ad Library's rendered text into structured ads."""
    today = today or date.today()
    parts = ID_SPLIT.split(text)
    # parts = [preamble, id1, chunk1, id2, chunk2, ...]
    ads: list[Ad] = []
    seen: set[str] = set()
    for i in range(1, len(parts) - 1, 2):
        library_id, chunk = parts[i], parts[i + 1]
        if library_id in seen:
            continue
        seen.add(library_id)

        started_raw = None
        days = None
        m = STARTED.search(chunk)
        if m:
            started_raw = m.group(1)
            d = _parse_date(started_raw)
            if d:
                days = (today - d).days

        vm = VARIANTS.search(chunk)
        variants = int(vm.group(1).replace(",", "")) if vm else 1

        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        advertiser = ""
        body_lines: list[str] = []
        if "Sponsored" in lines:
            s = lines.index("Sponsored")
            advertiser = lines[s - 1] if s > 0 else ""
            for ln in lines[s + 1:]:
                # The next card's status label marks the end of this card.
                if ln in ("Active", "Inactive"):
                    break
                if ln in NOISE or ln.startswith("Started running"):
                    continue
                body_lines.append(ln)
        ad_text = re.sub(r"\d{1,2}:\d{2}\s*/\s*\d{1,2}:\d{2}\s*", "", " ".join(body_lines)).strip()[:400]

        ads.append(Ad(
            library_id=library_id,
            advertiser=advertiser,
            started=started_raw,
            days_running=days,
            variant_count=variants,
            ad_text=ad_text,
            url=AD_URL.format(library_id=library_id),
            keyword=keyword,
        ))
    return ads


async def _dismiss_cookie_banner(page) -> None:
    # Only shown in some regions. Declines optional cookies if offered.
    for label in ("Decline optional cookies", "Only allow essential cookies"):
        btn = page.get_by_role("button", name=label)
        try:
            if await btn.count():
                await btn.first.click(timeout=2000)
                return
        except Exception:
            pass


async def _close_popups(page) -> None:
    # Logged-out visitors sometimes get a "log in to see more" dialog while scrolling.
    try:
        dialog = page.get_by_role("dialog")
        if await dialog.count():
            close = page.get_by_role("button", name=re.compile(r"^Close$", re.I))
            if await close.count():
                await close.first.click(timeout=1500)
            else:
                await page.keyboard.press("Escape")
    except Exception:
        pass


async def search_ads(
    keyword: str,
    country: str = "US",
    max_ads: int = 30,
    headless: bool | None = None,
) -> list[Ad]:
    """Open the Ad Library, scroll until we have enough cards, and parse them."""
    if headless is None:
        headless = os.getenv("HEADLESS", "0") == "1"

    url = LIBRARY_URL.format(country=country.upper(), query=quote_plus(keyword))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await _dismiss_cookie_banner(page)
            try:
                await page.wait_for_selector("text=Library ID", timeout=30000)
            except Exception:
                return []  # No results, or the page changed / blocked us.

            ads: list[Ad] = []
            stale_rounds = 0
            while len(ads) < max_ads and stale_rounds < 4:
                await _close_popups(page)
                await page.mouse.wheel(0, random.randint(2500, 4000))
                await page.wait_for_timeout(random.randint(1200, 2200))
                text = await page.evaluate("document.body.innerText")
                new_ads = parse_page_text(text, keyword)
                stale_rounds = stale_rounds + 1 if len(new_ads) <= len(ads) else 0
                ads = new_ads
            return ads[:max_ads]
        finally:
            await browser.close()


if __name__ == "__main__":
    # Windows consoles default to cp1252 and crash on emoji in ad copy.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Scrape the Meta Ad Library (no AI, no cost).")
    ap.add_argument("keyword")
    ap.add_argument("--country", default="US")
    ap.add_argument("--max", type=int, default=30)
    args = ap.parse_args()

    results = asyncio.run(search_ads(args.keyword, args.country, args.max))
    results.sort(key=lambda a: a.days_running or 0, reverse=True)
    print(json.dumps([asdict(a) for a in results], indent=2, ensure_ascii=False))
    print(f"\n{len(results)} ads scraped for '{args.keyword}'")
