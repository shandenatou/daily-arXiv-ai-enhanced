"""Topic pre-filtering for papers before paid AI enhancement."""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, Iterable, List, Sequence, Tuple


SUPPORTED_TOPICS = (
    "world_model",
    "vla",
    "embodied",
    "reinforcement_learning",
)

TOPIC_ALIASES = {
    "world-model": "world_model",
    "world_models": "world_model",
    "rl": "reinforcement_learning",
    "reinforcement-learning": "reinforcement_learning",
}


def _compile(*patterns: str) -> Tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


TOPIC_PATTERNS = {
    "world_model": _compile(
        r"\bworld[-\s]models?\b",
        r"\b(?:latent|learned) dynamics(?: models?)?\b",
        r"\bvideo prediction\b",
        r"\benvironment models?\b",
        r"\bpredictive world\b",
        r"\bmodel[-\s]based (?:planning|control)\b",
    ),
    "vla": _compile(
        r"\bvision[-\s]language[-\s]action(?: models?)?\b",
        r"\bVLAs?\b",
        r"\bvision[-\s]language (?:robot )?polic(?:y|ies)\b",
        r"\bmultimodal (?:robot )?polic(?:y|ies)\b",
    ),
    "embodied": _compile(
        r"\bembodied\b",
        r"\brobot(?:s|ic|ics)?\b",
        r"\bhumanoids?\b",
        r"\bmanipulation\b",
        r"\bgrasp(?:ing|s|ed)?\b",
        r"\bloc(?:o)?motion\b",
        r"\bdexterous(?:ness)?\b",
        r"\bphysical intelligence\b",
        r"\bautonomous (?:driving|vehicles?)\b",
        r"\bunmanned aerial vehicles?\b",
        r"\bUAVs?\b",
    ),
    "reinforcement_learning": _compile(
        r"\breinforcement[-\s]learning\b",
        r"\bRL\b",
        r"\bRLHF\b",
        r"\bactor[-\s]critic\b",
        r"\bQ[-\s]learning\b",
        r"\bpolicy[-\s]gradients?\b",
        r"\bpolicy optimization\b",
        r"\bproximal policy optimization\b",
        r"\bsoft actor[-\s]critic\b",
        r"\btemporal[-\s]difference learning\b",
        r"\breward[-\s](?:modeling|modelling|learning)\b",
        r"\b(?:PPO|GRPO|A2C|A3C|DQN)\b",
    ),
}


def parse_topics(raw_topics: str) -> Tuple[str, ...]:
    """Parse a comma-separated topic list and validate every topic."""
    parsed = []
    for value in raw_topics.split(","):
        topic = value.strip().lower()
        if not topic:
            continue
        topic = TOPIC_ALIASES.get(topic, topic)
        if topic not in SUPPORTED_TOPICS:
            supported = ", ".join(SUPPORTED_TOPICS)
            raise ValueError(f"Unknown topic '{topic}'. Supported topics: {supported}")
        if topic not in parsed:
            parsed.append(topic)
    return tuple(parsed)


def matching_topics(paper: Dict, selected_topics: Sequence[str]) -> Tuple[str, ...]:
    """Return selected topics matched by a paper's metadata."""
    text = " ".join(
        str(paper.get(field, ""))
        for field in ("title", "summary", "comment")
    )
    categories = set(paper.get("categories") or [])
    matches = []

    for topic in selected_topics:
        # cs.RO is itself a strong signal for embodied/robotics work.
        if topic == "embodied" and "cs.RO" in categories:
            matches.append(topic)
            continue
        if any(pattern.search(text) for pattern in TOPIC_PATTERNS[topic]):
            matches.append(topic)

    return tuple(matches)


def filter_papers_by_topics(
    papers: Iterable[Dict], raw_topics: str
) -> Tuple[List[Dict], Dict[str, int]]:
    """Keep papers matching at least one configured topic.

    An empty topic setting preserves the original behavior and keeps all papers.
    The returned counts are non-exclusive because one paper may match many topics.
    """
    paper_list = list(papers)
    selected_topics = parse_topics(raw_topics)
    if not selected_topics:
        return paper_list, {}

    kept = []
    counts: Counter[str] = Counter()
    for paper in paper_list:
        matches = matching_topics(paper, selected_topics)
        if matches:
            kept.append(paper)
            counts.update(matches)

    return kept, {topic: counts[topic] for topic in selected_topics}
