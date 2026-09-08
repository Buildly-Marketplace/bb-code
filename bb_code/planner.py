from __future__ import annotations

import re
from pathlib import Path

from .model_router import AnthropicClient, OllamaClient, OpenAICompatibleClient
from .utils import plans_path, slugify

ModelClient = OllamaClient | OpenAICompatibleClient | AnthropicClient


MANDATORY_SECTIONS = (
    "Feature Summary",
    "Relevant Files",
    "Implementation Steps",
    "Risks",
    "Test Plan",
    "Documentation Updates",
    "Open Questions",
)

BUILDLY_RULES = (
    "Prefer Python-first solutions.",
    "Prefer Docker-first local setup.",
    "Do not recommend Makefiles.",
    "Use an ops/startup.sh pattern if relevant.",
    "Always include /devdocs updates.",
    "Keep tests minimal but useful.",
)


def build_planning_prompt(task_description: str, context_markdown: str) -> str:
    sections = "\n".join(f"## {section}\n" for section in MANDATORY_SECTIONS)
    rules = "\n".join(f"- {rule}" for rule in BUILDLY_RULES)
    return f"""You are bb-code v0.1, a safe local-first coding planning assistant.

Create an implementation plan only.

Hard safety constraints:
- Do not edit files.
- Do not propose autonomous execution loops.
- Do not propose background tasks, daemons, schedulers, watchers, or periodic jobs.
- Do not propose remote APIs or cloud services.
- Do not propose git operations.
- Keep the plan suitable for a simple CLI workflow.
- If a task seems to require out-of-scope behavior, call that out under Risks or Open Questions and propose the simplest safe alternative.

Task:
{task_description}

Repository context:
{context_markdown}

Mandatory output format:
- Return Markdown only.
- Use exactly these level-2 headings.
- Do not rename, combine, omit, or replace these headings.
- Put useful content under every heading.

{sections}

Buildly rules:
{rules}

Keep the plan practical, scoped, and suitable for a Python-first local development workflow.
"""


def create_plan(
    repo_root: Path,
    task_description: str,
    context_markdown: str,
    client: ModelClient,
) -> Path:
    prompt = build_planning_prompt(task_description, context_markdown)
    plan_markdown = client.generate(prompt)
    plan_markdown = normalize_plan(plan_markdown)

    target_dir = plans_path(repo_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_plan_path(target_dir, slugify(task_description))
    target.write_text(plan_markdown, encoding="utf-8")
    return target


def normalize_plan(markdown: str) -> str:
    cleaned = _strip_outer_markdown_fence(markdown.strip())
    for section in MANDATORY_SECTIONS:
        if not _has_section(cleaned, section):
            cleaned += f"\n\n## {section}\n\nTBD\n"
    return cleaned.rstrip() + "\n"


def _strip_outer_markdown_fence(markdown: str) -> str:
    match = re.match(r"(?s)^```(?:markdown|md)?\s*\n(.*)\n```\s*$", markdown)
    if match:
        return match.group(1).strip()

    fenced_blocks = re.findall(r"(?s)```(?:markdown|md)?\s*\n(.*?)\n```", markdown)
    for block in fenced_blocks:
        if _has_section(block, "Feature Summary"):
            return block.strip()
    return markdown


def _has_section(markdown: str, section: str) -> bool:
    escaped = re.escape(section)
    patterns = (
        rf"(?im)^#{{1,6}}\s+{escaped}\s*:?\s*$",
        rf"(?im)^\*\*{escaped}\s*:?\*\*\s*$",
        rf"(?im)^__{escaped}\s*:?__\s*$",
    )
    return any(re.search(pattern, markdown) for pattern in patterns)


def _unique_plan_path(directory: Path, slug: str) -> Path:
    candidate = directory / f"{slug}.md"
    if not candidate.exists():
        return candidate

    index = 2
    while True:
        candidate = directory / f"{slug}-{index}.md"
        if not candidate.exists():
            return candidate
        index += 1
