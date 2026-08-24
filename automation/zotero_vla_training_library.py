#!/usr/bin/env python3
"""Maintain the top-level Zotero collection ``vla训练方式``.

The curator is idempotent: it reuses an existing paper matched by arXiv ID,
adds it to the collection, and creates a PDF link only when no PDF child is
already present.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import urllib.parse

from zotero_publish import ZoteroClient


COLLECTION_NAME = "vla训练方式"
ARXIV_ID = "2603.26666"
TITLE = (
    "VLA-OPD: Bridging Offline SFT and Online RL for "
    "Vision-Language-Action Models via On-Policy Distillation"
)
AUTHORS = [
    "Zhide Zhong",
    "Haodong Yan",
    "Junfeng Li",
    "Junjie He",
    "Tianran Zhang",
    "Haoang Li",
]
PUBLISHED = "2026-03-27"
ABSTRACT = (
    "VLA-OPD bridges offline supervised fine-tuning and online reinforcement "
    "learning by letting the student collect on-policy trajectories, asking a "
    "frozen expert teacher for dense token-level supervision on student-visited "
    "states, and optimizing a bounded mode-seeking Reverse-KL objective."
)


def creator(name: str) -> dict[str, str]:
    parts = name.split()
    return {
        "firstName": " ".join(parts[:-1]),
        "lastName": parts[-1],
        "creatorType": "author",
    }


def fetch_all(client: ZoteroClient, endpoint: str) -> list[dict]:
    values: list[dict] = []
    start = 0
    while True:
        query = urllib.parse.urlencode(
            {"limit": "100", "start": str(start), "format": "json"}
        )
        page = client.request("GET", f"/users/{client.user_id}/{endpoint}?{query}")
        if not isinstance(page, list):
            raise RuntimeError(f"Zotero returned an invalid {endpoint} list")
        values.extend(value for value in page if isinstance(value, dict))
        if len(page) < 100:
            return values
        start += len(page)


def ensure_collection(client: ZoteroClient) -> tuple[str, bool]:
    collections = fetch_all(client, "collections/top")
    matches = [
        value
        for value in collections
        if value.get("data", {}).get("name", "").casefold()
        == COLLECTION_NAME.casefold()
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple top-level collections match {COLLECTION_NAME!r}")
    if matches:
        return matches[0]["key"], False
    response = client.request(
        "POST",
        f"/users/{client.user_id}/collections",
        [{"name": COLLECTION_NAME, "parentCollection": False, "relations": {}}],
    )
    created = response.get("successful", {}).get("0") if isinstance(response, dict) else None
    if not isinstance(created, dict) or not created.get("key"):
        raise RuntimeError(f"Zotero did not create collection: {response}")
    return created["key"], True


def arxiv_id_from_item(item: dict) -> str | None:
    data = item.get("data", {})
    fields = [
        str(data.get("url", "")),
        str(data.get("DOI", "")),
        str(data.get("extra", "")),
        " ".join(
            str(tag.get("tag", ""))
            for tag in data.get("tags", [])
            if isinstance(tag, dict)
        ),
    ]
    match = re.search(r"(?:arxiv[.:/ ]+)(\d{4}\.\d{4,5})", "\n".join(fields), re.I)
    return match.group(1) if match else None


def build_item(collection_key: str) -> dict:
    accessed = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "itemType": "journalArticle",
        "title": TITLE,
        "creators": [creator(name) for name in AUTHORS],
        "abstractNote": ABSTRACT,
        "publicationTitle": "arXiv",
        "date": PUBLISHED,
        "journalAbbreviation": "arXiv",
        "language": "en",
        "DOI": f"10.48550/arXiv.{ARXIV_ID}",
        "shortTitle": "VLA-OPD",
        "url": f"https://arxiv.org/abs/{ARXIV_ID}",
        "accessDate": accessed,
        "archive": "arXiv",
        "archiveLocation": ARXIV_ID,
        "libraryCatalog": "arXiv.org",
        "extra": f"arXiv: {ARXIV_ID}",
        "tags": [
            {"tag": "VLA"},
            {"tag": "VLA训练方式"},
            {"tag": "On-Policy Distillation"},
            {"tag": "Reverse-KL"},
            {"tag": f"arXiv:{ARXIV_ID}"},
        ],
        "collections": [collection_key],
        "relations": {},
    }


def ensure_membership(client: ZoteroClient, item: dict, collection_key: str) -> bool:
    data = dict(item.get("data", {}))
    collections = set(data.get("collections", []))
    tags = {
        tag.get("tag", "")
        for tag in data.get("tags", [])
        if isinstance(tag, dict)
    }
    wanted_tags = {
        "VLA",
        "VLA训练方式",
        "On-Policy Distillation",
        "Reverse-KL",
        f"arXiv:{ARXIV_ID}",
    }
    if collection_key in collections and wanted_tags.issubset(tags):
        return False
    collections.add(collection_key)
    tags.update(wanted_tags)
    data["collections"] = sorted(collections)
    data["tags"] = [{"tag": value} for value in sorted(tags) if value]
    client.request(
        "PUT",
        f"/users/{client.user_id}/items/{item['key']}",
        data,
        extra_headers={"If-Unmodified-Since-Version": str(item["version"])},
    )
    return True


def has_pdf(client: ZoteroClient, parent_key: str) -> bool:
    children = client.request(
        "GET",
        f"/users/{client.user_id}/items/{parent_key}/children?limit=100&format=json",
    )
    if not isinstance(children, list):
        return False
    for child in children:
        data = child.get("data", {})
        if data.get("itemType") != "attachment":
            continue
        if (
            str(data.get("contentType", "")).lower() == "application/pdf"
            or "arxiv.org/pdf/" in str(data.get("url", "")).lower()
        ):
            return True
    return False


def create_pdf_link(client: ZoteroClient, parent_key: str) -> None:
    client.create_one(
        {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "linked_url",
            "title": "arXiv PDF — VLA-OPD",
            "accessDate": "",
            "url": f"https://arxiv.org/pdf/{ARXIV_ID}",
            "note": "",
            "tags": [{"tag": f"arXiv:{ARXIV_ID}"}],
            "collections": [],
            "relations": {},
            "contentType": "application/pdf",
            "charset": "",
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        print(f"{ARXIV_ID}\t{COLLECTION_NAME}\t{TITLE}")
        return 0

    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not api_key or not user_id.isdigit():
        raise RuntimeError("ZOTERO_API_KEY and numeric ZOTERO_USER_ID are required")
    client = ZoteroClient(int(user_id), api_key)
    collection_key, collection_created = ensure_collection(client)

    items = fetch_all(client, "items/top")
    matches = [item for item in items if arxiv_id_from_item(item) == ARXIV_ID]
    if len(matches) > 1:
        raise RuntimeError("Multiple Zotero items match VLA-OPD")
    created_item = False
    updated_item = False
    if matches:
        item = matches[0]
        parent_key = item["key"]
        updated_item = ensure_membership(client, item, collection_key)
    else:
        created = client.create_one(build_item(collection_key))
        parent_key = created["key"]
        created_item = True

    pdf_link_created = False
    if not has_pdf(client, parent_key):
        create_pdf_link(client, parent_key)
        pdf_link_created = True

    print(
        json.dumps(
            {
                "collection": COLLECTION_NAME,
                "collection_created": collection_created,
                "paper": "VLA-OPD",
                "arxiv_id": ARXIV_ID,
                "item_created": created_item,
                "existing_item_updated": updated_item,
                "pdf_link_created": pdf_link_created,
                "item_key": parent_key,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
