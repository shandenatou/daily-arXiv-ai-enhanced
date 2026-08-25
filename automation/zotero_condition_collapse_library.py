#!/usr/bin/env python3
"""Build and maintain the Zotero collection ``抑制条件塌陷``.

The importer is idempotent. Existing items are matched by arXiv ID and then
normalized title, moved into the collection, and tagged without duplication.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from zotero_publish import ZoteroClient


COLLECTION_NAME = "抑制条件塌陷"
USER_AGENT = "daily-arxiv-condition-collapse-curator/1.0"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ID_RE = re.compile(
    r"(?:arxiv(?:\.org/(?:abs|pdf)/|\s*[:.]\s*)|10\.48550/arxiv\.)(\d{4}\.\d{4,5})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PaperSpec:
    arxiv_id: str
    short_name: str
    category: str


PAPERS = [
    PaperSpec("2602.17659", "When Vision Overrides Language / CAG", "直接诊断与干预"),
    PaperSpec("2603.00592", "LangGap", "直接诊断与干预"),
    PaperSpec("2603.06001", "IGAR", "推理阶段补丁"),
    PaperSpec("2601.04052", "Stable Language Guidance / RSS", "直接诊断与干预"),
    PaperSpec("2601.15197", "LangForce", "直接诊断与干预"),
    PaperSpec("2608.04396", "CofactVLA", "直接诊断与干预"),
    PaperSpec("2512.11218", "BayesVLA", "直接诊断与干预"),
    PaperSpec("2607.13429", "Anchor-Align", "训练目标与后训练"),
    PaperSpec("2605.15735", "UAM", "训练目标与后训练"),
    PaperSpec("2504.16054", "π0.5", "数据与动作表征"),
    PaperSpec("2605.30280", "Qwen-VLA", "数据与动作表征"),
    PaperSpec("2601.03136", "Limited Linguistic Diversity", "数据与评测"),
    PaperSpec("2606.27295", "LA4VLA", "数据与动作表征"),
    PaperSpec("2605.27284", "FineVLA", "数据与动作表征"),
    PaperSpec("2608.10484", "SALT", "数据与动作表征"),
    PaperSpec("2509.22195", "Actions as Language", "训练目标与后训练"),
    PaperSpec("2507.17520", "InstructVLA", "训练目标与后训练"),
    PaperSpec("2605.30877", "Wall-OSS-0.5", "数据与动作表征"),
    PaperSpec("2607.01586", "VLAFlow", "训练目标与后训练"),
    PaperSpec("2607.04517", "VLA Grounder", "推理阶段补丁"),
]


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def creator(name: str) -> dict[str, str]:
    parts = clean_text(name).split()
    if len(parts) >= 2:
        return {
            "firstName": " ".join(parts[:-1]),
            "lastName": parts[-1],
            "creatorType": "author",
        }
    return {"name": clean_text(name), "creatorType": "author"}


def fetch_arxiv_metadata(specs: list[PaperSpec]) -> dict[str, dict]:
    query = urllib.parse.urlencode(
        {
            "id_list": ",".join(spec.arxiv_id for spec in specs),
            "start": "0",
            "max_results": str(len(specs)),
        }
    )
    request = urllib.request.Request(
        ARXIV_API + "?" + query,
        headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"},
    )
    final_error: Exception | None = None
    body = b""
    for attempt in range(1, 5):
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = response.read()
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            final_error = exc
            if attempt == 4:
                raise RuntimeError(f"Could not fetch arXiv metadata: {final_error}") from exc
            time.sleep(attempt * 3)

    atom = "{http://www.w3.org/2005/Atom}"
    root = ET.fromstring(body)
    metadata: dict[str, dict] = {}
    for entry in root.findall(atom + "entry"):
        entry_id = clean_text(entry.findtext(atom + "id", default=""))
        match = re.search(r"/(\d{4}\.\d{4,5})(?:v\d+)?$", entry_id)
        if not match:
            continue
        arxiv_id = match.group(1)
        metadata[arxiv_id] = {
            "title": clean_text(entry.findtext(atom + "title", default=arxiv_id)),
            "abstract": clean_text(entry.findtext(atom + "summary", default="")),
            "published": clean_text(entry.findtext(atom + "published", default=""))[:10],
            "authors": [
                clean_text(author.findtext(atom + "name", default=""))
                for author in entry.findall(atom + "author")
                if clean_text(author.findtext(atom + "name", default=""))
            ],
        }
    missing = [spec.arxiv_id for spec in specs if spec.arxiv_id not in metadata]
    if missing:
        raise RuntimeError("arXiv metadata is missing IDs: " + ", ".join(missing))
    return metadata


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
    match = ARXIV_ID_RE.search("\n".join(fields))
    return match.group(1) if match else None


def build_item(spec: PaperSpec, metadata: dict, collection_key: str) -> dict:
    return {
        "itemType": "journalArticle",
        "title": metadata["title"],
        "creators": [creator(name) for name in metadata["authors"]],
        "abstractNote": metadata["abstract"],
        "publicationTitle": "arXiv",
        "volume": "",
        "issue": "",
        "pages": "",
        "date": metadata["published"],
        "series": "",
        "seriesTitle": "",
        "seriesText": "",
        "journalAbbreviation": "arXiv",
        "language": "en",
        "DOI": f"10.48550/arXiv.{spec.arxiv_id}",
        "ISSN": "",
        "shortTitle": spec.short_name,
        "url": f"https://arxiv.org/abs/{spec.arxiv_id}",
        "accessDate": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "archive": "arXiv",
        "archiveLocation": spec.arxiv_id,
        "libraryCatalog": "arXiv.org",
        "callNumber": "",
        "rights": "",
        "extra": f"arXiv: {spec.arxiv_id}",
        "tags": [
            {"tag": "VLA"},
            {"tag": "抑制条件塌陷"},
            {"tag": "language conditioning"},
            {"tag": spec.category},
            {"tag": f"arXiv:{spec.arxiv_id}"},
        ],
        "collections": [collection_key],
        "relations": {},
    }


def update_existing_item(
    client: ZoteroClient,
    item: dict,
    spec: PaperSpec,
    collection_key: str,
) -> bool:
    data = dict(item.get("data", {}))
    collections = set(data.get("collections", []))
    tags = {
        tag.get("tag", "")
        for tag in data.get("tags", [])
        if isinstance(tag, dict)
    }
    wanted_tags = {
        "VLA",
        "抑制条件塌陷",
        "language conditioning",
        spec.category,
        f"arXiv:{spec.arxiv_id}",
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


def item_has_pdf(client: ZoteroClient, parent_key: str) -> bool:
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


def create_pdf_link(client: ZoteroClient, parent_key: str, spec: PaperSpec) -> None:
    client.create_one(
        {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "linked_url",
            "title": f"arXiv PDF — {spec.short_name}",
            "accessDate": "",
            "url": f"https://arxiv.org/pdf/{spec.arxiv_id}",
            "note": "",
            "tags": [
                {"tag": "抑制条件塌陷"},
                {"tag": f"arXiv:{spec.arxiv_id}"},
            ],
            "collections": [],
            "relations": {},
            "contentType": "application/pdf",
            "charset": "",
        }
    )


def validate_specs() -> None:
    ids = [spec.arxiv_id for spec in PAPERS]
    if len(ids) != 20:
        raise RuntimeError(f"Expected 20 unique papers, found {len(ids)}")
    if len(set(ids)) != len(ids):
        raise RuntimeError("The condition-collapse list contains duplicate arXiv IDs")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    validate_specs()
    metadata = fetch_arxiv_metadata(PAPERS)
    if args.dry_run:
        for spec in PAPERS:
            print(f"{spec.arxiv_id}\t{spec.category}\t{metadata[spec.arxiv_id]['title']}")
        print(f"Validated {len(PAPERS)} condition-collapse papers from arXiv.")
        return 0

    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not api_key or not user_id.isdigit():
        raise RuntimeError("ZOTERO_API_KEY and numeric ZOTERO_USER_ID are required")
    client = ZoteroClient(int(user_id), api_key)
    collection_key, collection_created = ensure_collection(client)

    top_items = fetch_all(client, "items/top")
    by_id: dict[str, list[dict]] = {}
    by_title: dict[str, list[dict]] = {}
    for item in top_items:
        arxiv_id = arxiv_id_from_item(item)
        if arxiv_id:
            by_id.setdefault(arxiv_id, []).append(item)
        title = normalized_title(str(item.get("data", {}).get("title", "")))
        if title:
            by_title.setdefault(title, []).append(item)

    created_items = 0
    reused_items = 0
    updated_items = 0
    pdf_links = 0
    for index, spec in enumerate(PAPERS, 1):
        title_key = normalized_title(metadata[spec.arxiv_id]["title"])
        matches = by_id.get(spec.arxiv_id, []) or by_title.get(title_key, [])
        if len(matches) > 1:
            raise RuntimeError(
                f"Ambiguous Zotero matches for {spec.arxiv_id}: "
                + ", ".join(str(value.get("key", "?")) for value in matches)
            )
        if matches:
            item = matches[0]
            parent_key = item["key"]
            reused_items += 1
            if update_existing_item(client, item, spec, collection_key):
                updated_items += 1
                print(f"[{index:02d}/{len(PAPERS)}] reused+updated {spec.short_name}")
            else:
                print(f"[{index:02d}/{len(PAPERS)}] reused {spec.short_name}")
        else:
            created = client.create_one(
                build_item(spec, metadata[spec.arxiv_id], collection_key)
            )
            parent_key = created["key"]
            created_items += 1
            print(f"[{index:02d}/{len(PAPERS)}] created {spec.short_name}")
        if not item_has_pdf(client, parent_key):
            create_pdf_link(client, parent_key, spec)
            pdf_links += 1
        time.sleep(0.08)

    print(
        json.dumps(
            {
                "collection": COLLECTION_NAME,
                "collection_created": collection_created,
                "papers": len(PAPERS),
                "created_items": created_items,
                "reused_items": reused_items,
                "updated_existing_items": updated_items,
                "pdf_links_created": pdf_links,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
