from __future__ import annotations

from bb_code.planner import MANDATORY_SECTIONS, build_planning_prompt, normalize_plan


def test_prompt_includes_task_context_and_buildly_rules() -> None:
    prompt = build_planning_prompt("Add login system", "# Context")

    assert "Add login system" in prompt
    assert "# Context" in prompt
    assert "Do not propose background tasks" in prompt
    assert "Use exactly these level-2 headings" in prompt
    assert "## Feature Summary" in prompt
    assert "Do not recommend Makefiles" in prompt
    assert "Always include /devdocs updates" in prompt


def test_normalize_plan_adds_missing_sections() -> None:
    plan = normalize_plan("## Feature Summary\n\nDo it.")

    for section in MANDATORY_SECTIONS:
        assert f"## {section}" in plan


def test_normalize_plan_accepts_bold_section_labels() -> None:
    plan = normalize_plan(
        "\n\n".join(f"**{section}:**\n\nContent." for section in MANDATORY_SECTIONS)
    )

    assert "TBD" not in plan


def test_normalize_plan_strips_outer_markdown_fence() -> None:
    body = "\n\n".join(f"## {section}\n\nContent." for section in MANDATORY_SECTIONS)

    plan = normalize_plan(f"```markdown\n{body}\n```")

    assert plan.startswith("## Feature Summary")
    assert not plan.startswith("```")
    assert "TBD" not in plan


def test_normalize_plan_extracts_fenced_plan_after_intro() -> None:
    body = "\n\n".join(f"## {section}\n\nContent." for section in MANDATORY_SECTIONS)

    plan = normalize_plan(f"Here is the plan:\n\n```markdown\n{body}\n```")

    assert plan.startswith("## Feature Summary")
    assert "Here is the plan" not in plan
    assert "TBD" not in plan
