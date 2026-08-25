#!/usr/bin/env python3
"""Merge locally imported PDF attachments into canonical Zotero items."""

from __future__ import annotations

import argparse
import json
import os
import time

from zotero_condition_collapse_library import (
    ARXIV_ID_RE,
    PAPERS,
    arxiv_id_from_item,
    fetch_all,
)
from zotero_publish import ZoteroClient


STAGING_TAG = "condition-collapse-local-pdf-staging"


def tags(item: dict) -> set[str]:
    return {
        str(tag.get("tag", ""))
        for tag in item.get("data", {}).get("tags", [])
        if isinstance(tag, dict)
    }


def children(client: ZoteroClient, parent_key: str) -> list[dict]:
    values = client.request(
        "GET",
        f"/users/{client.user_id}/items/{parent_key}/children?limit=100&format=json",
    )
    if not isinstance(values, list):
        raise RuntimeError(f"Invalid Zotero children response for {parent_key}")
    return [value for value in values if isinstance(value, dict)]


def is_real_pdf(item: dict) -> bool:
    data = item.get("data", {})
    if data.get("itemType") != "attachment":
        return False
    link_mode = str(data.get("linkMode", ""))
    content_type = str(data.get("contentType", "")).lower()
    filename = str(data.get("filename", "")).lower()
    return link_mode != "linked_url" and (
        content_type == "application/pdf" or filename.endswith(".pdf")
    )


def is_arxiv_pdf_link(item: dict, arxiv_id: str) -> bool:
    data = item.get("data", {})
    return (
        data.get("itemType") == "attachment"
        and data.get("linkMode") == "linked_url"
        and f"arxiv.org/pdf/{arxiv_id}" in str(data.get("url", "")).lower()
    )


def delete_item(client: ZoteroClient, item: dict) -> None:
    key = item.get("key")
    version = item.get("version")
    if not isinstance(key, str) or not isinstance(version, int):
        raise RuntimeError("Cannot delete Zotero item without key/version")
    client.request(
        "DELETE",
        f"/users/{client.user_id}/items/{key}",
        extra_headers={"If-Unmodified-Since-Version": str(version)},
    )


def reparent_attachment(
    client: ZoteroClient,
    attachment: dict,
    canonical_key: str,
) -> None:
    data = dict(attachment.get("data", {}))
    key = attachment.get("key")
    version = attachment.get("version")
    if not isinstance(key, str) or not isinstance(version, int):
        raise RuntimeError("Cannot reparent Zotero attachment without key/version")
    data["parentItem"] = canonical_key
    data["title"] = "Full Text PDF"
    client.request(
        "PUT",
        f"/users/{client.user_id}/items/{key}",
        data,
        extra_headers={"If-Unmodified-Since-Version": str(version)},
    )


def discover(
    client: ZoteroClient,
    papers: list,
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    top_items = fetch_all(client, "items/top")
    canonical: dict[str, dict] = {}
    staging: dict[str, list[dict]] = {}
    expected = {spec.arxiv_id for spec in papers}
    for item in top_items:
        arxiv_id = arxiv_id_from_item(item)
        if arxiv_id not in expected:
            continue
        if STAGING_TAG in tags(item):
            staging.setdefault(arxiv_id, []).append(item)
            continue
        if arxiv_id in canonical:
            raise RuntimeError(f"Duplicate canonical item for {arxiv_id}")
        canonical[arxiv_id] = item
    return canonical, staging


def wait_for_staging(
    client: ZoteroClient,
    papers: list,
    wait_seconds: int,
) -> tuple[
    dict[str, dict],
    dict[str, list[dict]],
    dict[str, dict],
    dict[str, dict],
]:
    expected = {spec.arxiv_id for spec in papers}
    total = len(expected)
    deadline = time.monotonic() + wait_seconds
    while True:
        canonical, staging = discover(client, papers)
        ready_staging: dict[str, dict] = {}
        ready_attachments: dict[str, dict] = {}
        for arxiv_id, items in staging.items():
            candidates: list[tuple[dict, dict]] = []
            for item in items:
                real = [
                    value
                    for value in children(client, item["key"])
                    if is_real_pdf(value)
                ]
                if len(real) > 1:
                    raise RuntimeError(f"Multiple PDF attachments under one staging item for {arxiv_id}")
                if real:
                    candidates.append((item, real[0]))
            if len(candidates) > 1:
                raise RuntimeError(f"Multiple staged PDF copies for {arxiv_id}")
            if candidates:
                ready_staging[arxiv_id], ready_attachments[arxiv_id] = candidates[0]
        missing_canonical = expected - set(canonical)
        missing_staging = expected - set(staging)
        missing_pdf = expected - set(ready_attachments)
        if not missing_canonical and not missing_staging and not missing_pdf:
            return canonical, staging, ready_staging, ready_attachments
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Timed out waiting for Zotero sync; "
                f"canonical={len(canonical)}/{total} staging={len(staging)}/{total} "
                f"pdf={len(ready_attachments)}/{total}"
            )
        print(
            f"Waiting for Zotero sync: canonical={len(canonical)}/{total} "
            f"staging={len(staging)}/{total} pdf={len(ready_attachments)}/{total}",
            flush=True,
        )
        time.sleep(15)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument("--arxiv-id")
    args = parser.parse_args()
    papers = PAPERS
    if args.arxiv_id:
        papers = [spec for spec in PAPERS if spec.arxiv_id == args.arxiv_id]
        if not papers:
            raise RuntimeError(f"Unknown condition-collapse arXiv ID: {args.arxiv_id}")
    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not api_key or not user_id.isdigit():
        raise RuntimeError("ZOTERO_API_KEY and numeric ZOTERO_USER_ID are required")
    client = ZoteroClient(int(user_id), api_key)

    canonical, staging, ready_staging, staged_pdfs = wait_for_staging(
        client, papers, args.wait_seconds
    )
    reparented = 0
    links_deleted = 0
    staging_deleted = 0
    for index, spec in enumerate(papers, 1):
        canonical_item = canonical[spec.arxiv_id]
        staging_items = staging[spec.arxiv_id]
        staging_item = ready_staging[spec.arxiv_id]
        canonical_children = children(client, canonical_item["key"])
        real_existing = [value for value in canonical_children if is_real_pdf(value)]
        if real_existing:
            print(f"[{index:02d}/{len(papers)}] canonical PDF already exists: {spec.short_name}")
        else:
            reparent_attachment(client, staged_pdfs[spec.arxiv_id], canonical_item["key"])
            reparented += 1
            print(f"[{index:02d}/{len(papers)}] attached real PDF: {spec.short_name}")
        for linked in canonical_children:
            if is_arxiv_pdf_link(linked, spec.arxiv_id):
                delete_item(client, linked)
                links_deleted += 1
        for temporary_item in staging_items:
            delete_item(client, temporary_item)
            staging_deleted += 1
        time.sleep(0.1)

    print(
        json.dumps(
            {
                "papers": len(papers),
                "real_pdfs_reparented": reparented,
                "old_pdf_links_deleted": links_deleted,
                "staging_items_deleted": staging_deleted,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
