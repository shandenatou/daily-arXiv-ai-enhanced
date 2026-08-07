#!/usr/bin/env python3
"""Build and maintain a curated World-Action Model collection in Zotero.

The importer is deliberately idempotent:

* existing Zotero items are matched by arXiv ID, then by normalized title;
* matched items are added to the WAM collections instead of being recreated;
* missing items are populated from arXiv metadata;
* a linked arXiv PDF attachment is added only when an item has no PDF child.

The script uses linked PDF attachments because personal Zotero libraries may
use WebDAV rather than Zotero Storage. Existing imported PDF files are kept.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from zotero_publish import ZoteroClient


ROOT_COLLECTION = "wam"
USER_AGENT = "daily-arxiv-wam-curator/1.0 (personal research library)"
ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_ID_RE = re.compile(
    r"(?:arxiv(?:\.org/(?:abs|pdf)/|\s*[:.]\s*)|10\.48550/arxiv\.)(\d{4}\.\d{4,5})",
    re.IGNORECASE,
)

COLLECTIONS = {
    "survey": "00-综述与技术脉络",
    "core": "01-核心基模",
    "efficient": "02-高效推理",
    "latent": "03-潜空间与表征",
    "robust": "04-鲁棒性与泛化",
    "specialized": "05-力觉触觉与专用方向",
    "online": "06-在线强化学习与适应",
}


@dataclass(frozen=True)
class PaperSpec:
    arxiv_id: str
    short_name: str
    category: str


# Thirty papers tracing the field from video-policy precursors to current WAM systems.
# Keep this list intentionally selective: the living WAM survey contains over
# one hundred related papers, many of which are narrower follow-up variants.
PAPERS = [
    PaperSpec("2605.12090", "WAM Frontier Survey", "survey"),
    PaperSpec("2606.20781", "World Action Models: A Survey", "survey"),
    PaperSpec("2302.00111", "UniPi", "core"),
    PaperSpec("2312.13139", "GR-1", "core"),
    PaperSpec("2411.18179", "PAD", "core"),
    PaperSpec("2412.14803", "VPP", "core"),
    PaperSpec("2503.00200", "UVA", "core"),
    PaperSpec("2504.02792", "UWM", "core"),
    PaperSpec("2512.13030", "Motus", "core"),
    PaperSpec("2601.21998", "LingBot-VA", "core"),
    PaperSpec("2602.15922", "DreamZero", "core"),
    PaperSpec("2603.17240", "GigaWorld-Policy", "core"),
    PaperSpec("2604.27792", "MotuBrain", "core"),
    PaperSpec("2607.08639", "LingBot-VA 2.0", "core"),
    PaperSpec("2607.13960", "GigaWorld-Policy-0.5", "core"),
    PaperSpec("2601.16163", "Cosmos Policy", "latent"),
    PaperSpec("2603.10448", "DiT4DiT", "latent"),
    PaperSpec("2606.01027", "tau_0-WM", "latent"),
    PaperSpec("2606.01955", "WALL-WM", "latent"),
    PaperSpec("2606.15768", "LaWAM", "latent"),
    PaperSpec("2608.03701", "LiLa-WAM", "latent"),
    PaperSpec("2608.06375", "omega-0", "latent"),
    PaperSpec("2603.16666", "Fast-WAM", "efficient"),
    PaperSpec("2606.05254", "Flash-WAM", "efficient"),
    PaperSpec("2606.10040", "Efficient-WAM", "efficient"),
    PaperSpec("2606.09811", "AHA-WAM", "efficient"),
    PaperSpec("2606.19531", "ImageWAM", "robust"),
    PaperSpec("2608.04996", "DreamWAM", "robust"),
    PaperSpec("2608.05903", "Robust-WAM", "robust"),
    PaperSpec("2607.04265", "HALO-WA", "online"),
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


def create_collection(client: ZoteroClient, name: str, parent: str | bool) -> dict:
    response = client.request(
        "POST",
        f"/users/{client.user_id}/collections",
        [{"name": name, "parentCollection": parent, "relations": {}}],
    )
    successful = response.get("successful", {}) if isinstance(response, dict) else {}
    created = successful.get("0")
    if not isinstance(created, dict) or not created.get("key"):
        raise RuntimeError(f"Zotero did not create collection {name}: {response}")
    return created


def ensure_collection(
    client: ZoteroClient,
    collections: list[dict],
    name: str,
    parent: str | bool,
) -> tuple[str, bool]:
    matches = [
        value
        for value in collections
        if value.get("data", {}).get("name", "").casefold() == name.casefold()
        and value.get("data", {}).get("parentCollection") == parent
    ]
    if len(matches) > 1:
        raise RuntimeError(f"Multiple Zotero collections match {name!r}")
    if matches:
        return matches[0]["key"], False
    created = create_collection(client, name, parent)
    collections.append(created)
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


def build_item(spec: PaperSpec, metadata: dict, collection_keys: set[str]) -> dict:
    arxiv_id = spec.arxiv_id
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
        "DOI": f"10.48550/arXiv.{arxiv_id}",
        "ISSN": "",
        "shortTitle": spec.short_name,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "accessDate": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "archive": "arXiv",
        "archiveLocation": arxiv_id,
        "libraryCatalog": "arXiv.org",
        "callNumber": "",
        "rights": "",
        "extra": f"arXiv: {arxiv_id}",
        "tags": [
            {"tag": "WAM"},
            {"tag": "World Action Model"},
            {"tag": f"WAM/{COLLECTIONS[spec.category]}"},
            {"tag": f"arXiv:{arxiv_id}"},
        ],
        "collections": sorted(collection_keys),
        "relations": {},
    }


def update_existing_item(
    client: ZoteroClient,
    item: dict,
    spec: PaperSpec,
    collection_keys: set[str],
) -> bool:
    data = dict(item.get("data", {}))
    current_collections = set(data.get("collections", []))
    current_tags = {
        tag.get("tag", "") for tag in data.get("tags", []) if isinstance(tag, dict)
    }
    wanted_tags = {
        "WAM",
        "World Action Model",
        f"WAM/{COLLECTIONS[spec.category]}",
        f"arXiv:{spec.arxiv_id}",
    }
    changed = not collection_keys.issubset(current_collections) or not wanted_tags.issubset(
        current_tags
    )
    if not changed:
        return False
    data["collections"] = sorted(current_collections | collection_keys)
    data["tags"] = [{"tag": value} for value in sorted(current_tags | wanted_tags) if value]
    key = item.get("key")
    version = item.get("version")
    if not isinstance(key, str) or not isinstance(version, int):
        raise RuntimeError("Existing Zotero item lacks key/version metadata")
    client.request(
        "PUT",
        f"/users/{client.user_id}/items/{key}",
        data,
        extra_headers={"If-Unmodified-Since-Version": str(version)},
    )
    return True


def create_linked_pdf(client: ZoteroClient, parent_key: str, spec: PaperSpec) -> None:
    client.create_one(
        {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "linked_url",
            "title": f"arXiv PDF — {spec.short_name}",
            "accessDate": "",
            "url": f"https://arxiv.org/pdf/{spec.arxiv_id}",
            "note": "",
            "tags": [{"tag": "WAM"}, {"tag": f"arXiv:{spec.arxiv_id}"}],
            "collections": [],
            "relations": {},
            "contentType": "application/pdf",
            "charset": "",
        }
    )


def item_has_pdf(client: ZoteroClient, parent_key: str) -> bool:
    children = client.request(
        "GET",
        f"/users/{client.user_id}/items/{parent_key}/children?limit=100&format=json",
    )
    if not isinstance(children, list):
        raise RuntimeError(f"Zotero returned invalid children for {parent_key}")
    for child in children:
        data = child.get("data", {})
        if data.get("itemType") != "attachment":
            continue
        content_type = str(data.get("contentType", "")).lower()
        url = str(data.get("url", "")).lower()
        title = str(data.get("title", "")).lower()
        if content_type == "application/pdf" or "arxiv.org/pdf/" in url or "pdf" in title:
            return True
    return False


def validate_specs() -> None:
    ids = [spec.arxiv_id for spec in PAPERS]
    if len(ids) != 30:
        raise RuntimeError(f"The curated WAM list must contain 30 papers, found {len(ids)}")
    if len(set(ids)) != len(ids):
        raise RuntimeError("The curated WAM list contains duplicate arXiv IDs")
    unknown = sorted({spec.category for spec in PAPERS} - set(COLLECTIONS))
    if unknown:
        raise RuntimeError("Unknown WAM categories: " + ", ".join(unknown))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    validate_specs()
    metadata = fetch_arxiv_metadata(PAPERS)
    if args.dry_run:
        for spec in PAPERS:
            print(f"{spec.arxiv_id}\t{spec.category}\t{metadata[spec.arxiv_id]['title']}")
        print(f"Validated {len(PAPERS)} curated WAM papers from arXiv.")
        return 0

    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id_raw = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not api_key or not user_id_raw.isdigit():
        raise RuntimeError("ZOTERO_API_KEY and numeric ZOTERO_USER_ID are required")
    client = ZoteroClient(int(user_id_raw), api_key)

    collections = fetch_all(client, "collections")
    root_key, root_created = ensure_collection(client, collections, ROOT_COLLECTION, False)
    category_keys: dict[str, str] = {}
    created_collections = int(root_created)
    for category, name in COLLECTIONS.items():
        key, created = ensure_collection(client, collections, name, root_key)
        category_keys[category] = key
        created_collections += int(created)

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
    linked_pdfs = 0
    for index, spec in enumerate(PAPERS, 1):
        title_key = normalized_title(metadata[spec.arxiv_id]["title"])
        matches = by_id.get(spec.arxiv_id, []) or by_title.get(title_key, [])
        if len(matches) > 1:
            raise RuntimeError(
                f"Ambiguous Zotero matches for {spec.arxiv_id} {spec.short_name}: "
                + ", ".join(str(value.get("key", "?")) for value in matches)
            )
        collection_keys = {root_key, category_keys[spec.category]}
        if matches:
            item = matches[0]
            parent_key = item["key"]
            reused_items += 1
            if update_existing_item(client, item, spec, collection_keys):
                updated_items += 1
                print(f"[{index:02d}/30] reused+updated {spec.short_name} ({spec.arxiv_id})")
            else:
                print(f"[{index:02d}/30] reused {spec.short_name} ({spec.arxiv_id})")
        else:
            created = client.create_one(build_item(spec, metadata, collection_keys))
            parent_key = created["key"]
            created_items += 1
            print(f"[{index:02d}/30] created {spec.short_name} ({spec.arxiv_id})")
        if not item_has_pdf(client, parent_key):
            create_linked_pdf(client, parent_key, spec)
            linked_pdfs += 1
        time.sleep(0.08)

    print(
        json.dumps(
            {
                "papers": len(PAPERS),
                "created_items": created_items,
                "reused_items": reused_items,
                "updated_existing_items": updated_items,
                "linked_pdf_attachments_created": linked_pdfs,
                "collections_created": created_collections,
                "root_collection": ROOT_COLLECTION,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
