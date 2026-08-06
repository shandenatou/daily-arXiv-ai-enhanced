#!/usr/bin/env python3
"""Generate a private, personalized daily Top 10 report with DeepSeek.

The input is the repository's AI-enhanced JSONL. The generated Markdown is
kept on the ephemeral runner and passed directly to Zotero; only SHA-256
hashes of recommended arXiv IDs are persisted for future deduplication.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


ARXIV_HEADING_RE = re.compile(
    r"^###\s+(\d+)\.\s+\[(.+?)\]\(https://arxiv\.org/abs/([^\s?#)]+)[^)]*\)\s*$",
    re.MULTILINE,
)
DEFAULT_TOPICS = "world_model,vla,embodied,reinforcement_learning"


def arxiv_hash(arxiv_id: str) -> str:
    return hashlib.sha256(arxiv_id.lower().encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL line {line_number} is invalid: {exc}") from exc
        if isinstance(value, dict) and value.get("id") and value.get("title"):
            records.append(value)
    return records


def load_dedupe(path: Path | None) -> dict:
    if not path or not path.exists():
        return {"version": 1, "recommended_id_hashes": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("dedupe state must be a JSON object")
    hashes = value.get("recommended_id_hashes", [])
    if not isinstance(hashes, list):
        raise ValueError("recommended_id_hashes must be a list")
    return {"version": 1, "recommended_id_hashes": hashes}


def relevance_score(paper: dict, topics: list[str]) -> int:
    ai = paper.get("AI") if isinstance(paper.get("AI"), dict) else {}
    text = " ".join(
        str(value)
        for value in (
            paper.get("title", ""),
            paper.get("summary", ""),
            ai.get("tldr", ""),
            ai.get("motivation", ""),
            ai.get("method", ""),
            ai.get("result", ""),
        )
    ).lower()
    aliases = {
        "world_model": ["world model", "world-model", "world action", "wam", "future prediction"],
        "vla": ["vision-language-action", "vision language action", "vla", "action chunk"],
        "embodied": ["embodied", "robot manipulation", "robotic manipulation", "contact-rich"],
        "reinforcement_learning": ["reinforcement learning", "offline rl", "grpo", "reward model"],
    }
    score = 0
    for topic in topics:
        words = aliases.get(topic, [topic.replace("_", " ")])
        score += sum(text.count(word) for word in words) * 3
    title = str(paper.get("title", "")).lower()
    score += sum(5 for word in ("world", "vla", "action", "reward", "progress", "contact") if word in title)
    return score


def compact_paper(paper: dict) -> dict:
    ai = paper.get("AI") if isinstance(paper.get("AI"), dict) else {}
    return {
        "id": str(paper.get("id", "")),
        "title": paper.get("title", ""),
        "authors": paper.get("authors", []),
        "categories": paper.get("categories", []),
        "abs": paper.get("abs") or f"https://arxiv.org/abs/{paper.get('id', '')}",
        "pdf": paper.get("pdf") or f"https://arxiv.org/pdf/{paper.get('id', '')}",
        "summary": paper.get("summary", ""),
        "ai": {
            key: ai.get(key, "")
            for key in ("tldr", "motivation", "method", "result", "conclusion")
        },
    }


def build_prompt(run_date: str, candidates: list[dict]) -> tuple[str, str]:
    system = """你是一名严格的机器人学习研究助理。你的任务是从给定论文候选中，为 LingBotVA 研究生成每日 Top 10 报告。

研究重点：世界模型/World-Action Model、VLA、动作块执行与重规划、未来预测、任务进度与视觉奖励、强化学习、接触丰富操作、物体交互表示，以及能够直接改进 RoboTwin/RobotWin 实验的工作。

要求：
1. 只能使用候选 JSON 中明确提供的信息，禁止编造机构、数字、代码开源情况或实验结论。
2. 严格选 10 篇且不得重复；前 5 篇必须最贴近研究方向。
3. 所有 arXiv 链接和标题必须与候选完全一致。
4. 默认使用简体中文，技术名词和模型名保留英文。
5. 说明每篇为何与 LingBotVA 相关，并给出可执行的实验建议；如果证据有限要明确说明。
6. 输出只能是 Markdown 报告，不要使用代码围栏，不要附加解释。
"""
    user = f"""日期：{run_date}

请严格按照以下结构输出：

# {run_date}：针对 LingBotVA 的 Top 10

> 数据状态与筛选重点（简短说明）

## 今日结论

用 2—4 段总结最重要的研究信号和优先级。

## Top Picks（详细）

### 1. [完整标题](https://arxiv.org/abs/ID)
**作者**：完整作者列表。
**重点标注**：一句话。
**动机**：...
**方法**：...
**结果**：...
**为什么贴近 LingBotVA**：...
**建议怎么用**：使用项目符号给出 3—6 条具体实验建议。
**需要注意**：证据边界或风险。

按同样格式写到第 5 篇，每篇之间用 --- 分隔。

## 另外五篇也值得读（简要）

### 6. [完整标题](https://arxiv.org/abs/ID)
**作者**：...
用 1—3 段说明方法、结果、相关性和建议。

按同样格式写到第 10 篇。

候选论文 JSON：
{json.dumps(candidates, ensure_ascii=False, separators=(',', ':'))}
"""
    return system, user


def chat_completion(system: str, user: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    api_base = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    model = os.environ.get("MODEL_NAME", "deepseek-v4-flash").strip()
    if not api_key or not api_base:
        raise RuntimeError("OPENAI_API_KEY and OPENAI_BASE_URL are required")
    if api_base.endswith("/chat/completions"):
        endpoint = api_base
    else:
        endpoint = api_base + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": int(os.environ.get("RECOMMENDATION_MAX_TOKENS", "32768")),
        "stream": False,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "daily-arxiv-recommendations/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
    content = body.get("choices", [{}])[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("LLM returned no report text")
    content = content.strip()
    if content.startswith("```markdown") and content.endswith("```"):
        content = content[len("```markdown") : -3].strip()
    return content


def validate_report(markdown: str, allowed_ids: set[str]) -> list[str]:
    matches = ARXIV_HEADING_RE.findall(markdown)
    ranks = [int(rank) for rank, _, _ in matches]
    ids = [re.sub(r"v\d+$", "", arxiv_id) for _, _, arxiv_id in matches]
    errors: list[str] = []
    if ranks != list(range(1, 11)):
        errors.append(f"expected headings 1..10, got {ranks}")
    if len(set(ids)) != 10:
        errors.append("the 10 arXiv IDs are not unique")
    unknown = [value for value in ids if value not in allowed_ids]
    if unknown:
        errors.append(f"IDs not present in candidates: {unknown}")
    if "## Top Picks（详细）" not in markdown:
        errors.append("missing detailed Top Picks section")
    if "## 另外五篇也值得读（简要）" not in markdown:
        errors.append("missing brief recommendations section")
    if errors:
        raise ValueError("; ".join(errors))
    return ids


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--dedupe", type=Path)
    parser.add_argument("--dedupe-output", type=Path, required=True)
    args = parser.parse_args()

    papers = load_jsonl(args.input)
    dedupe = load_dedupe(args.dedupe)
    old_hashes = set(dedupe["recommended_id_hashes"])
    fresh = [paper for paper in papers if arxiv_hash(str(paper["id"])) not in old_hashes]
    if len(fresh) < 10:
        raise RuntimeError(f"Only {len(fresh)} non-duplicate candidates are available; need at least 10")

    topics = [value.strip() for value in os.environ.get("TOPIC_FILTER", DEFAULT_TOPICS).split(",") if value.strip()]
    ranked = sorted(fresh, key=lambda paper: relevance_score(paper, topics), reverse=True)
    candidates = [compact_paper(paper) for paper in ranked[:80]]
    allowed = {paper["id"] for paper in candidates}
    system, user = build_prompt(args.date, candidates)

    report = chat_completion(system, user)
    try:
        selected_ids = validate_report(report, allowed)
    except ValueError as exc:
        repair = (
            user
            + "\n\n上一次输出未通过格式验证："
            + str(exc)
            + "\n请重新生成完整报告，并严格修复上述问题。"
        )
        report = chat_completion(system, repair)
        selected_ids = validate_report(report, allowed)

    args.output.write_text(report.rstrip() + "\n", encoding="utf-8")
    selection = {
        "version": 1,
        "date": args.date,
        "filename": f"daily-paper-recommendations-{args.date}-lingbot.md",
        "top10": selected_ids,
        "top5": selected_ids[:5],
        "markdown": report.rstrip() + "\n",
    }
    args.selection.write_text(json.dumps(selection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    next_hashes = sorted(old_hashes | {arxiv_hash(value) for value in selected_ids})
    args.dedupe_output.write_text(
        json.dumps(
            {
                "version": 1,
                "last_successful_date": args.date,
                "recommended_id_hashes": next_hashes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Generated and validated Top 10 for {args.date}; Top 5: {', '.join(selected_ids[:5])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
