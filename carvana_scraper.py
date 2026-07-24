#!/usr/bin/env python3
"""
Carvana inventory scraper.

  1. Query page 1 to learn `totalMatchedInventory` and `totalMatchedPages`.
  2. Walk pages sequentially, deduping vehicles by VIN as we go.
  3. If we detect the API silently capping depth (repeated VINs, short/empty
     pages before we expect the end, or totalMatchedPages that doesn't match
     what we can actually retrieve), log a warning — this is the signal you'd
     need to fall back to filter-based partitioning instead of raw pagination.
  4. Retries with backoff on failure, rate-limits itself between requests.

Posture: this is a polite client. It paces itself, honors Retry-After, and
STOPS when the server pushes back with a rate-limit ceiling or a bot challenge
rather than trying to grind through it. Check Carvana's robots.txt and Terms of
Service before running against the live site, and keep request volume modest.

Usage:
    python carvana_scraper.py --zip5 63118 --out-dir ./output
"""

import argparse
import json
import logging
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

JSONDict = dict[str, Any]

API_URL = "https://apik.carvana.io/merch/search/api/v2/search"
PAGE_SIZE = 24
REQUEST_TIMEOUT = 20
MAX_RETRIES = 5
# How many times we'll honor a rate-limit backoff before giving up on a page.
# Separate budget from transient-error retries so sustained throttling produces
# a clear "we're being rate-limited" error instead of a generic failure.
MAX_RATE_LIMIT_HITS = 4
BASE_DELAY = 1.5


class ChallengedError(RuntimeError):
    """The endpoint served a bot-challenge / block instead of data.

    When a site puts up a human-verification wall, the right move is to stop,
    not to grind retries against it — retrying a challenge is exactly the
    abusive pattern to avoid. We surface this so the caller halts cleanly.
    """


class RateLimitedError(RuntimeError):
    """We stayed rate-limited after honoring several backoffs. Stop, don't hammer."""


class DepthLimitError(RuntimeError):
    """The API rejected the request with a 400. Deep in pagination this is the
    result-window ceiling (you can't page past ~10k results) — retrying the same
    request won't help, so we stop and keep what we've collected. Near the start
    of a scrape a 400 means a malformed request instead, which surfaces loudly.
    """


def _backoff(attempt: int) -> float:
    return BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)

# Matches the analyticsData.browser: "Chrome" claim in the payload — without
# this the request body says "Chrome" while the actual HTTP headers say
# python-requests, which is a much easier bot signal than anything in the body.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.carvana.com",
    "Referer": "https://www.carvana.com/cars",
    "sec-ch-ua": '"Chromium";v="126", "Google Chrome";v="126", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    # www.carvana.com -> apik.carvana.io are different registrable domains
    # (.com vs .io), which Chrome classifies as cross-site, not same-site.
    "Sec-Fetch-Site": "cross-site",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("carvana_scraper")


def build_payload(
    zip5: str,
    page: int,
    filters: JSONDict | None = None,
    browser_cookie_id: str = "",
    search_session_id: str = "",
) -> JSONDict:
    # The API 400s on empty browserCookieId/searchSessionId — it wants real-looking
    # GUIDs, same as the browser sends. Held stable across a scrape's pages, same
    # as a real browsing session would.
    return {
        "analyticsData": {
            "browser": "Chrome",
            "clientId": "srp_ui",
            "deviceName": "",
            "isBot": False,
            "isFirstActiveSearchSession": page == 1,
            "isMobileDevice": False,
            "previousSearchRequestId": "",
            "referrer": "https://www.carvana.com/cars",
            "searchSessionId": search_session_id or str(uuid.uuid4()),
            "utmParams": {},
        },
        "browserCookieId": browser_cookie_id or str(uuid.uuid4()),
        "dealershipId": None,
        "filters": filters or {},
        "pagination": {"page": page, "pageSize": PAGE_SIZE},
        "preferredAcquisitionName": "",
        "requestedFeatures": [
            "EarliestAcquisitionBoosting",
            "ExcludeFacetData",
            "HideImpossibleCombos",
            "LoanTermPricing",
            "LocationBasedPrefiltering",
            "Personalization",
            "ConditionalTiles",
            "Sprinkles",
            "ApplyTradeIn",
        ],
        "sortBy": "MostPopular",
        "zip5": zip5,
    }


def fetch_page(
    session: requests.Session,
    zip5: str,
    page: int,
    filters: JSONDict | None = None,
    browser_cookie_id: str = "",
    search_session_id: str = "",
) -> JSONDict:
    payload = build_payload(zip5, page, filters, browser_cookie_id, search_session_id)
    last_exc = None
    rate_limit_hits = 0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(API_URL, json=payload, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            wait = _backoff(attempt)
            log.warning("Error on page %s (attempt %s): %s — retrying in %.1fs", page, attempt, exc, wait)
            time.sleep(wait)
            continue

        # Rate limiting: honor the server's Retry-After and back off on its own
        # budget. If it stays throttled, stop — we're being told we're too fast.
        if resp.status_code == 429:
            rate_limit_hits += 1
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else _backoff(attempt)
            log.warning("429 on page %s — honoring backoff %.1fs (rate-limit hit %s/%s)",
                        page, wait, rate_limit_hits, MAX_RATE_LIMIT_HITS)
            if rate_limit_hits >= MAX_RATE_LIMIT_HITS:
                raise RateLimitedError(
                    f"Still rate-limited on page {page} after {rate_limit_hits} backoffs — "
                    "stopping instead of hammering."
                )
            time.sleep(wait)
            continue

        # Depth ceiling: a 400 won't be fixed by retrying the identical request.
        # Deep in pagination it's the result-window cap; stop and keep what we have.
        if resp.status_code == 400:
            raise DepthLimitError(
                f"Page {page} got HTTP 400 — the API rejected the request. Deep in "
                "pagination this is the result-window ceiling; near the start it's a malformed request."
            )

        # Challenge / block: a verification wall or non-JSON interstitial is not
        # something to retry. Distinguish it from throttling so it's visible.
        content_type = resp.headers.get("Content-Type", "").lower()
        if resp.status_code in (401, 403) or "json" not in content_type:
            raise ChallengedError(
                f"Page {page} returned status {resp.status_code} with Content-Type "
                f"'{content_type or 'none'}' — looks like a bot challenge or block, not data."
            )

        try:
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            wait = _backoff(attempt)
            log.warning("HTTP %s on page %s (attempt %s) — retrying in %.1fs",
                        resp.status_code, page, attempt, wait)
            time.sleep(wait)
            continue

        try:
            return resp.json()
        except ValueError as exc:
            # 200 + JSON content-type but an unparseable body is itself a red flag.
            raise ChallengedError(
                f"Page {page} returned a {resp.status_code} that didn't parse as JSON — "
                "likely an interstitial rather than real data."
            ) from exc

    raise RuntimeError(f"Failed to fetch page {page} after {MAX_RETRIES} attempts") from last_exc


def scrape(zip5: str, filters: JSONDict | None = None, max_pages: int | None = None) -> tuple[dict[str, JSONDict], JSONDict]:
    """
    Returns (vehicles_by_vin, meta) where meta has counts/diagnostics.
    """
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)
    vehicles_by_vin: dict[str, JSONDict] = {}
    browser_cookie_id = str(uuid.uuid4())
    search_session_id = str(uuid.uuid4())

    try:
        first = fetch_page(session, zip5, page=1, filters=filters,
                           browser_cookie_id=browser_cookie_id, search_session_id=search_session_id)
    except (ChallengedError, RateLimitedError) as exc:
        log.error("Stopped before collecting anything: %s", exc)
        meta = {
            "zip5": zip5, "filters": filters or {},
            "total_reported": None, "total_pages_reported": None,
            "pages_walked": 0, "unique_vehicles_collected": 0,
            "stopped_early": True, "stop_reason": type(exc).__name__,
        }
        return {}, meta
    inv = first["data"]["inventory"] if "data" in first else first["inventory"]
    pagination = inv["pagination"]
    total_reported = pagination["totalMatchedInventory"]
    total_pages_reported = pagination["totalMatchedPages"]

    log.info(
        "zip=%s filters=%s -> totalMatchedInventory=%s totalMatchedPages=%s",
        zip5, filters, total_reported, total_pages_reported,
    )

    pages_to_walk = total_pages_reported if max_pages is None else min(total_pages_reported, max_pages)

    stalled_pages = 0
    empty_pages = 0
    stop_reason = None
    page = 0

    for page in range(1, pages_to_walk + 1):
        if page == 1:
            data = first
        else:
            time.sleep(BASE_DELAY + random.uniform(0, 0.75))
            try:
                data = fetch_page(session, zip5, page=page, filters=filters,
                                  browser_cookie_id=browser_cookie_id, search_session_id=search_session_id)
            except (ChallengedError, RateLimitedError, DepthLimitError) as exc:
                log.warning("Stopping at page %s: %s", page, exc)
                stop_reason = type(exc).__name__
                page -= 1  # this page didn't yield data
                break

        inv = data["data"]["inventory"] if "data" in data else data["inventory"]
        vehicles = inv.get("vehicles", [])

        if not vehicles:
            empty_pages += 1
            log.warning("Page %s returned 0 vehicles (expected up to %s pages)", page, pages_to_walk)
            if empty_pages >= 3:
                log.warning("3 consecutive/near empty pages — likely hit a real pagination ceiling. Stopping early.")
                break
            continue

        new_this_page = 0
        for v in vehicles:
            vin = v.get("vin") or v.get("id")
            if vin is None:
                log.warning("Vehicle on page %s missing VIN/id field, skipping dedupe key", page)
                continue
            if vin not in vehicles_by_vin:
                vehicles_by_vin[vin] = v
                new_this_page += 1

        if new_this_page == 0:
            stalled_pages += 1
            log.warning(
                "Page %s returned %s vehicles but 0 new VINs — repeating data, likely at the API's depth cap.",
                page, len(vehicles),
            )
            if stalled_pages >= 3:
                log.warning("3 consecutive stalled pages (no new VINs). Stopping — pagination has capped out.")
                break
        else:
            stalled_pages = 0

        if page % 50 == 0:
            log.info("Progress: page %s/%s, %s unique vehicles so far", page, pages_to_walk, len(vehicles_by_vin))

    meta: JSONDict = {
        "zip5": zip5,
        "filters": filters or {},
        "total_reported": total_reported,
        "total_pages_reported": total_pages_reported,
        "pages_walked": page,
        "unique_vehicles_collected": len(vehicles_by_vin),
        "stopped_early": stalled_pages >= 3 or empty_pages >= 3 or stop_reason is not None,
        "stop_reason": stop_reason,
    }
    return vehicles_by_vin, meta


def main():
    parser = argparse.ArgumentParser(description="Scrape Carvana inventory for a given zip code.")
    parser.add_argument("--zip5", required=True, help="5-digit delivery zip code, e.g. 63118")
    parser.add_argument("--out-dir", default="./output", help="Directory to write results to")
    parser.add_argument("--max-pages", type=int, default=None, help="Cap on pages to walk (for testing the depth ceiling)")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    vehicles_by_vin, meta = scrape(args.zip5, filters={}, max_pages=args.max_pages)

    log.info("Done. %s", json.dumps(meta, indent=2))

    if meta.get("stop_reason") == "DepthLimitError":
        log.warning(
            "Hit the API's pagination depth ceiling (HTTP 400) around page %s with %s vehicles. "
            "Raw pagination can't go deeper — filter-based partitioning (price/make/year buckets) "
            "is needed to reach the rest.",
            meta["pages_walked"], meta["unique_vehicles_collected"],
        )
    elif meta.get("stop_reason") in ("ChallengedError", "RateLimitedError"):
        log.warning(
            "Stopped because the server pushed back (%s). This is a signal to slow down or "
            "back off entirely, not to retry harder.", meta["stop_reason"],
        )

    if meta["stopped_early"] and meta["total_reported"] \
            and meta["unique_vehicles_collected"] < meta["total_reported"] * 0.95:
        log.warning(
            "Collected %s of %s reported vehicles (%.1f%%). Raw pagination likely capped out — "
            "you'll need filter-based partitioning (make/price/year buckets) to get the rest.",
            meta["unique_vehicles_collected"], meta["total_reported"],
            100 * meta["unique_vehicles_collected"] / max(meta["total_reported"], 1),
        )

    vehicles_path = out_dir / f"carvana_vehicles_{args.zip5}.json"
    meta_path = out_dir / f"carvana_meta_{args.zip5}.json"

    with open(vehicles_path, "w") as f:
        json.dump(list(vehicles_by_vin.values()), f, indent=2)
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    log.info("Wrote %s vehicles to %s", len(vehicles_by_vin), vehicles_path)
    log.info("Wrote run metadata to %s", meta_path)


if __name__ == "__main__":
    sys.exit(main())