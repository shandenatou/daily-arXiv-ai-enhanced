#!/usr/bin/env python3
"""Export an exact Markdown daily report from a private Zotero library."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from pathlib import Path

from zotero_publish import MARKER, ZoteroClient


DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")
PRE_RE = re.compile(r"<pre\b([^>]*)>(.*?)</pre>", re.DOTALL | re.IGNORECASE)
ARXIV_HEADING_RE = re.compile(
    r"^###\s+(\d+)\.\s+\[.+?\]\(https?://(?:www\.)?arxiv\.org/abs/"
    r"([0-9]{4}\.[0-9]{4,5})(?:v\d+)?[^)]*\)",
    re.MULTILINE,
)


def extract_markdown(note: str, expected_filename: str) -> str | None:
    if MARKER not in note:
        return None
    for attributes, encoded in PRE_RE.findall(note):
        if not re.search(
            r'data-daily-paper-markdown=["\']true["\']',
            attributes,
            re.IGNORECASE,
        ):
            continue
        filename_match = re.search(
            r'data-filename=["\']([^"\']+)["\']',
            attributes,
            re.IGNORECASE,
        )
        if not filename_match:
            raise RuntimeError("Zotero report note is missing data-filename")
        filename = html.unescape(filename_match.group(1))
        if filename != expected_filename:
            raise RuntimeError(
                f"Zotero report filename mismatch: expected {expected_filename}, got {filename}"
            )
        return html.unescape(encoded).replace("\r\n", "\n").rstrip() + "\n"
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    if not DATE_RE.fullmatch(args.date):
        raise RuntimeError("--date must be YYYY-MM-DD")
    api_key = os.environ.get("ZOTERO_API_KEY", "").strip()
    user_id_raw = os.environ.get("ZOTERO_USER_ID", "").strip()
    if not api_key or not user_id_raw.isdigit():
        raise RuntimeError("ZOTERO_API_KEY and numeric ZOTERO_USER_ID are required")

    filename = f"daily-paper-recommendations-{args.date}-lingbot.md"
    client = ZoteroClient(int(user_id_raw), api_key)
    report = client.find_report(args.date)
    if not report:
        raise RuntimeError(f"Zotero does not contain a report for {args.date}")

    parent_key = report.get("key")
    if not isinstance(parent_key, str) or not parent_key:
        raise RuntimeError("Zotero report is missing its item key")
    children = client.request(
        "GET",
        f"/users/{client.user_id}/items/{parent_key}/children?itemType=note&limit=100&format=json",
    )
    if not isinstance(children, list):
        raise RuntimeError("Zotero returned an invalid report-note list")

    markdown_values: list[str] = []
    for child in children:
        note = child.get("data", {}).get("note", "")
        if not isinstance(note, str):
            continue
        markdown = extract_markdown(note, filename)
        if markdown is not None:
            markdown_values.append(markdown)
    if len(markdown_values) != 1:
        raise RuntimeError(
            f"Expected exactly one Markdown note for {args.date}, found {len(markdown_values)}"
        )

    markdown = markdown_values[0]
    headings = [(int(rank), arxiv_id) for rank, arxiv_id in ARXIV_HEADING_RE.findall(markdown)]
    if [rank for rank, _ in headings] != list(range(1, 11)):
        raise RuntimeError(f"Report Top 10 headings are incomplete: {headings}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.output_dir / filename
    report_path.write_text(markdown, encoding="utf-8")
    manifest = {
        "date": args.date,
        "filename": filename,
        "top5": [arxiv_id for _, arxiv_id in headings[:5]],
        "report_key": parent_key,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Exported {filename} with Top 5: {','.join(manifest['top5'])}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        sys.exit(1)
