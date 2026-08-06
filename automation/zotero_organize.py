#!/usr/bin/env python3
"""Organize daily reports and recommended papers into dated Zotero collections."""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import urllib.parse

from zotero_publish import ZoteroClient


ROOT_COLLECTION = "每日论文推荐"
DATE_TAG_RE = re.compile(r"^日报 (20\d{2}-\d{2}-\d{2})$")
DATE_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
RELEVANT_TYPES = {"report", "journalArticle", "preprint"}


def fetch_all(client: ZoteroClient, endpoint: str, parameters: dict[str, str]) -> list[dict]:
    values: list[dict] = []
    start = 0
    while True:
        query = urllib.parse.urlencode({**parameters, "limit": "100", "start": str(start), "format": "json"})
        page = client.request("GET", f"/users/{client.user_id}/{endpoint}?{query}")
        if not isinstance(page, list):
            raise RuntimeError(f"Zotero returned an invalid {endpoint} list")
        values.extend(value for value in page if isinstance(value, dict))
        if len(page) < 100:
            break
        start += len(page)
    return values


def create_collection(client: ZoteroClient, name: str, parent: str | bool) -> dict:
    payload = {
        "name": name,
        "parentCollection": parent,
        "relations": {},
    }
    response = client.request("POST", f"/users/{client.user_id}/collections", [payload])
    successful = response.get("successful", {}) if isinstance(response, dict) else {}
    created = successful.get("0")
    if not isinstance(created, dict) or not created.get("key"):
        raise RuntimeError(f"Zotero did not create collection {name}")
    return created


def collection_index(client: ZoteroClient) -> list[dict]:
    return fetch_all(client, "collections", {})


def ensure_unique_collection(
    client: ZoteroClient,
    collections: list[dict],
    name: str,
    parent: str | bool,
) -> tuple[str, bool]:
    matches = [
        value
        for value in collections
        if value.get("data", {}).get("name") == name
        and value.get("data", {}).get("parentCollection") == parent
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Zotero contains duplicate collections named {name}")
    if matches:
        return matches[0]["key"], False
    created = create_collection(client, name, parent)
    collections.append(created)
    return created["key"], True


def dates_for_item(item: dict) -> set[str]:
    dates: set[str] = set()
    for tag in item.get("data", {}).get("tags", []):
        value = tag.get("tag", "") if isinstance(tag, dict) else ""
        match = DATE_TAG_RE.fullmatch(value)
        if match:
            dates.add(match.group(1))
    return dates


def relevant_items(client: ZoteroClient, target_date: str | None) -> dict[str, dict]:
    found: dict[str, dict] = {}
    tags = [f"日报 {target_date}"] if target_date else ["daily-paper-recommendations", "daily-papers"]
    for tag in tags:
        for item in fetch_all(client, "items", {"tag": tag}):
            data = item.get("data", {})
            key = item.get("key")
            if isinstance(key, str) and data.get("itemType") in RELEVANT_TYPES and dates_for_item(item):
                found[key] = item
    return found


def add_to_collections(client: ZoteroClient, item: dict, collection_keys: set[str]) -> bool:
    data = dict(item.get("data", {}))
    current = set(data.get("collections", []))
    if collection_keys.issubset(current):
        return False
    data["collections"] = sorted(current | collection_keys)
    key = item.get("key")
    version = item.get("version")
    if not isinstance(key, str) or not isinstance(version, int):
        raise RuntimeError("Zotero item lacks key/version metadata")
    try:
        client.request(
            "PUT",
            f"/users/{client.user_id}/items/{key}",
            data,
            extra_headers={"If-Unmodified-Since-Version": str(version)},
        )
    except RuntimeError as exc:
        if "HTTP 412" not in str(exc):
            raise
        fresh = client.request("GET", f"/users/{client.user_id}/items/{key}?format=json")
        if not isinstance(fresh, dict):
            raise RuntimeError(f"Could not refresh conflicting Zotero item {key}") from exc
        fresh_data = dict(fresh.get("data", {}))
        fresh_data["collections"] = sorted(set(fresh_data.get("collections", [])) | collection_keys)
        client.request(
            "PUT",
            f"/users/{client.user_id}/items/{key}",
            fresh_data,
            extra_headers={"If-Unmodified-Since-Version": str(fresh["version"])},
        )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true")
    group.add_argument("--date")
    args = parser.parse_args()
    if args.date and not DATE_RE.fullmatch(args.date):
        raise RuntimeError("--date must use YYYY-MM-DD")

    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id_raw = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not api_key or not user_id_raw.isdigit():
        raise RuntimeError("ZOTERO_API_KEY and numeric ZOTERO_USER_ID are required")
    client = ZoteroClient(int(user_id_raw), api_key)
    items = relevant_items(client, args.date)
    if not items:
        print("No dated Zotero reports or papers need organizing.")
        return 0

    item_dates = {key: dates_for_item(item) for key, item in items.items()}
    all_dates = sorted(set().union(*item_dates.values()))
    collections = collection_index(client)
    root_key, root_created = ensure_unique_collection(client, collections, ROOT_COLLECTION, False)
    date_keys: dict[str, str] = {}
    created_count = int(root_created)
    for value in all_dates:
        date_keys[value], created = ensure_unique_collection(client, collections, value, root_key)
        created_count += int(created)

    updated_count = 0
    for key, item in items.items():
        wanted = {date_keys[value] for value in item_dates[key]}
        if add_to_collections(client, item, wanted):
            updated_count += 1
        time.sleep(0.08)
    print(
        f"Organized {len(items)} Zotero items across {len(all_dates)} dates; "
        f"created {created_count} collections; updated {updated_count} items."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
