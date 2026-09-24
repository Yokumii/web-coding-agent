from pathlib import Path
import hashlib

from src.orchestration.edit_context import (
    _javascript_complete_range,
    _javascript_related_function_ranges,
    ensure_edit_context,
    render_edit_context,
)


def test_javascript_context_keeps_one_hop_router_and_navigation_functions():
    lines = (
        "function handleRouteChange() { showDetailView('id'); }\n"
        "function renderGallery() { navigateToDetail('id'); }\n"
        "function navigateToDetail(id) { location.hash = id; }\n"
        "function showDetailView(id) { return id; }\n"
        "function unrelated() { return 0; }\n"
    ).splitlines(keepends=True)

    ranges = _javascript_related_function_ranges(lines, [2, 4])

    exposed = "".join(
        lines[start - 1 : end][0] for start, end in sorted(ranges)
    )
    assert "handleRouteChange" in exposed
    assert "renderGallery" in exposed
    assert "navigateToDetail" in exposed
    assert "showDetailView" in exposed
    assert "unrelated" not in exposed


def test_javascript_context_keeps_callback_chain_and_load_save_pair():
    lines = (
        "function loadState() { return localStorage.getItem('history'); }\n"
        "function saveState() { localStorage.setItem('history', '[]'); }\n"
        "function renderGallery() { addDragListeners(); }\n"
        "function addDragListeners() { card.addEventListener('drop', handleDrop); }\n"
        "function handleDrop() { saveState(); renderGallery(); }\n"
        "function unrelated() { return 0; }\n"
    ).splitlines(keepends=True)

    ranges = _javascript_related_function_ranges(lines, [3])
    exposed = "".join(lines[start - 1 : end][0] for start, end in sorted(ranges))

    assert "loadState" in exposed
    assert "saveState" in exposed
    assert "addDragListeners" in exposed
    assert "handleDrop" in exposed
    assert "unrelated" not in exposed


def test_javascript_context_expands_dangling_function_header_to_boundary():
    lines = (
        "const ready = true;\n"
        "function loadState() {\n"
        "  return localStorage.getItem('history');\n"
        "}\n"
        "function next() { return true; }\n"
    ).splitlines(keepends=True)

    assert _javascript_complete_range(lines, 1, 2) == (1, 4)
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
    assert window["file_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert window["slice_id"].startswith("frontend/app.js:245-")
    assert payload["exposure"]["ratio"] < 0.1
    rendered = render_edit_context(payload)
    assert "   245 | const line245 = 245;" in rendered
    assert "display-only" in rendered


def test_edit_context_spends_small_overage_to_close_javascript_function(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    source = (
        "function targetHandler() {\n"
        + "".join(f"  const value{index} = {index};\n" for index in range(120))
        + "  return value119;\n"
        + "}\n"
        + "function afterTarget() { return true; }\n"
    )
    (frontend / "app.js").write_text(source, encoding="utf-8")
    plan = _plan()
    plan["source_change_cone"]["hotspots"] = [
        {"path": "frontend/app.js", "matches": [{"line": 60, "anchor": "targetHandler"}]}
    ]

    payload = ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=plan,
        round_num=1,
        max_total_chars=3_500,
        max_file_chars=2_000,
        context_lines=5,
    )

    window = payload["source_windows"][0]
    assert window["start_line"] == 1
    assert window["end_line"] >= 123
    assert "return value119;\n}" in window["content"]
    assert len(window["content"]) > 2_000


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


def test_edit_context_preloads_existing_local_paths_but_skips_planned_new_page(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "app.js").write_text("const app = true;\n", encoding="utf-8")
    (frontend / "index.html").write_text("<main>Dashboard</main>\n", encoding="utf-8")
    plan = _plan()
    plan["source_change_cone"].update(
        {
            "initial_paths": ["frontend/app.js", "frontend/settings.html"],
            "local_paths": ["frontend/app.js", "frontend/index.html"],
            "planned_new_paths": ["frontend/settings.html"],
        }
    )

    payload = ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=plan,
        round_num=1,
    )

    assert payload["exposure"]["selected_paths"] == [
        "frontend/app.js",
        "frontend/index.html",
    ]


def test_behavior_only_edit_context_skips_connected_stylesheet(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "app.js").write_text("const app = true;\n", encoding="utf-8")
    (frontend / "index.html").write_text("<main>Dashboard</main>\n", encoding="utf-8")
    (frontend / "styles.css").write_text("main { color: black; }\n", encoding="utf-8")
    plan = _plan()
    plan["target_contract"] = {"requested_source_roles": ["behavior"]}
    plan["source_change_cone"].update(
        {
            "initial_paths": ["frontend/app.js"],
            "local_paths": [
                "frontend/app.js",
                "frontend/index.html",
                "frontend/styles.css",
            ],
        }
    )

    payload = ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=plan,
        round_num=1,
    )

    assert payload["exposure"]["selected_paths"] == [
        "frontend/app.js",
        "frontend/index.html",
    ]


def test_repair_context_exposes_all_current_frontend_sources(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "entry.tsx").write_text(
        "import './styles.css';\n" + "\n".join(f"const line{n} = {n};" for n in range(300)),
        encoding="utf-8",
    )
    (frontend / "styles.css").write_text("main { color: black; }\n", encoding="utf-8")
    plan = _plan()
    plan["source_change_cone"].update(
        {"initial_paths": ["frontend/entry.tsx"], "local_paths": ["frontend/entry.tsx"]}
    )

    payload = ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=plan,
        round_num=2,
        max_total_chars=20_000,
        max_file_chars=20_000,
        full_repair_context=True,
    )

    assert payload["exposure"]["selected_paths"] == [
        "frontend/entry.tsx",
        "frontend/styles.css",
    ]
    assert all(item["start_line"] == 1 for item in payload["source_windows"])
    assert all(
        item["end_line"] == len((tmp_path / item["path"]).read_text(encoding="utf-8").splitlines())
        for item in payload["source_windows"]
    )


def test_frozen_compound_context_includes_direct_markup_and_style_dependencies(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "app.js").write_text("const app = true;\n", encoding="utf-8")
    (frontend / "index.html").write_text("<main>Dashboard</main>\n", encoding="utf-8")
    (frontend / "styles.css").write_text("main { color: black; }\n", encoding="utf-8")
    plan = _plan()
    plan["target_contract"] = {"requested_source_roles": ["behavior"]}
    plan["source_change_cone"].update(
        {
            "initial_paths": ["frontend/app.js"],
            "local_paths": ["frontend/app.js"],
            "dependency_paths": ["frontend/index.html", "frontend/styles.css"],
        }
    )

    payload = ensure_edit_context(
        workdir=tmp_path,
        harness_dir=tmp_path / ".harness",
        plan=plan,
        round_num=1,
        include_dependency_paths=True,
    )

    assert payload["exposure"]["selected_paths"] == [
        "frontend/app.js",
        "frontend/index.html",
        "frontend/styles.css",
    ]
