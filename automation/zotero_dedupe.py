#!/usr/bin/env python3
"""Load and save recommendation deduplication state in a Zotero note."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from zotero_publish import ZoteroClient


STATE_TAG = "daily-paper-dedupe-state"
STATE_MARKER = 'data-daily-paper-dedupe="true"'
HASH_RE = re.compile(r"[0-9a-f]{64}")
ARXIV_RE = re.compile(r"arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", re.IGNORECASE)


def empty_state() -> dict:
    return {"version": 1, "recommended_id_hashes": []}


def validate_state(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("dedupe state must be a JSON object")
    hashes = value.get("recommended_id_hashes", [])
    if not isinstance(hashes, list) or any(not isinstance(item, str) or not HASH_RE.fullmatch(item) for item in hashes):
        raise ValueError("recommended_id_hashes must contain only SHA-256 strings")
    result = {
        "version": 1,
        "recommended_id_hashes": sorted(set(hashes)),
    }
    last_date = value.get("last_successful_date")
    if isinstance(last_date, str) and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", last_date):
        result["last_successful_date"] = last_date
    return result


def note_html(state: dict) -> str:
    encoded = html.escape(json.dumps(state, ensure_ascii=False, separators=(",", ":")), quote=False)
    return (
        '<div data-schema-version="9"><p>Daily paper recommendation deduplication state. '
        'Managed automatically; do not delete.</p><pre style="display:none" '
        f'{STATE_MARKER}>{encoded}</pre></div>'
    )


def parse_note(note: str) -> dict:
    match = re.search(
        r'<pre[^>]*data-daily-paper-dedupe="true"[^>]*>(.*?)</pre>',
        note,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Zotero dedupe note is missing its state marker")
    return validate_state(json.loads(html.unescape(match.group(1))))


def credentials(read_key_from_stdin: bool) -> tuple[int, str]:
    if read_key_from_stdin:
        api_key = sys.stdin.buffer.read().decode("utf-8").strip()
    else:
        api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{20,64}", api_key):
        raise RuntimeError("A valid Zotero API key is required")

    user_id_raw = os.environ.get("ZOTERO_USER_ID", "").strip()
    if user_id_raw.isdigit():
        return int(user_id_raw), api_key
    if not read_key_from_stdin:
        raise RuntimeError("Numeric ZOTERO_USER_ID is required")

    request = urllib.request.Request(
        "https://api.zotero.org/keys/" + urllib.parse.quote(api_key, safe=""),
        headers={"Zotero-API-Version": "3", "User-Agent": "daily-arxiv-recommendations/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            key_info = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not validate the Zotero API key") from exc
    user_id = key_info.get("userID")
    if not isinstance(user_id, int) or user_id <= 0:
        raise RuntimeError("Zotero did not return a numeric user ID")
    return user_id, api_key


def find_state_note(client: ZoteroClient) -> dict | None:
    query = urllib.parse.urlencode(
        {
            "tag": STATE_TAG,
            "itemType": "note",
            "sort": "dateModified",
            "direction": "desc",
            "limit": "2",
            "format": "json",
        }
    )
    items = client.request("GET", f"/users/{client.user_id}/items?{query}")
    if not isinstance(items, list) or not items:
        return None
    if len(items) > 1:
        raise RuntimeError("Multiple Zotero dedupe notes exist; refusing an ambiguous update")
    return items[0]


def bootstrap_from_reports(client: ZoteroClient) -> dict:
    hashes: set[str] = set()
    start = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "tag": "daily-paper-recommendations",
                "itemType": "report",
                "limit": "100",
                "start": str(start),
                "format": "json",
            }
        )
        reports = client.request("GET", f"/users/{client.user_id}/items?{query}")
        if not isinstance(reports, list):
            raise RuntimeError("Zotero returned an invalid historical report list")
        for report in reports:
            key = report.get("key")
            if not isinstance(key, str):
                continue
            children = client.request(
                "GET",
                f"/users/{client.user_id}/items/{key}/children?itemType=note&limit=100&format=json",
            )
            if not isinstance(children, list):
                continue
            for child in children:
                note = child.get("data", {}).get("note", "")
                for arxiv_id in ARXIV_RE.findall(note):
                    hashes.add(hashlib.sha256(arxiv_id.lower().encode("utf-8")).hexdigest())
        if len(reports) < 100:
            break
        start += len(reports)
    return {"version": 1, "recommended_id_hashes": sorted(hashes)}


def load(client: ZoteroClient) -> tuple[dict, bool]:
    existing = find_state_note(client)
    if not existing:
        return bootstrap_from_reports(client), True
    return parse_note(existing.get("data", {}).get("note", "")), False


def save(client: ZoteroClient, state: dict) -> str:
    existing = find_state_note(client)
    rendered = note_html(state)
    if not existing:
        created = client.create_one(
            {
                "itemType": "note",
                "note": rendered,
                "tags": [{"tag": STATE_TAG}],
                "collections": [],
                "relations": {},
            }
        )
        return created["key"]

    key = existing.get("key")
    version = existing.get("version")
    data = dict(existing.get("data", {}))
    if not isinstance(key, str) or not isinstance(version, int):
        raise RuntimeError("Existing Zotero dedupe note lacks key/version metadata")
    data["note"] = rendered
    data["tags"] = [{"tag": STATE_TAG}]
    client.request(
        "PUT",
        f"/users/{client.user_id}/items/{key}",
        data,
        extra_headers={"If-Unmodified-Since-Version": str(version)},
    )
    return key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-key-stdin", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    load_parser = subparsers.add_parser("load")
    load_parser.add_argument("--output", type=Path, required=True)
    save_parser = subparsers.add_parser("save")
    save_parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()

    user_id, api_key = credentials(args.api_key_stdin)
    client = ZoteroClient(user_id, api_key)
    if args.command == "load":
        state, bootstrapped = load(client)
        args.output.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        source = "historical Zotero reports" if bootstrapped else "the private Zotero state note"
        print(f"Loaded {len(state['recommended_id_hashes'])} private dedupe hashes from {source}.")
        return 0

    state = validate_state(json.loads(args.input.read_text(encoding="utf-8")))
    key = save(client, state)
    print(f"Saved {len(state['recommended_id_hashes'])} private dedupe hashes in Zotero; key={key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
