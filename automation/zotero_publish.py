#!/usr/bin/env python3
"""Publish a generated daily report to a personal Zotero library."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


API_BASE = "https://api.zotero.org"
MARKER = 'data-daily-paper-markdown="true"'


def inline_markdown(value: str) -> str:
    escaped = html.escape(value, quote=True)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        lambda match: f'<a href="{match.group(2)}">{match.group(1)}</a>',
        escaped,
    )
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def render_markdown(markdown: str) -> str:
    output: list[str] = []
    list_type: str | None = None

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            output.append(f"</{list_type}>")
            list_type = None

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if not line:
            close_list()
            output.append("<p>&nbsp;</p>")
            continue
        if re.fullmatch(r"-{3,}", line.strip()):
            close_list()
            output.append("<hr>")
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            close_list()
            level = len(heading.group(1))
            output.append(f"<h{level}>{inline_markdown(heading.group(2))}</h{level}>")
            continue
        quote = re.match(r"^>\s?(.*)$", line)
        if quote:
            close_list()
            output.append(f"<blockquote>{inline_markdown(quote.group(1))}</blockquote>")
            continue
        ordered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        unordered = re.match(r"^\s*[-*]\s+(.+)$", line)
        if ordered or unordered:
            wanted = "ol" if ordered else "ul"
            if list_type != wanted:
                close_list()
                output.append(f"<{wanted}>")
                list_type = wanted
            output.append(f"<li>{inline_markdown((ordered or unordered).group(1))}</li>")
            continue
        close_list()
        output.append(f"<p>{inline_markdown(line)}</p>")
    close_list()
    return "".join(output)


def build_note(markdown: str, filename: str) -> str:
    visible = render_markdown(markdown)
    original = html.escape(markdown, quote=False)
    safe_filename = html.escape(filename, quote=True)
    return (
        '<div data-schema-version="9">'
        + visible
        + '<pre style="display:none" data-daily-paper-markdown="true" data-filename="'
        + safe_filename
        + '">'
        + original
        + "</pre></div>"
    )


class ZoteroClient:
    def __init__(self, user_id: int, api_key: str):
        self.user_id = user_id
        self.api_key = api_key

    def request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> object:
        data = None
        headers = {
            "Zotero-API-Key": self.api_key,
            "Zotero-API-Version": "3",
            "User-Agent": "daily-arxiv-recommendations/1.0",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if extra_headers:
            headers.update(extra_headers)
        request = urllib.request.Request(
            API_BASE + path,
            method=method,
            data=data,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"Zotero HTTP {exc.code}: {detail}") from exc
        return json.loads(body.decode("utf-8")) if body else {}

    def find_report(self, run_date: str) -> dict | None:
        query = urllib.parse.urlencode(
            {
                "tag": f"daily-report:{run_date}",
                "itemType": "report",
                "limit": "1",
                "format": "json",
            }
        )
        items = self.request("GET", f"/users/{self.user_id}/items?{query}")
        return items[0] if isinstance(items, list) and items else None

    def create_one(self, item: dict) -> dict:
        response = self.request("POST", f"/users/{self.user_id}/items", [item])
        successful = response.get("successful", {}) if isinstance(response, dict) else {}
        created = successful.get("0")
        if not isinstance(created, dict) or not created.get("key"):
            raise RuntimeError(f"Zotero did not create the item: {json.dumps(response, ensure_ascii=False)[:1500]}")
        return created

    def ensure_note(self, parent_key: str, note_html: str) -> None:
        children = self.request(
            "GET",
            f"/users/{self.user_id}/items/{parent_key}/children?itemType=note&limit=100&format=json",
        )
        if isinstance(children, list):
            for child in children:
                note = child.get("data", {}).get("note", "")
                if MARKER in note:
                    return
        self.create_one(
            {
                "itemType": "note",
                "parentItem": parent_key,
                "note": note_html,
                "tags": [{"tag": "daily-paper-recommendations"}],
                "collections": [],
                "relations": {},
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    run_date = selection["date"]
    markdown = selection["markdown"]
    filename = selection["filename"]
    top5 = selection["top5"]
    title_match = re.search(r"^#\s+(.+)$", markdown, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else f"{run_date}：针对 LingBotVA 的 Top 10"
    report_item = {
        "itemType": "report",
        "title": title,
        "creators": [],
        "date": run_date,
        "reportType": "Daily arXiv Top 10",
        "institution": "Daily Paper Recommendations",
        "language": "zh-CN",
        "url": "",
        "abstractNote": "由每日 arXiv 检索与 DeepSeek V4 Flash 自动生成的 LingBotVA 个性化 Top 10。",
        "extra": "DailyPaperDate: " + run_date + "\nTop5: " + ",".join(top5),
        "tags": [
            {"tag": "daily-paper-recommendations"},
            {"tag": "Top 10"},
            {"tag": "LingBotVA"},
            {"tag": f"日报 {run_date}"},
            {"tag": f"daily-report:{run_date}"},
            *({"tag": f"top5:{arxiv_id}"} for arxiv_id in top5),
        ],
        "collections": [],
        "relations": {},
    }
    note_html = build_note(markdown, filename)
    if args.dry_run:
        print(json.dumps({"report": report_item, "note_bytes": len(note_html.encode('utf-8'))}, ensure_ascii=False, indent=2))
        return 0

    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id_raw = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not api_key or not user_id_raw.isdigit():
        raise RuntimeError("ZOTERO_API_KEY and numeric ZOTERO_USER_ID are required")
    client = ZoteroClient(int(user_id_raw), api_key)
    existing = client.find_report(run_date)
    if existing:
        parent_key = existing["key"]
        created = False
    else:
        parent_key = client.create_one(report_item)["key"]
        created = True
    client.ensure_note(parent_key, note_html)
    print(f"Zotero report {'created' if created else 'already existed'}; note verified; key={parent_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
