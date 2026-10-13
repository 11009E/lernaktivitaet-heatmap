#!/usr/bin/env python3
"""Generate *public* daily learning-minute totals from a private Notion data source.

Only YYYY-MM-DD -> total minutes is written; no page titles, modules,
Notion IDs, access tokens, or other private properties are published.

Required environment variables (GitHub Actions repository secrets):
  NOTION_TOKEN
  NOTION_DATA_SOURCE_ID
"""

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

NOTION_VERSION = "2026-03-11"
API_ROOT = "https://api.notion.com/v1"
LOCAL_TZ = ZoneInfo("Europe/Berlin")


def notion_request(method: str, endpoint: str, token: str, payload=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(f"{API_ROOT}{endpoint}", data=body, headers=headers, method=method)
    for attempt in range(4):
        try:
            with urlopen(req, timeout=30) as res:
                return json.load(res)
        except HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                delay = min(30, max(1, int(exc.headers.get("Retry-After", "2"))))
                time.sleep(delay)
                continue
            # Don't echo response bodies or credentials into public Actions logs.
            raise RuntimeError(f"Notion request failed (HTTP {exc.code}). Check access and API version.") from None
        except URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError("Unable to contact Notion API after retries.") from None
    raise RuntimeError("Notion request failed after retries.")


def check_schema(source: dict):
    props = source.get("properties", {})
    expected = {"Datum": "date", "Lernzeit (Min.)": "number"}
    for name, prop_type in expected.items():
        actual = props.get(name, {}).get("type")
        if actual != prop_type:
            raise ValueError(
                f"Property '{name}' must have type '{prop_type}' (found: {actual!r}). "
                "Nothing will be published until this is corrected."
            )


def all_pages(source_id: str, token: str):
    cursor = None
    while True:
        query = {"page_size": 100}
        if cursor:
            query["start_cursor"] = cursor
        response = notion_request("POST", f"/data_sources/{source_id}/query", token, query)
        for page in response.get("results", []):
            if page.get("object") == "page" and not page.get("in_trash", False) and not page.get("archived", False):
                yield page
        if not response.get("has_more", False):
            return
        cursor = response.get("next_cursor")
        if not cursor:
            raise RuntimeError("Notion returned has_more=true without next_cursor.")


def berlin_date(start: str) -> date:
    if len(start) == 10:
        return date.fromisoformat(start)
    timestamp = datetime.fromisoformat(start.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=LOCAL_TZ)
    return timestamp.astimezone(LOCAL_TZ).date()


def summarize(pages, today: date):
    earliest = today - timedelta(days=364)
    daily = defaultdict(int)
    incomplete_count = 0
    for page in pages:
        properties = page.get("properties", {})
        date_value = properties.get("Datum", {}).get("date")
        minutes_value = properties.get("Lernzeit (Min.)", {}).get("number")
        # Incomplete Notion rows aren't real learning sessions yet.
        if not date_value or not date_value.get("start") or minutes_value is None:
            incomplete_count += 1
            continue
        day = berlin_date(date_value["start"])
        try:
            minutes = Decimal(str(minutes_value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Invalid learning-minute value.") from exc
        if not minutes.is_finite() or minutes < 0 or minutes != minutes.to_integral_value():
            raise ValueError("Learning minutes must be nonnegative whole numbers.")
        if earliest <= day <= today and minutes > 0:
            daily[day.isoformat()] += int(minutes)
    return {day: daily[day] for day in sorted(daily)}, incomplete_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="_site/activity.json")
    args = parser.parse_args()

    token = os.getenv("NOTION_TOKEN")
    source_id = os.getenv("NOTION_DATA_SOURCE_ID")
    if not token or not source_id:
        raise RuntimeError("Required GitHub repository secrets are missing.")

    source = notion_request("GET", f"/data_sources/{source_id}", token)
    check_schema(source)
    today = datetime.now(LOCAL_TZ).date()
    daily, skipped = summarize(all_pages(source_id, token), today)

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    # This file WILL BE PUBLIC after Pages deployment. Publish totals only.
    path.write_text(
        json.dumps({"daily_minutes": daily}, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"Sync complete: {len(daily)} active days; {skipped} incomplete records skipped.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError) as error:
        print(f"Synchronization stopped: {error}", file=sys.stderr)
        sys.exit(1)
