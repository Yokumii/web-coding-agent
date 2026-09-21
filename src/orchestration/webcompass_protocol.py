"""WebCompass model-I/O serialization and strict record validation.

The public JSONL row, the content visible to the model, and the hidden reference
are deliberately separate.  Prompt text mirrors the 2026-09-04 checkout of the
official WebCompass implementation vendored in the parent data workspace.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


EDIT_TYPES = {
    "Data Table",
    "Rich Text Editor",
    "Drag & Drop Interface",
    "Tree View",
    "Real-time Dashboard",
    "Infinite Scroll",
    "Async Form Validation",
    "File Upload with Progress",
    "Parallax Scrolling",
    "Page Transitions",
    "Particle Effects",
    "Skeleton Loading",
    "Shopping Cart",
    "User Authentication",
    "Multi-step Wizard",
    "Notification Center",
}

REPAIR_TYPES = {
    "Occlusion",
    "Crowding",
    "Text Overlap",
    "Alignment",
    "Color Contrast",
    "Overflow",
    "Sizing Proportion",
    "Loss of Interactivity",
    "Semantic Error",
    "Nesting Error",
    "Missing Attributes",
}

REPAIR_TYPE_DEFINITIONS = {
    "Occlusion": "Important content is covered by another element because layering or positioning is wrong.",
    "Crowding": "Elements have insufficient spacing and the layout is cramped.",
    "Text Overlap": "Text overlaps other text or elements.",
    "Alignment": "Elements are misaligned with their grid or siblings.",
    "Color Contrast": "Foreground and background contrast is insufficient.",
    "Overflow": "Content exceeds its container without correct overflow handling.",
    "Sizing Proportion": "Element dimensions or aspect ratios are disproportionate.",
    "Loss of Interactivity": "An intended interaction is disabled or blocked.",
    "Semantic Error": "HTML elements use the wrong semantics.",
    "Nesting Error": "HTML elements are nested invalidly.",
    "Missing Attributes": "Required accessibility, functionality, or validation attributes are absent.",
}

EDIT_INSTRUCTION_PROMPT = """
You are an expert frontend developer. Your task is to edit the provided web code based on the given instructions.
You will receive the current code and a set of editing instructions.
**Output Format Requirements:**
- Use search/replace blocks to indicate modifications
- Each block must be wrapped in `<search_replace path="..."></search_replace>` tags
- The `path` attribute must specify the relative file path (e.g., "index.html", "resources/style.css")
- Each block must contain one `<search>` and one `<replace>`

Return XML format with the following structure:
<search_replace path="path/to/file">
<search>
exact text to find in the original file
</search>
<replace>
replacement text with the modification applied
</replace>
</search_replace>

If you want to create additional files, please follow this structure:
<search_replace path="path/to/new_file">
<search></search>
<replace>
complete code for the new file
</replace>
</search_replace>
The search block for new files should be empty.
Important:
- The <search> block must contain the EXACT text from the original file (including whitespace and indentation).
- The <replace> block contains the modified code.
- One <search_replace></search_replace> block can only contain one pair of <search> and <replace>.
- The <search_replace> block must contain both <search></search> and <replace></replace> blocks.
- You can include multiple <search_replace></search_replace> blocks if you need to modify multiple locations, you can also modify multiple files.
- You must complete the task in single response, do not ask for clarification.
"""

REPAIR_INSTRUCTION_PROMPT = """
You are an expert frontend developer. Your task is to repair the provided web code based on the given defect types.
You will receive the current code with a set of defect types to fix.
Here are the issue types and explanations (the code may not contain all of these issue):

- Occlusion: Elements are incorrectly layered causing important content to be covered by other elements due to improper z-index values or positioning.

- Crowding: Elements are too close together due to missing or insufficient spacing such as margins or padding making the layout cramped.

- Text Overlap: Text content overlaps with other text or elements due to insufficient container size incorrect positioning or improper line-height settings.

- Alignment: Elements are not properly aligned with the grid system or their sibling elements creating visual inconsistency in the layout.

- Color Contrast: Insufficient contrast between text and background colors makes content difficult or impossible to read affecting accessibility.

- Overflow: Content exceeds its container boundaries without proper overflow handling breaking the layout structure.

- Sizing Proportion: Elements have incorrect dimensions or aspect ratios that distort their appearance or make them disproportionate to the layout.

- Loss of Interactivity: Interactive elements are disabled or blocked from user interaction through disabled attributes or CSS properties like pointer-events none.

- Semantic Error: HTML elements are used incorrectly replacing semantic tags with generic divs or spans reducing code quality and accessibility.

- Nesting Error: HTML elements are nested in invalid ways that violate HTML specifications such as block elements inside inline elements.

- Missing Attributes: Required or important attributes are missing from elements affecting accessibility functionality or validation such as alt attributes or aria-labels.

**Output Format Requirements:**
- Use search/replace blocks to indicate modifications
- Each block must be wrapped in `<search_replace path="..."></search_replace>` tags
- The `path` attribute must specify the relative file path (e.g., "index.html", "resources/style.css")
- Each block must contain one `<search>` and one `<replace>`

Return XML format with the following structure:
<search_replace path="path/to/file">
<search>
exact text to find in the original file
</search>
<replace>
replacement text with the modification applied
</replace>
</search_replace>

Important:
- The <search> block must contain the EXACT text from the original file (including whitespace and indentation).
- The <replace> block contains the modified code.
- One <search_replace></search_replace> block can only contain one pair of <search> and <replace>.
- The <search_replace> block must contain both <search></search> and <replace></replace> blocks.
- You can include multiple <search_replace></search_replace> blocks if you need to modify multiple locations, you can also modify multiple files.
- You must complete the task in single response, do not ask for clarification.
"""

TEXT_GENERATION_PROMPT = '''You are a highly skilled professional front-end engineer.

Your task: Based on the web design document below, generate a complete runnable web project repository.

Hard output contract (MUST follow):
1) Your entire response MUST be pure Markdown text.
2) ABSOLUTELY NO explanations, no extra commentary, no preface, no trailing notes.
3) Every file MUST be emitted using the following format:

# path/to/file.ext
```ext
<full file content>
```

4) The heading line MUST start with '# ' followed by the file path (relative path).
5) The code fence language MUST match the file type when possible (html/css/js/json/md/txt).
6) Include all necessary files (HTML, CSS, JS, etc.) so the project can run.
7) Do NOT nest triple backticks inside code blocks.
8) Use HTML, CSS, and JavaScript only. No frameworks or build tools required.

Few-shot examples:

# index.html
```html
<!doctype html>
<html>
    <head>
        <meta charset="utf-8" />
        <title>Demo</title>
        <link rel="stylesheet" href="styles.css" />
    </head>
    <body>
        Hello
        <script type="module" src="main.js"></script>
    </body>
</html>
```

# styles.css
```css
body {{ font-family: system-ui; }}
```

# main.js
```js
console.log('ok')
```

Web design document:
---
{document}
---
'''

IMAGE_GENERATION_PROMPT = '''You are a highly skilled professional front-end engineer.

Your task: Based on the web design document and the reference screenshots provided, generate a complete runnable web project repository.
The screenshots represent the target UI and function. The generated site should match the screenshots as closely as possible.

Hard output contract (MUST follow):
1) Your entire response MUST be pure Markdown text.
2) ABSOLUTELY NO explanations, no extra commentary, no preface, no trailing notes.
3) Every file MUST be emitted using the following format:

# path/to/file.ext
```ext
<full file content>
```

4) The heading line MUST start with '# ' followed by the file path (relative path).
5) The code fence language MUST match the file type when possible (html/css/js/json/md/txt).
6) Include all necessary files (HTML, CSS, JS, etc.) so the project can run.
7) Do NOT nest triple backticks inside code blocks.

Startup requirements:
- MUST include a README.md with the simplest way to run locally.
- MUST be runnable via a static server (no backend). Prefer Vite or plain static files.

Web design document:
---
{document}
---
'''


def _code_context(src_code: list[dict[str, str]]) -> str:
    body = ""
    for item in src_code:
        body += f'<file path="{item["path"]}">\n{item["code"]}\n</file>\n'
    return body


def construct_edit_text(record: dict[str, Any]) -> str:
    descriptions = record["description"]
    task_description = "\n".join(
        f"Task{index}- {item['task_type']}: {item['description']}"
        for index, item in enumerate(descriptions)
    )
    return (
        EDIT_INSTRUCTION_PROMPT
        + "\n## Task Description\n"
        + task_description
        + "\n## Source Code\n"
        + "The following is the current code that needs to be modified:\n\n"
        + "<code_context>\n"
        + _code_context(record["src_code"])
        + "</code_context>\n"
    )


def construct_repair_text(record: dict[str, Any]) -> str:
    issue_count = len(record["description"])
    return (
        REPAIR_INSTRUCTION_PROMPT
        + "\n"
        + f"You have only {issue_count} issues to fix, and you can not fix more than {issue_count} issues."
        + "\n## Source Code\n"
        + "The following is the current code that needs to be modified:\n\n"
        + "<code_context>\n"
        + _code_context(record["src_code"])
        + "</code_context>\n"
    )


def image_generation_document(instance_id: str, image_paths: list[str]) -> str:
    names = "\n".join(f"- {Path(path).name}" for path in sorted(image_paths))
    return (
        "# Goal\n"
        "Generate a runnable website that matches the provided reference screenshots as closely as possible.\n\n"
        f"# Instance\n{instance_id}\n\n"
        "# Reference screenshots (sorted)\n"
        f"{names}\n\n"
        "# Instructions\n"
        "Study the reference screenshots carefully and implement:\n"
        "- The exact visual layout, colors, typography, and spacing shown\n"
        "- All UI components visible in the screenshots\n"
        "- Any interactive elements implied by the design (buttons, forms, navigation, etc.)\n"
        "- Responsive behavior if multiple viewport sizes are shown\n"
    )


def markdown_repository(code: list[dict[str, str]]) -> str:
    language = {
        ".html": "html", ".htm": "html", ".css": "css", ".js": "js",
        ".json": "json", ".md": "md", ".txt": "txt", ".svg": "xml",
    }
    blocks = []
    for item in sorted(code, key=lambda value: value["path"]):
        path = item["path"]
        blocks.append(
            f"# {path}\n```{language.get(Path(path).suffix.lower(), '')}\n"
            f"{item['code']}\n```"
        )
    return "\n\n".join(blocks)


def parse_markdown_repository(text: str) -> list[dict[str, str]]:
    pattern = re.compile(r"^# ([^\n]+)\n```[^\n]*\n(.*?)\n```(?=\n\n# |\Z)", re.MULTILINE | re.DOTALL)
    return [
        {"path": match.group(1), "code": match.group(2)}
        for match in pattern.finditer(text)
    ]


def official_patches(patches: list[dict[str, str]]) -> list[dict[str, str]]:
    """Convert native create_file atoms to WebCompass's empty-search encoding."""
    result = []
    for patch in patches:
        if patch.get("operation", "replace") == "create_file":
            result.append({
                "path": patch["path"],
                "search": "",
                "replace": patch["content"],
            })
        else:
            result.append({
                "path": patch["path"],
                "search": patch["search"],
                "replace": patch["replace"],
            })
    return result


def search_replace_xml(patches: list[dict[str, str]]) -> str:
    blocks = []
    for patch in official_patches(patches):
        blocks.append(
            f'<search_replace path="{patch["path"]}">\n'
            f'<search>\n{patch["search"]}\n</search>\n'
            f'<replace>\n{patch["replace"]}\n</replace>\n'
            "</search_replace>"
        )
    return "\n\n".join(blocks)


def validate_generation_record(record: dict[str, Any], *, modality: str) -> None:
    required = {"repo", "instance_id", "base_commit", "problem_statement", "meta", "working_dir"}
    if modality == "text":
        required.add("instruction")
    missing = required - record.keys()
    if missing:
        raise ValueError(f"generation record missing fields: {sorted(missing)}")
    if modality == "image" and "instruction" in record:
        raise ValueError("official image-generation rows do not carry an instruction field")
    if not isinstance(record["problem_statement"], list):
        raise ValueError("problem_statement must be a checklist list")


def validate_edit_repair_record(record: dict[str, Any]) -> None:
    required = {
        "instance_id", "task", "task_type", "difficulty", "description",
        "src_code", "dst_code", "src_screenshot", "dst_screenshot",
        "label_modified_files", "resources",
    }
    missing = required - record.keys()
    if missing:
        raise ValueError(f"edit/repair record missing fields: {sorted(missing)}")
    task = record["task"]
    if task not in {"edit", "repair"}:
        raise ValueError(f"unsupported WebCompass task: {task!r}")
    descriptions = record["description"]
    if not 4 <= len(descriptions) <= 12:
        raise ValueError("official compound task count must be between 4 and 12")
    if len(record["task_type"]) != len(descriptions):
        raise ValueError("task_type and description lengths differ")
    allowed = EDIT_TYPES if task == "edit" else REPAIR_TYPES
    unknown = sorted(set(record["task_type"]) - allowed)
    if unknown:
        raise ValueError(f"task types outside the official taxonomy: {unknown}")
    if [item.get("task_type") for item in descriptions] != record["task_type"]:
        raise ValueError("description task_type order does not match task_type")


def model_io_view(
    record: dict[str, Any], *, task_name: str, image_paths: list[str] | None = None,
    target_image_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Build an audit view; this is not an additional official dataset schema."""
    images = image_paths or []
    target_images = target_image_paths or []
    def base(text: str, visible_images: list[str]) -> dict[str, Any]:
        return {
            "instance_id": record["instance_id"],
            "task": task_name,
            "model_visible_text": text,
            "model_visible_images": visible_images,
            "model_visible_content": [{"type": "text", "text": text}],
        }

    def append_images(
        view: dict[str, Any], *, heading: str | None, label: str | None,
        paths: list[str],
    ) -> None:
        if not paths:
            return
        if heading:
            view["model_visible_content"].append({"type": "text", "text": heading})
        for path in paths:
            if label:
                view["model_visible_content"].append({
                    "type": "text", "text": f"\n[{label}: {Path(path).name}]"
                })
            # The official implementation sends a base64 data URL.  The audit
            # view retains the source path so the payload stays inspectable.
            view["model_visible_content"].append({
                "type": "image_url", "image_url": {"path": path}
            })

    if task_name == "text-generation":
        validate_generation_record(record, modality="text")
        return base(TEXT_GENERATION_PROMPT.format(document=record["instruction"]), [])
    if task_name == "image-generation":
        validate_generation_record(record, modality="image")
        document = image_generation_document(record["instance_id"], images)
        view = base(IMAGE_GENERATION_PROMPT.format(document=document), images)
        append_images(view, heading=None, label=None, paths=images)
        return view
    validate_edit_repair_record(record)
    if task_name in {"text-editing", "image-editing"}:
        if record["task"] != "edit":
            raise ValueError("editing view requires task=edit")
        visible = images if task_name == "image-editing" else []
        view = base(construct_edit_text(record), visible)
        if task_name == "image-editing":
            append_images(
                view,
                heading="\n## Current State Screenshots\nThe following screenshots show the current state:\n\n",
                label="Current Screenshot",
                paths=images,
            )
        return view
    if task_name in {"diagnostic-repair", "visual-diagnostic-repair"}:
        if record["task"] != "repair":
            raise ValueError("repair view requires task=repair")
        visible = [*images, *target_images] if task_name == "visual-diagnostic-repair" else []
        view = base(construct_repair_text(record), visible)
        if task_name == "visual-diagnostic-repair":
            append_images(
                view,
                heading="\n## Current State Screenshots\nThe following screenshots show the current state:\n\n",
                label="Current Screenshot",
                paths=images,
            )
            append_images(
                view,
                heading="\n## Target State Screenshots\nThe following screenshots show the expected result:\n\n",
                label="Target Screenshot",
                paths=target_images,
            )
        return view
    raise ValueError(f"unsupported task view: {task_name}")
