from pathlib import Path

from src.orchestration.edit_context import ensure_edit_context
from src.orchestration.minimal_path_guidance import MinimalPathPolicy


def _plan() -> dict:
    return {
        "schema_version": "minimal-path-v4",
        "owner": "harness",
        "round": 1,
        "mode": "generate",
        "route_scope": {"off_target_paths": [], "cross_route_shared_paths": []},
        "source_change_cone": {
            "local_paths": ["frontend/app.js"],
            "initial_paths": ["frontend/app.js"],
            "dependency_paths": [],
            "protected_paths": [],
            "dependency_edges": [],
            "guarded_shared_regions": [],
            "hotspots": [
                {
                    "path": "frontend/app.js",
                    "matches": [{"line": 250, "anchor": "targetHandler"}],
                }
            ],
        },
        "budgets": {"max_patch_lines": 20, "max_touched_files": 1},
    }


def test_edit_context_selects_hotspot_window_and_measures_exposure(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    source = "".join(f"const line{index} = {index};\n" for index in range(1, 501))
    (frontend / "app.js").write_text(source, encoding="utf-8")

    payload = ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=_plan(),
        round_num=1,
        max_total_chars=2_000,
        max_file_chars=2_000,
        context_lines=5,
    )

    window = payload["source_windows"][0]
    assert window["start_line"] == 245
    assert "const line250 = 250;" in window["content"]
    assert payload["exposure"]["ratio"] < 0.1


def test_preloaded_window_allows_exact_patch_but_not_unseen_source(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    source = "".join(f"const line{index} = {index};\n" for index in range(1, 501))
    (frontend / "app.js").write_text(source, encoding="utf-8")
    plan = _plan()
    ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=plan,
        round_num=1,
        max_total_chars=2_000,
        max_file_chars=2_000,
        context_lines=5,
    )
    policy = MinimalPathPolicy(tmp_path, plan)

    assert policy.check(
        "apply_patch",
        {
            "path": "frontend/app.js",
            "old_string": "const line250 = 250;",
            "new_string": "const line250 = 251;",
        },
    ) is None
    denial = policy.check(
        "apply_patch",
        {
            "path": "frontend/app.js",
            "old_string": "const line10 = 10;",
            "new_string": "const line10 = 11;",
        },
    )
    assert denial is not None
    assert "preloaded" in denial.lower()

    policy.observe_result(
        "read_file", {"path": "frontend/app.js"}, ok=True, output=source
    )
    assert policy.check(
        "apply_patch",
        {
            "path": "frontend/app.js",
            "old_string": "const line10 = 10;",
            "new_string": "const line10 = 11;",
        },
    ) is None


def test_edit_context_includes_structural_outline_when_new_selector_has_no_match(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    html = "\n".join(
        ["<!doctype html>", "<style>body { color: black; }</style>"]
        + [f"<p>Filler {index}</p>" for index in range(200)]
        + ["<section class='visitor-info'>", "<h2>Public Transit</h2>", "</section>"]
    )
    (frontend / "index.html").write_text(html, encoding="utf-8")
    plan = _plan()
    plan["source_change_cone"]["local_paths"] = ["frontend/index.html"]
    plan["source_change_cone"]["initial_paths"] = ["frontend/index.html"]
    plan["source_change_cone"]["hotspots"] = []

    payload = ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=plan,
        round_num=1,
        max_total_chars=1_000,
        max_file_chars=500,
        source_anchors=["Public Transit"],
    )

    entries = payload["source_outlines"][0]["entries"]
    assert any(item["content"] == "<h2>Public Transit</h2>" for item in entries)
    assert payload["rendered_outline_paths"] == []
    assert payload["exposure"]["outline_chars"] == 0
    assert payload["exposure"]["exposed_source_chars"] == payload["exposure"]["window_chars"]
    assert "<h2>Public Transit</h2>" in payload["source_windows"][0]["content"]
    assert payload["exposure"]["full_source_chars"] > payload["exposure"]["exposed_source_chars"]
