from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.generator import (
    _MAX_ATOMIC_SEMANTIC_ATTEMPTS,
    _control_topology_invariants,
    _atomic_executor_eligible,
    _build_generator_prompt,
    _compact_browser_error,
    _checkpoint_interrupted_model_work,
    _recover_deferred_model_patches,
    _describe_failures,
    _declared_source_functions,
    _is_scope_contract_only_repair,
    _is_harness_checkpoint_for_round,
    _normalize_atomic_patch_response,
    _normalize_atomic_new_files,
    _atomic_new_path_allowed,
    _atomic_transaction_sort_key,
    _normalize_atomic_operations,
    _apply_sha_line_operations,
    _render_control_topology_directives,
    _render_failed_action_directives,
    _recent_repair_runtime_errors,
    _validate_generator_commits,
    _validate_javascript_syntax,
    _validate_no_external_runtime_dependencies,
    _validate_minimal_path_final_diff,
    _validate_repair_scope,
    _validate_generator_runnable_files,
    _trace_confirms_commit,
    _last_replayable_atomic_candidate,
    _unreferenced_near_duplicate_functions,
    _uniquify_first_exact_patch,
    _trace_has_successful_validation,
    _trace_written_frontend_paths,
    run_generator,
)
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.orchestration.minimal_path_guidance import MinimalPathPolicy
from src.prompts.generator import GENERATOR_SYSTEM_PROMPT
from src.agents._shared import expose_local_claude_skills


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_atomic_edit_generation_has_no_internal_candidate_retry():
    assert _MAX_ATOMIC_SEMANTIC_ATTEMPTS == 1


def test_frozen_exact_patch_adds_context_around_repeated_search():
    source = "<main><section></section><section></section></main>"

    old_text, new_text = _uniquify_first_exact_patch(
        source, "<section></section>", "<section><button>Open</button></section>"
    )

    assert source.count(old_text) == 1
    updated = source.replace(old_text, new_text, 1)
    assert updated == (
        "<main><section><button>Open</button></section><section></section></main>"
    )


def test_unplanned_new_path_only_blocks_when_minimality_is_enabled():
    relaxed = HarnessConfig(minimality_guard_enabled=False)
    strict = HarnessConfig(minimality_guard_enabled=True)

    assert _atomic_new_path_allowed(relaxed, "frontend/archive.html", set()) is True
    assert _atomic_new_path_allowed(strict, "frontend/archive.html", set()) is False
    assert _atomic_new_path_allowed(
        strict, "frontend/archive.html", {"frontend/archive.html"}
    ) is True


def test_atomic_copy_runs_before_mutating_its_source():
    transactions = [
        ("line_edits", "frontend/gallery.html", []),
        ("copy_from", "frontend/scrapbook.html", {"source":"frontend/gallery.html"}),
    ]
    transactions.sort(
        key=lambda item: _atomic_transaction_sort_key(
            item, {"frontend/gallery.html"}
        )
    )

    assert [item[0] for item in transactions] == ["copy_from", "line_edits"]


def _write_generator_context(file_comm: FileComm) -> None:
    file_comm.write_sprint_plan(
        {
            "total_sprints": 2,
            "sprints": [
                {
                    "number": 1,
                    "title": "Core counter",
                    "goal": "Ship the primary counter flow.",
                    "feature_ids": ["F001"],
                    "deliverables": ["Visible counter UI."],
                    "exit_criteria": ["Counter increments correctly."],
                },
                {
                    "number": 2,
                    "title": "Refine interactions",
                    "goal": "Repair and polish the interaction flow.",
                    "feature_ids": ["F002"],
                    "deliverables": ["Repair evaluator findings."],
                    "exit_criteria": ["Reset works correctly."],
                },
            ],
        }
    )
    file_comm.write_accepted_sprints(
        {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 0,
        }
    )


def test_expose_local_skills_replaces_external_symlink_with_copy(tmp_path: Path):
    workdir = tmp_path / "workdir"
    source = tmp_path / "repo-skills"
    (source / "ui-skill").mkdir(parents=True)
    (source / "ui-skill" / "SKILL.md").write_text("# skill\n")
    (workdir / ".claude").mkdir(parents=True)
    (workdir / ".claude" / "skills").symlink_to(source, target_is_directory=True)

    expose_local_claude_skills(workdir, source)

    exposed = workdir / ".claude" / "skills"
    assert not exposed.is_symlink()
    assert (exposed / "ui-skill" / "SKILL.md").read_text() == "# skill\n"


def test_trace_confirms_only_the_exact_recorded_commit(tmp_path: Path):
    trace = tmp_path / "generator.jsonl"
    trace.write_text(
        '{"event":"tool","name":"run_command","output":"[main abc1234] feat(form): validate contact form\\n"}\n',
        encoding="utf-8",
    )
    assert _trace_confirms_commit(trace, "abc1234def567", "feat(form): validate contact form")
    assert not _trace_confirms_commit(trace, "abc1234def567", "feat(form): unrelated")
    assert not _trace_confirms_commit(trace, "def9876", "feat(form): validate contact form")


def test_javascript_syntax_check_rejects_broken_esm_js_without_package_type(tmp_path: Path):
    (tmp_path / "app.js").write_text(
        "import { value } from './data.js';\nconst state = {};\n};\n"
    )

    ok, output = _validate_javascript_syntax(tmp_path, "app.js")

    assert ok is False
    assert "SyntaxError" in output


def test_atomic_rejection_feedback_explains_duplicate_declaration_boundary():
    from src.agents.generator import _atomic_rejection_feedback

    feedback = _atomic_rejection_feedback(
        RuntimeError("SyntaxError: Identifier 'saveState' has already been declared")
    )

    assert "multiple saveState declarations in one scope" in feedback
    assert "deleting a closing brace" in feedback
    assert "complete original boundary" in feedback


def test_last_replayable_atomic_candidate_requires_a_later_local_rejection(tmp_path: Path):
    trace = tmp_path / "generator.jsonl"
    trace.write_text(
        '\n'.join(
            [
                json.dumps({"event": "assistant_response", "content": '{"operations":[1]}'}),
                json.dumps({"event": "run_error", "error": "old guard rejected"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _last_replayable_atomic_candidate(trace) == '{"operations":[1]}'
    with trace.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "event": "tool",
                    "name": "run_command",
                    "ok": True,
                    "output": "committed",
                }
            )
            + "\n"
        )
    assert _last_replayable_atomic_candidate(trace) == ""


def test_final_diff_guard_rejects_indirect_protected_page_change(tmp_path: Path):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "catalog.js").write_text("catalog before\n")
    (frontend / "settings.js").write_text("settings before\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed"],
        cwd=frontend, check=True, capture_output=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, text=True,
        check=True, capture_output=True,
    ).stdout.strip()
    (frontend / "catalog.js").write_text("catalog after\n")
    (frontend / "settings.js").write_text("settings after\n")
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "feat: edit"],
        cwd=frontend, check=True, capture_output=True,
    )
    plan = {
        "schema_version": "minimal-path-plan-v1",
        "round": 1,
        "source_change_cone": {
            "initial_paths": ["frontend/catalog.js"],
            "local_paths": ["frontend/catalog.js"],
            "dependency_paths": [],
            "protected_paths": ["frontend/settings.js"],
            "dependency_edges": [],
        },
        "route_scope": {
            "cross_route_shared_paths": [],
            "off_target_paths": ["frontend/settings.js"],
        },
        "budgets": {"max_patch_lines": 20, "max_touched_files": 2},
    }
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.touched_paths.add("frontend/catalog.js")

    error = _validate_minimal_path_final_diff(frontend, baseline, policy)

    assert error is not None
    assert "frontend/settings.js" in error
    assert "protected multi-page source" in error


def test_final_diff_guard_allows_only_guarded_region_in_shared_file(tmp_path: Path):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    shared_before = (
        "/* --- CATALOG MODULE --- */\n"
        "const Catalog = { render: () => 'old catalog' };\n\n"
        "/* --- SETTINGS MODULE --- */\n"
        "const Settings = { render: () => 'old settings' };\n"
    )
    (frontend / "app.js").write_text(shared_before)
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed"],
        cwd=frontend, check=True, capture_output=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, text=True,
        check=True, capture_output=True,
    ).stdout.strip()
    plan = {
        "schema_version": "minimal-path-plan-v3",
        "round": 1,
        "source_change_cone": {
            "initial_paths": ["frontend/app.js"],
            "local_paths": ["frontend/app.js"],
            "dependency_paths": [],
            "protected_paths": [],
            "dependency_edges": [],
            "guarded_shared_regions": [
                {
                    "path": "frontend/app.js",
                    "route": "/catalog.html",
                    "symbol": "Catalog",
                    "kind": "object",
                    "start_line": 1,
                    "end_line": 2,
                }
            ],
        },
        "route_scope": {
            "cross_route_shared_paths": ["frontend/app.js"],
            "off_target_paths": [],
        },
        "budgets": {"max_patch_lines": 20, "max_touched_files": 1},
    }
    policy = MinimalPathPolicy.from_plan(tmp_path, plan)
    policy.touched_paths.add("frontend/app.js")

    (frontend / "app.js").write_text(
        shared_before.replace("old catalog", "new catalog")
    )
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "catalog"],
        cwd=frontend, check=True, capture_output=True,
    )
    assert _validate_minimal_path_final_diff(frontend, baseline, policy) is None

    (frontend / "app.js").write_text(
        (frontend / "app.js").read_text().replace("old settings", "changed settings")
    )
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "settings"],
        cwd=frontend, check=True, capture_output=True,
    )
    error = _validate_minimal_path_final_diff(frontend, baseline, policy)
    assert error is not None and "outside the guarded target-route region" in error


def test_final_diff_guard_rejects_uncontracted_asset_change(tmp_path: Path):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "catalog.js").write_text("before\n")
    (frontend / "hero.png").write_bytes(b"before")
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "seed"],
        cwd=frontend, check=True, capture_output=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, text=True,
        check=True, capture_output=True,
    ).stdout.strip()
    (frontend / "hero.png").write_bytes(b"after")
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "feat: image"],
        cwd=frontend, check=True, capture_output=True,
    )
    plan = {
        "schema_version": "minimal-path-plan-v1", "round": 1,
        "source_change_cone": {
            "initial_paths": ["frontend/catalog.js"],
            "local_paths": ["frontend/catalog.js"], "dependency_paths": [],
            "protected_paths": [], "dependency_edges": [],
        },
        "route_scope": {"cross_route_shared_paths": [], "off_target_paths": []},
        "budgets": {"max_patch_lines": 20, "max_touched_files": 2},
    }

    error = _validate_minimal_path_final_diff(
        frontend, baseline, MinimalPathPolicy.from_plan(tmp_path, plan)
    )

    assert error is not None and "hero.png" in error
    assert "resource-manifest contract" in error


def test_trace_written_frontend_paths_requires_successful_explicit_source_writes(tmp_path: Path):
    trace = tmp_path / "generator.jsonl"
    trace.write_text(
        '\n'.join([
            '{"event":"assistant","message":{"tool_calls":[{"function":{"name":"write_file","arguments":"{\\"path\\": \\"frontend/main.js\\"}"}}]}}',
            '{"event":"tool","name":"write_file","ok":true,"output":"wrote frontend/main.js"}',
            '{"event":"assistant","message":{"tool_calls":[{"function":{"name":"apply_patch","arguments":"{\\"path\\": \\"frontend/styles.css\\"}"}}]}}',
            '{"event":"tool","name":"apply_patch","ok":false,"output":"not found"}',
            '{"event":"assistant","message":{"tool_calls":[{"function":{"name":"write_file","arguments":"{\\"path\\": \\".harness/progress.md\\"}"}}]}}',
            '{"event":"tool","name":"write_file","ok":true,"output":"wrote .harness/progress.md"}',
        ]) + '\n', encoding="utf-8",
    )
    assert _trace_written_frontend_paths(trace) == {"main.js"}


def test_trace_validation_requires_a_successful_model_validation_command(tmp_path: Path):
    trace = tmp_path / "generator.jsonl"
    trace.write_text(
        '\n'.join([
            '{"event":"assistant","message":{"tool_calls":[{"function":{"name":"run_command","arguments":"{\\"command\\": \\"git diff --check\\"}"}}]}}',
            '{"event":"tool","name":"run_command","ok":false,"output":"failed"}',
            '{"event":"assistant","message":{"tool_calls":[{"function":{"name":"run_command","arguments":"{\\"command\\": \\"node --check main.js\\"}"}}]}}',
            '{"event":"tool","name":"run_command","ok":true,"output":""}',
        ]) + '\n', encoding="utf-8",
    )
    assert _trace_has_successful_validation(trace)


def test_interrupted_checkpoint_requires_trace_recorded_validation(tmp_path: Path):
    import json
    import subprocess

    workdir = tmp_path
    frontend = workdir / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=frontend, check=True)
    (frontend / "main.js").write_text("const value = 1;\n")
    subprocess.run(["git", "add", "main.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "chore: baseline"], cwd=frontend, check=True, capture_output=True)
    (frontend / "main.js").write_text("const value = 2;\n")
    (workdir / "seed_manifest.json").write_text("{}\n")
    file_comm = FileComm(workdir / ".harness")
    file_comm.dir.mkdir(exist_ok=True)
    (file_comm.dir / "edit_scope_round_1.json").write_text(
        json.dumps({"allowed_root_keys": [], "allow_new_roots": False})
    )
    trace = file_comm.dir / "traces" / "generator_round_1.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("\n".join([
        json.dumps({"event": "assistant", "message": {"tool_calls": [{"function": {"name": "write_file", "arguments": json.dumps({"path": "frontend/main.js"})}}]}}),
        json.dumps({"event": "tool", "name": "write_file", "ok": True, "output": "wrote"}),
    ]) + "\n")

    assert _checkpoint_interrupted_model_work(frontend, file_comm, workdir, 1, "generate") is None
    assert subprocess.run(["git", "status", "--porcelain"], cwd=frontend, text=True, capture_output=True, check=True).stdout == " M main.js\n"


def test_paid_resume_calls_are_disabled_by_default():
    assert HarnessConfig().allow_paid_resume_call is False


def test_recovery_replays_only_model_patch_deferred_by_validation_gate(
    tmp_path: Path,
):
    import json
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=frontend, check=True
    )
    (frontend / "app.js").write_text("const route = 'hash';\n", encoding="utf-8")
    (frontend / "index.html").write_text(
        '<a href="#/settings">Settings</a>\n', encoding="utf-8"
    )
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: baseline"],
        cwd=frontend, check=True, capture_output=True,
    )
    (frontend / "app.js").write_text("const route = 'physical';\n", encoding="utf-8")
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.dir.mkdir(exist_ok=True)
    (file_comm.dir / "edit_scope_round_1.json").write_text(
        json.dumps({"allowed_root_keys": [], "allow_new_roots": True}),
        encoding="utf-8",
    )
    (file_comm.dir / "edit_context_round_1.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-context-v1",
                "source_windows": [
                    {"path": "frontend/app.js", "content": "const route = 'hash';\n"},
                    {
                        "path": "frontend/index.html",
                        "content": '<a href="#/settings">Settings</a>\n',
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (file_comm.dir / "minimal_path_plan_round_1.json").write_text(
        json.dumps(
            {
                "schema_version": "minimal-path-plan-v3",
                "round": 1,
                "source_change_cone": {
                    "initial_paths": ["frontend/app.js"],
                    "local_paths": ["frontend/app.js", "frontend/index.html"],
                    "dependency_paths": [],
                    "planned_new_paths": [],
                    "protected_paths": [],
                    "dependency_edges": [
                        {"from": "frontend/app.js", "to": "frontend/index.html"}
                    ],
                    "guarded_shared_regions": [],
                },
                "route_scope": {
                    "cross_route_shared_paths": [],
                    "off_target_paths": [],
                },
                "budgets": {"max_patch_lines": 20, "max_touched_files": 2},
            }
        ),
        encoding="utf-8",
    )
    trace = file_comm.dir / "traces" / "generator_round_1.jsonl"
    trace.parent.mkdir(parents=True)
    deferred_args = {
        "path": "frontend/index.html",
        "old_text": '<a href="#/settings">Settings</a>',
        "new_text": '<a href="/settings.html">Settings</a>',
    }
    trace.write_text(
        "\n".join(
            [
                json.dumps({"event": "run_start", "model": "qwen-test"}),
                json.dumps(
                    {
                        "event": "assistant",
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": json.dumps(
                                            {
                                                "path": "frontend/app.js",
                                                "old_text": "const route = 'hash';",
                                                "new_text": "const route = 'physical';",
                                            }
                                        ),
                                    }
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {"event": "tool", "name": "apply_patch", "ok": True, "output": "patched"}
                ),
                json.dumps(
                    {
                        "event": "assistant",
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "apply_patch",
                                        "arguments": json.dumps(deferred_args),
                                    }
                                }
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "tool",
                        "name": "apply_patch",
                        "ok": False,
                        "output": (
                            "A post-mutation validation attempt is required before the "
                            "harness widens from frontend/app.js to its dependency "
                            "frontend/index.html."
                        ),
                    }
                ),
                json.dumps(
                    {
                        "event": "usage",
                        "cumulative_usage": {"input_tokens": 100, "output_tokens": 20},
                        "estimated_cost_usd": 0.001,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    recovered = _recover_deferred_model_patches(
        frontend, file_comm, tmp_path, 1
    )
    commit = _checkpoint_interrupted_model_work(
        frontend, file_comm, tmp_path, 1, "generate"
    )

    assert recovered == {"index.html"}
    assert '<a href="/settings.html">' in (frontend / "index.html").read_text()
    assert commit == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    recovery_events = [
        json.loads(line)
        for line in trace.read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("event") == "harness_recovery_patch"
    ]
    assert recovery_events[0]["path"] == "frontend/index.html"


def test_interrupted_root_generate_checkpoints_trace_written_untracked_files(tmp_path: Path):
    import json
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=frontend, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "chore: baseline"],
        cwd=frontend, check=True, capture_output=True,
    )
    (frontend / "main.js").write_text("const value = 1;\n", encoding="utf-8")
    file_comm = FileComm(tmp_path / ".harness")
    trace = file_comm.dir / "traces" / "generator_round_1.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("\n".join([
        json.dumps({"event": "run_start", "model": "qwen3.6-plus"}),
        json.dumps({"event": "assistant", "message": {"tool_calls": [{"function": {"name": "write_file", "arguments": json.dumps({"path": "frontend/main.js"})}}]}}),
        json.dumps({"event": "tool", "name": "write_file", "ok": True, "output": "wrote"}),
        json.dumps({"event": "usage", "cumulative_usage": {"input_tokens": 100, "output_tokens": 20}, "estimated_cost_usd": 0.001}),
        json.dumps({"event": "assistant", "message": {"tool_calls": [{"function": {"name": "run_command", "arguments": json.dumps({"command": "node --check frontend/main.js"})}}]}}),
        json.dumps({"event": "tool", "name": "run_command", "ok": True, "output": ""}),
    ]) + "\n", encoding="utf-8")

    commit = _checkpoint_interrupted_model_work(
        frontend, file_comm, tmp_path, 1, "generate"
    )

    assert commit == subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=frontend, check=True,
        text=True, capture_output=True,
    ).stdout == ""
    metadata = json.loads(
        (file_comm.dir / "recovery_commit_round_1.json").read_text(encoding="utf-8")
    )
    assert metadata["source_files"] == ["main.js"]
    assert metadata["precheckpoint_usage"]["estimated_cost_usd"] == 0.001


def test_interrupted_generate_prompt_does_not_hide_untracked_files(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.dir.mkdir(parents=True, exist_ok=True)
    sprint_context = {
        "number": 1,
        "title": "Root",
        "feature_ids": ["F001"],
        "deliverables": [],
        "exit_criteria": [],
    }

    prompt = _build_generator_prompt(
        mode="generate",
        file_comm=file_comm,
        round_num=1,
        sprint_num=1,
        sprint_context=sprint_context,
        accepted_sprints={"accepted": []},
        resume_uncommitted_work=True,
    )

    assert "git -C frontend status --short" in prompt
    assert "untracked" in prompt


def test_generator_rejects_external_runtime_resources_but_allows_plain_links(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text(
        '<a href="https://example.com/docs">Docs</a>\n'
        '<link rel="stylesheet" href="https://fonts.example.com/font.css">\n',
        encoding="utf-8",
    )

    error = _validate_no_external_runtime_dependencies(frontend)

    assert error is not None
    assert "index.html" in error
    assert "fonts.example.com" in error
    (frontend / "index.html").write_text(
        '<a href="https://example.com/docs">Docs</a>\n'
        '<link rel="stylesheet" href="styles.css">\n',
        encoding="utf-8",
    )
    assert _validate_no_external_runtime_dependencies(frontend) is None


def test_generator_rejects_external_css_and_javascript_network_dependencies(
    tmp_path: Path,
):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "styles.css").write_text(
        '.hero { background: url("https://cdn.example.com/hero.png"); }\n',
        encoding="utf-8",
    )
    (frontend / "app.js").write_text(
        'fetch("https://api.example.com/books");\n', encoding="utf-8"
    )

    error = _validate_no_external_runtime_dependencies(frontend)

    assert error is not None
    assert "app.js" in error
    assert "styles.css" in error


def test_incremental_generator_grandfathers_accepted_external_runtime_dependency(
    tmp_path: Path,
):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=frontend,
        check=True,
    )
    (frontend / "index.html").write_text(
        '<link rel="stylesheet" href="https://fonts.example.com/legacy.css">\n',
        encoding="utf-8",
    )
    (frontend / "app.js").write_text("const version = 1;\n", encoding="utf-8")
    subprocess.run(["git", "add", "--all"], cwd=frontend, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: accepted source"],
        cwd=frontend,
        check=True,
        capture_output=True,
    )
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=frontend,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    (frontend / "app.js").write_text("const version = 2;\n", encoding="utf-8")

    assert _validate_no_external_runtime_dependencies(
        frontend, baseline_commit=baseline
    ) is None

    (frontend / "app.js").write_text(
        'fetch("https://api.example.com/new");\n', encoding="utf-8"
    )
    error = _validate_no_external_runtime_dependencies(
        frontend, baseline_commit=baseline
    )
    assert error is not None
    assert "api.example.com/new" in error


def test_harness_checkpoint_requires_exact_metadata_commit_and_clean_tree(tmp_path: Path):
    import json
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=frontend, check=True)
    (frontend / "main.js").write_text("const a = 1;\n")
    subprocess.run(["git", "add", "main.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "chore: baseline"], cwd=frontend, check=True, capture_output=True)
    (frontend / "main.js").write_text("const a = 2;\n")
    subprocess.run(["git", "add", "main.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "feat(recovery): checkpoint interrupted model implementation"], cwd=frontend, check=True, capture_output=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=frontend, check=True, text=True, capture_output=True).stdout.strip()
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "recovery_commit_round_1.json").write_text(json.dumps({
        "status": "ok", "commit_mode": "harness_checkpoint", "round": 1, "commit": head,
        "source_change_author": "native_model_trace", "source_files": ["main.js"],
    }))
    assert _is_harness_checkpoint_for_round(frontend, file_comm, 1, "generate")
    (frontend / "stray.txt").write_text("not committed\n")
    assert not _is_harness_checkpoint_for_round(frontend, file_comm, 1, "generate")


@pytest.mark.anyio
async def test_generator_generate_mode_builds_sprint_scoped_prompt(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        sprint_num=1,
        mode="generate",
    )

    # claude-sonnet-4-6 at $3 per 1M input tokens
    # → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert "Mode: generate" in captured["prompt"]
    assert "Sprint: 1" in captured["prompt"]
    assert "Sprint Title: Core counter" in captured["prompt"]
    assert "Target Feature IDs: F001" in captured["prompt"]
    assert "Required Reads:" in captured["prompt"]
    assert "do not repeatedly reread a truncated whole source file" in captured["prompt"]
    assert "- .harness/sprint_plan.json" in captured["prompt"]
    assert "- .harness/design_tokens.json" in captured["prompt"]
    assert "Do not reread feature_list.json or accepted_sprints.json" in captured["prompt"]
    assert ".harness/feedback_round_1.md" not in captured["prompt"]
    assert ".harness/grade_round_1.json" not in captured["prompt"]
    assert "Do not implement future sprint functionality or unrelated refactors." in captured["prompt"]
    assert "preserve accepted work" in captured["prompt"]
    assert "Do not attempt to read that path" in captured["prompt"]
    assert "npm --prefix frontend run build" in captured["prompt"]
    assert "Read the planning bundle first" not in captured["prompt"]
    assert ".harness/spec.md" not in captured["prompt"]
    assert ".harness/ui_verification_plan.json" in captured["prompt"]
    assert "exact stable selector specified" in captured["prompt"]


@pytest.mark.anyio
async def test_generator_repair_mode_builds_feedback_scoped_prompt(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_feedback(1, "Fix reset interaction.")
    file_comm.write_grades(
        1,
        {
            "round": 1,
            "overall_passed": False,
            "criteria": {
                "design_quality": {"score": 6.0, "passed": True},
                "functionality": {"score": 5.0, "passed": False, "notes": "increment broken"},
                "originality": {"score": 5.0, "passed": True},
                "craft": {"score": 6.0, "passed": True},
            },
            "ui_checks": [
                {
                    "check_id": "UI-001",
                    "feature_id": "F001",
                    "critical": True,
                    "status": "fail",
                    "task": "Click increment once.",
                    "expected_result": "Counter increments by one.",
                    "notes": "Counter does not change after click.",
                },
                {
                    "check_id": "UI-099",
                    "feature_id": "F999",
                    "critical": False,
                    "status": "fail",
                    "task": "Unrelated future sprint check.",
                    "expected_result": "Future feature visible.",
                    "notes": "future sprint thing",
                },
                {
                    "check_id": "UI-002",
                    "feature_id": "F001",
                    "critical": False,
                    "status": "partial",
                    "task": "Audio output check.",
                    "expected_result": "Click plays audio.",
                    "notes": "Sound missing on click.",
                },
            ],
            "target_exit_criteria_results": [
                {
                    "criterion_id": "EXIT-01-01",
                    "feature_id": "F001",
                    "critical": True,
                    "passed": False,
                    "criterion": "Counter increments correctly.",
                    "notes": "increment regression",
                }
            ],
        },
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    (tmp_path / "seed_manifest.json").write_text("{}\n")
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    stats = await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=2,
        sprint_num=1,
        mode="repair",
    )

    # claude-sonnet-4-6 at $3 per 1M input tokens → 100_000 * 3 / 1e6 = $0.30.
    assert stats.cost_usd == 0.3
    assert stats.duration_ms == 1
    assert "Mode: repair" in captured["prompt"]
    assert "Sprint: 1" in captured["prompt"]
    assert "Sprint Title: Core counter" in captured["prompt"]
    assert "Repair Scope: Fix evaluator-reported issues for the current sprint only" in captured["prompt"]
    assert "## Previous evaluation findings" in captured["prompt"]
    # Failed criterion below threshold is inlined.
    assert "functionality" in captured["prompt"]
    assert "increment broken" in captured["prompt"]
    # Failed UI check for the current sprint is inlined; future-sprint check is filtered out.
    assert "Counter does not change after click." in captured["prompt"]
    assert "UI-099" not in captured["prompt"]
    assert "future sprint thing" not in captured["prompt"]
    # Failed exit criterion is inlined.
    assert "increment regression" in captured["prompt"]
    # No self-report file is referenced anymore.
    assert "repair_targets_round_" not in captured["prompt"]
    assert "repair_report_round_" not in captured["prompt"]
    assert "Repair Completion Protocol" not in captured["prompt"]
    assert "next evaluation round verifies your work" in captured["prompt"]
    assert "Required minimal reads:" in captured["prompt"]
    assert ".harness/feedback_round_1.md" not in captured["prompt"]
    assert ".harness/traces/evaluator_round_1.jsonl" not in captured["prompt"]
    assert ".harness/edit_scope_round_1.json" in captured["prompt"]
    assert "failed normal browser_click" in captured["prompt"]
    assert "targeted line-range reads" in captured["prompt"]
    assert "syntactically valid" in captured["prompt"]
    assert "pending asynchronous work" in captured["prompt"]
    assert "adding redundant event handlers" in captured["prompt"]
    assert "FIRST ACTION" in captured["prompt"]
    assert "copy `.harness/edit_scope_round_1.json` to `.harness/edit_scope_round_2.json`" in captured["prompt"]
    assert "merely partial or unverified check is not by itself proof" in captured["prompt"]
    assert "Never alter required product visibility" in captured["prompt"]
    assert "Preserve the previous edit scope" in captured["prompt"]
    assert "scope audit reports an undeclared new root" in captured["prompt"]
    assert ".harness/grade_round_1.json" not in captured["prompt"]
    assert "- .harness/sprint_plan.json" not in captured["prompt"]
    assert "- .harness/design_tokens.json" not in captured["prompt"]
    assert "- .harness/accepted_sprints.json" not in captured["prompt"]
    assert "- .harness/ui_verification_plan.json" in captured["prompt"]
    assert "Do not attempt to read that path" in captured["prompt"]
    assert "Do not implement new features from future sprints." in captured["prompt"]
    assert "Do not start work for the next sprint." in captured["prompt"]
    assert "npm --prefix frontend run build" in captured["prompt"]
    assert ".harness/spec.md" not in captured["prompt"]
    assert ".harness/feature_list.json" not in captured["prompt"]


def test_generator_prompt_recovers_existing_uncommitted_sprint_without_reexploring(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)

    prompt = _build_generator_prompt(
        mode="generate", file_comm=file_comm, round_num=2, sprint_num=2,
        sprint_context={"title": "Refine", "feature_ids": ["F002"], "goal": "Improve", "deliverables": [], "exit_criteria": []},
        accepted_sprints={"accepted": [1]}, resume_uncommitted_work=True,
    )

    assert "Interrupted-attempt recovery" in prompt
    assert "git -C frontend diff --stat" in prompt
    assert "Do not reread whole source files" in prompt
    assert "Verify the targeted diff" in prompt


def test_generator_system_prompt_limits_validation_and_git_workflow():
    prompt = GENERATOR_SYSTEM_PROMPT

    assert "The Harness, not you, starts the dev server" in prompt
    assert "Never start or background a dev server" in prompt
    assert "test_server.js" in prompt
    assert "one optional `git status`, then `git add`, then `git commit`" in prompt
    assert "no `&`, `&&`, `||`, `|`" in prompt
    assert "Command chains and pipelines" not in prompt
    assert "You own the Git history and decide when to commit" not in prompt


def test_atomic_executor_normalizes_existing_file_exact_patches():
    assert _normalize_atomic_patch_response(
        {
            "patches": [
                {
                    "path": "index.html",
                    "search": "<h2>Before</h2>",
                    "replace": "<h2>After</h2>",
                }
            ]
        }
    ) == [
        {
            "path": "frontend/index.html",
            "old_text": "<h2>Before</h2>",
            "new_text": "<h2>After</h2>",
        }
    ]


def test_atomic_executor_normalizes_planned_new_files():
    assert _normalize_atomic_new_files(
        {
            "new_files": [
                {
                    "path": "settings.html",
                    "content": "<!doctype html>\n<title>Settings</title>\n",
                }
            ]
        }
    ) == [
        {
            "path": "frontend/settings.html",
            "content": "<!doctype html>\n<title>Settings</title>\n",
        }
    ]


def test_atomic_exact_patch_operation_preserves_literal_replacements():
    payload = {'operations': [{'op': 'patches', 'path': 'page.html',
                              'old_text': '<h2>Before</h2>', 'new_text': '<h2>After</h2>'}]}
    assert _normalize_atomic_operations(payload) == []
    assert _normalize_atomic_patch_response(payload) == [
        {'path': 'frontend/page.html', 'old_text': '<h2>Before</h2>', 'new_text': '<h2>After</h2>'}]
    payload['operations'].append({'op': 'invented_action'})
    with pytest.raises(ValueError, match='unsupported atomic operation'):
        _normalize_atomic_operations(payload)


def test_complete_unapplied_response_is_replayed_without_another_model_call(tmp_path):
    trace = tmp_path/'trace.jsonl'
    raw = '{"operations":[{"op":"patches"}]}'
    events = [{'event': 'assistant_response', 'content': raw}, {'event': 'usage', 'request_usage': {}}]
    trace.write_text(''.join(json.dumps(item)+'\n' for item in events))
    assert _last_replayable_atomic_candidate(trace) == raw
    events.append({'event': 'tool', 'name': 'apply_patch', 'ok': True})
    trace.write_text(''.join(json.dumps(item)+'\n' for item in events))
    assert _last_replayable_atomic_candidate(trace) == ''


def test_atomic_executor_applies_compact_sha_line_protocol_without_old_text():
    source = "alpha\nbeta\ngamma\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    operations = _normalize_atomic_operations({
        "operations": [
            {
                "op": "replace_lines", "path": "main.js", "file_sha256": digest,
                "start_line": 2, "end_line": 2, "replacement": "BETA\n",
            },
            {
                "op": "insert_after", "path": "main.js", "file_sha256": digest,
                "after_line": 3, "content": "delta\n",
            },
        ]
    })

    updated, exact_inputs = _apply_sha_line_operations(
        source,
        operations,
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert updated == "alpha\nBETA\ngamma\ndelta\n"
    assert exact_inputs[0]["old_text"] == "beta\n"
    assert all(item["path"] == "frontend/main.js" for item in exact_inputs)


def test_atomic_executor_rejects_html_sibling_inserted_inside_deeper_open_tag():
    source = (
        '<section class="cta">\n'
        '    <div class="links">\n'
        '        <a href="#">Previous</a>\n'
        '    </div>\n'
        '</section>\n'
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    operation = {
        "op": "insert_after",
        "path": "frontend/index.html",
        "file_sha256": digest,
        "after_line": 3,
        "content": '    <section data-testid="history"></section>',
    }

    with pytest.raises(ValueError, match="unsafe HTML insertion boundary"):
        _apply_sha_line_operations(
            source,
            [operation],
            expected_path="frontend/index.html",
            expected_sha256=digest,
        )

    operation["after_line"] = 4
    updated, _ = _apply_sha_line_operations(
        source,
        [operation],
        expected_path="frontend/index.html",
        expected_sha256=digest,
    )
    assert updated.index('data-testid="history"') > updated.index("</div>")


def test_atomic_executor_accepts_unindented_body_child():
    source = '<html>\n<body>\n<footer>Existing footer</footer>\n</body>\n</html>\n'
    digest = hashlib.sha256(source.encode()).hexdigest()
    operation = {
        "op": "insert_after", "path": "frontend/index.html",
        "file_sha256": digest, "after_line": 3,
        "content": '<section id="workspace">New workspace</section>',
    }
    updated, _ = _apply_sha_line_operations(
        source, [operation], expected_path="frontend/index.html", expected_sha256=digest,
    )
    assert '<footer>Existing footer</footer>\n<section id="workspace">New workspace</section>\n</body>' in updated


def test_atomic_executor_accepts_unindented_child_inside_unindented_main():
    source = (
        '<main data-testid="workspace">\n'
        '<section data-testid="board"></section>\n'
        '</main>\n'
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    operation = {
        "op": "insert_after", "path": "frontend/index.html",
        "file_sha256": digest, "after_line": 2,
        "content": '<section data-testid="intake"></section>',
    }
    updated, _ = _apply_sha_line_operations(
        source, [operation], expected_path="frontend/index.html", expected_sha256=digest,
    )
    assert '<section data-testid="board"></section>\n<section data-testid="intake"></section>\n</main>' in updated


def test_atomic_executor_rejects_replacement_that_unbalances_html():
    source = (
        '<section class="cta">\n'
        '    <div class="links">\n'
        '        <a href="#">Alternative</a>\n'
        '    </div>\n'
        '</section>\n'
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    operation = {
        "op": "replace_lines",
        "path": "frontend/index.html",
        "file_sha256": digest,
        "start_line": 3,
        "end_line": 3,
        "replacement": (
            '</section>\n'
            '<section data-testid="history"></section>\n'
            '</section>'
        ),
    }

    with pytest.raises(ValueError, match="unbalanced HTML edit"):
        _apply_sha_line_operations(
            source,
            [operation],
            expected_path="frontend/index.html",
            expected_sha256=digest,
        )


def test_atomic_executor_rebases_one_novel_html_subtree_without_replacing_context():
    source = (
        '<main>\n'
        '    <section class="card">\n'
        '        <div class="links">\n'
        '            <a href="#">Alternative</a>\n'
        '        </div>\n'
        '    </section>\n'
        '\n'
        '    <aside class="sidebar">Keep</aside>\n'
        '</main>\n'
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    operation = {
        "op": "replace_lines",
        "path": "frontend/index.html",
        "file_sha256": digest,
        "start_line": 4,
        "end_line": 4,
        "replacement": (
            '        </div>\n'
            '    </section>\n\n'
            '    <!-- HISTORY VIEW -->\n'
            '    <section data-testid="history">\n'
            '        <h2 data-testid="history-title">History</h2>\n'
            '    </section>\n\n'
            '    <aside class="sidebar">Keep</aside>'
        ),
    }

    updated, exact_inputs = _apply_sha_line_operations(
        source,
        [operation],
        expected_path="frontend/index.html",
        expected_sha256=digest,
    )

    assert updated.count('data-testid="history"') == 1
    assert updated.count('data-testid="history-title"') == 1
    assert updated.count('<!-- HISTORY VIEW -->') == 1
    assert updated.count('<a href="#">Alternative</a>') == 1
    assert updated.count('<aside class="sidebar">Keep</aside>') == 1
    assert updated.index('data-testid="history"') > updated.index('    </section>')
    assert exact_inputs[0]["old_text"] in source


def test_atomic_executor_rebases_section_replacement_to_comment_boundaries():
    source = (
        "(function () {\n"
        "    // ---------- Download state machine ----------\n"
        "    const button = true;\n"
        "    if (button) {\n"
        "        start();\n"
        "    }\n"
        "\n"
        "    // ---------- Sidebar ----------\n"
        "    keepSidebar();\n"
        "})();\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    replacement = (
        "    // ---------- Download state machine and history ----------\n"
        "    const button = true;\n"
        "    if (button) {\n"
        "        start();\n"
        "        saveHistory();\n"
        "    }\n"
        "\n"
    )

    updated, exact_inputs = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations(
            {
                "operations": [
                    {
                        "op": "replace_lines",
                        "path": "main.js",
                        "file_sha256": digest,
                        "start_line": 3,
                        "end_line": 5,
                        "replacement": replacement,
                    }
                ]
            }
        ),
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert "saveHistory();" in updated
    assert updated.count("// ---------- Sidebar ----------") == 1
    assert exact_inputs[0]["old_text"].startswith(
        "    // ---------- Download state machine ----------"
    )


def test_atomic_executor_rebases_replacement_to_same_named_function_boundary():
    source = (
        "if (ready) {\n"
        "    function loadHistory() {\n"
        "        oldRender();\n"
        "    }\n"
        "    keepGoing();\n"
        "}\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, exact_inputs = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations(
            {
                "operations": [
                    {
                        "op": "replace_lines",
                        "path": "main.js",
                        "file_sha256": digest,
                        "start_line": 3,
                        "end_line": 3,
                        "replacement": (
                            "    function loadHistory() {\n"
                            "        newRender();\n"
                            "    }\n"
                        ),
                    }
                ]
            }
        ),
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert updated.count("function loadHistory()") == 1
    assert "newRender();" in updated
    assert "keepGoing();" in updated
    assert exact_inputs[0]["old_text"].startswith("    function loadHistory()")


def test_atomic_executor_preserves_explicit_range_for_multiple_functions():
    source = (
        "function loadState() {\n"
        "  oldLoad();\n"
        "}\n"
        "function saveState() {\n"
        "  oldSave();\n"
        "}\n"
        "keep();\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    replacement = (
        "function loadState() { newLoad(); }\n"
        "function saveState() { newSave(); }\n"
        "function renderHistory() { render(); }\n"
    )

    updated, exact_inputs = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations({"operations": [{
            "op": "replace_lines",
            "path": "main.js",
            "file_sha256": digest,
            "start_line": 1,
            "end_line": 6,
            "replacement": replacement,
        }]}),
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert updated.count("function saveState()") == 1
    assert "function renderHistory()" in updated
    assert "keep();" in updated
    assert exact_inputs[0]["_harness_end_line"] == 6


def test_atomic_executor_does_not_expand_incomplete_function_prefix():
    source = (
        "function renderGallery() {\n"
        "  items.forEach(item => {\n"
        "    render(item);\n"
        "  });\n"
        "}\n"
        "keep();\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, exact_inputs = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations(
            {
                "operations": [
                    {
                        "op": "replace_lines",
                        "path": "main.js",
                        "file_sha256": digest,
                        "start_line": 1,
                        "end_line": 2,
                        "replacement": (
                            "function renderGallery() {\n"
                            "  filteredItems.forEach(item => {\n"
                        ),
                    }
                ]
            }
        ),
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert "    render(item);" in updated
    assert "keep();" in updated
    assert exact_inputs[0]["old_text"] == (
        "function renderGallery() {\n  items.forEach(item => {\n"
    )


def test_atomic_executor_stops_section_rebase_at_lower_indent_comment():
    source = (
        "  // Type Filter\n"
        "  if (filter) {\n"
        "    oldHandler();\n"
        "  }\n"
        "// Start App\n"
        "document.addEventListener('DOMContentLoaded', init);\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, _exact_inputs = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations(
            {
                "operations": [
                    {
                        "op": "replace_lines",
                        "path": "main.js",
                        "file_sha256": digest,
                        "start_line": 2,
                        "end_line": 4,
                        "replacement": (
                            "  // Type Filter\n"
                            "  if (filter) {\n"
                            "    newHandler();\n"
                            "  }"
                        ),
                    }
                ]
            }
        ),
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert "newHandler();\n  }\n// Start App" in updated
    assert "document.addEventListener('DOMContentLoaded', init);" in updated


def test_atomic_executor_preserves_function_header_for_indented_body_slice():
    source = (
        "function setupEventListeners() {\n"
        "  // Reset Button\n"
        "  resetBtn.addEventListener('click', () => {\n"
        "    oldReset();\n"
        "  });\n"
        "  keepOtherListener();\n"
        "}\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, exact_inputs = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations({
            "operations": [{
                "op": "replace_lines",
                "path": "app.js",
                "file_sha256": digest,
                "start_line": 1,
                "end_line": 5,
                "replacement": (
                    "  // Reset Button\n"
                    "  resetBtn.addEventListener('click', () => {\n"
                    "    newReset();\n"
                    "  });\n"
                ),
            }],
        }),
        expected_path="frontend/app.js",
        expected_sha256=digest,
    )

    assert updated.startswith("function setupEventListeners() {\n  // Reset Button")
    assert "newReset();" in updated
    assert "keepOtherListener();\n}" in updated
    assert exact_inputs[0]["_harness_start_line"] == 2


def test_atomic_executor_does_not_expand_past_included_next_section_header():
    source = (
        "// Event Listeners\n"
        "function setup() {\n"
        "  old();\n"
        "}\n"
        "\n"
        "// Start App\n"
        "document.addEventListener('DOMContentLoaded', init);\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, _ = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations({
            "operations": [{
                "op": "replace_lines",
                "path": "app.js",
                "file_sha256": digest,
                "start_line": 2,
                "end_line": 6,
                "replacement": (
                    "// Event Listeners\n"
                    "function setup() {\n"
                    "  updated();\n"
                    "}\n"
                ),
            }],
        }),
        expected_path="frontend/app.js",
        expected_sha256=digest,
    )

    assert "updated();" in updated
    assert "document.addEventListener('DOMContentLoaded', init);" in updated


def test_atomic_executor_uses_unique_suffix_for_blank_line_insertion():
    source = "function one() {\n}\n\nfunction two() {\n}\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, exact_inputs = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations(
            {
                "operations": [
                    {
                        "op": "insert_after",
                        "path": "main.js",
                        "file_sha256": digest,
                        "after_line": 3,
                        "content": "const inserted = true;\n",
                    }
                ]
            }
        ),
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert "\n\nconst inserted = true;\nfunction two" in updated
    assert source.count(exact_inputs[0]["old_text"]) == 1


def test_atomic_executor_rebases_named_function_after_adjacent_outer_brace():
    source = (
        "function renderGallery() {\n"
        "  items.forEach(item => {\n"
        "    render(item);\n"
        "  });\n"
        "}\n"
        "start();\n"
    )
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, _ = _apply_sha_line_operations(
        source,
        _normalize_atomic_operations({
            "operations": [{
                "op": "insert_after",
                "path": "main.js",
                "file_sha256": digest,
                "after_line": 4,
                "content": "function renderHistory() {\n  return true;\n}\n",
            }],
        }),
        expected_path="frontend/main.js",
        expected_sha256=digest,
    )

    assert "  });\n}\nfunction renderHistory()" in updated
    assert updated.index("function renderHistory") < updated.index("start();")


def test_atomic_executor_copy_from_normalizes_nested_line_edits():
    payload = _normalize_atomic_operations({
        "operations": [{
            "op": "copy_from",
            "source": "index.html",
            "path": "settings.html",
            "source_sha256": "abc",
            "line_edits": [{
                "op": "replace_lines", "start_line": 3, "end_line": 3,
                "replacement": "<h1>Settings</h1>\n",
            }],
        }]
    })

    assert payload[0]["source"] == "frontend/index.html"
    assert payload[0]["path"] == "frontend/settings.html"
    assert payload[0]["line_edits"][0]["path"] == ""


def test_complete_inner_method_preserves_adjacent_outer_call_closer():
    import hashlib
    source = "const view = mount({\n  onUpdate(job){\n    render(job);\n  },\n});\nview.start();\n"
    digest = hashlib.sha256(source.encode()).hexdigest()
    updated, patches = _apply_sha_line_operations(source, [{
        "op": "replace_lines", "path": "frontend/app.js", "start_line": 2, "end_line": 5,
        "replacement": "  onUpdate(job){\n    render(job.state);\n  },",
    }], expected_path="frontend/app.js", expected_sha256=digest, preserve_operations=True)
    assert updated == source.replace("render(job)", "render(job.state)")
    assert patches[0]["_harness_end_line"] == 4


def test_atomic_executor_removes_whitespace_only_lines_from_replacement():
    patches = _normalize_atomic_patch_response(
        {
            "patches": [
                {
                    "path": "index.html",
                    "search": "<div>Before</div>",
                    "replace": "<div>After</div>\n    \n\t\n<p>Done</p>",
                }
            ]
        }
    )

    assert patches[0]["new_text"] == "<div>After</div>\n\n\n<p>Done</p>"


def test_atomic_executor_removes_trailing_horizontal_whitespace_from_code_lines():
    patches = _normalize_atomic_patch_response(
        {
            "patches": [
                {
                    "path": "index.html",
                    "search": "<button>Before</button>",
                    "replace": "<button   \n  type=\"button\" \t\n>After</button>",
                }
            ]
        }
    )

    assert patches[0]["new_text"] == (
        "<button\n  type=\"button\"\n>After</button>"
    )


def test_hidden_target_contract_derives_concrete_control_placement_invariant():
    checks = [
        {
            "id": "toggle",
            "route": "/",
            "actions": [
                {"action": "click", "selector": "#control"},
                {"action": "assert_hidden", "selector": "#target"},
                {"action": "click", "selector": "#control"},
                {"action": "assert_visible", "selector": "#target"},
            ],
        }
    ]

    assert _control_topology_invariants(checks) == [
        {
            "route": "/",
            "control_selector": "#control",
            "hidden_target_selector": "#target",
            "constraint": "control_must_not_be_descendant_of_hidden_target",
        }
    ]
    directives = _render_control_topology_directives(
        _control_topology_invariants(checks)
    )
    assert "insert #control before the opening element for #target" in directives
    assert "never place #control between that target's opening and closing tags" in directives


def test_repair_packet_derives_selector_level_handler_instruction():
    checks = [
        {
            "id": "toggle",
            "actions": [
                {"action": "click", "selector": "#control"},
                {"action": "assert_hidden", "selector": "#target"},
            ],
        }
    ]
    packet = {
        "failed_checks": [
            {
                "check_id": "toggle",
                "steps": [
                    {"action": "click", "ok": True},
                    {"action": "assert_hidden", "ok": False},
                ],
            }
        ]
    }

    directives = _render_failed_action_directives(packet, checks)

    assert "clicking #control left #target visible" in directives
    assert "Fix #control's event handler" in directives
    assert "Do not only change #target's initial style" in directives


def test_repair_packet_turns_browser_reference_error_into_scope_directive():
    checks = [{
        "id": "deep-link",
        "actions": [{"action": "assert_visible", "selector": ".summary"}],
    }]
    packet = {"failed_checks": [{
        "check_id": "deep-link",
        "console_errors": [
            "renderSummary is not defined",
            "renderSummary is not defined",
        ],
        "steps": [{"action": "assert_visible", "ok": False}],
    }]}

    directives = _render_failed_action_directives(packet, checks)

    assert directives.count("renderSummary is not defined") == 1
    assert "common enclosing lexical scope" in directives
    assert "Do not hide the defect with `typeof`" in directives
    assert "duplicate helper" in directives
    assert "three-site bridge" in directives
    assert "assign the existing `renderSummary` function reference" in directives
    assert "use one `insert_after` operation" in directives
    assert "do not repeat or replace any closing brace" in directives


def test_repair_keeps_deduplicated_runtime_error_across_masking_rounds(
    tmp_path: Path,
):
    file_comm = FileComm(tmp_path / ".harness")
    (file_comm.dir / "repair_packet_round_2.json").write_text(
        json.dumps({"failed_checks": [{
            "console_errors": [
                "renderSummary is not defined",
                "renderSummary is not defined",
            ]
        }]}),
        encoding="utf-8",
    )
    (file_comm.dir / "repair_packet_round_3.json").write_text(
        json.dumps({"failed_checks": [{"console_errors": []}]}),
        encoding="utf-8",
    )

    errors = _recent_repair_runtime_errors(file_comm, before_round=3)

    assert errors == [{
        "source_round": 2,
        "error": "renderSummary is not defined",
    }]


def test_browser_error_compaction_retains_actionability_cause():
    error = "TimeoutError: drag_and_drop\n" + "waiting for target\n" * 180 + "fixed toolbar intercepts pointer events"
    compact = _compact_browser_error(error)
    assert compact.startswith("TimeoutError: drag_and_drop")
    assert "fixed toolbar intercepts pointer events" in compact
    assert len(compact) < 2100


def test_frozen_repair_prompt_uses_only_latest_reproduced_runtime_error(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_grades(2, {"round": 2, "sprint": 1, "criteria": {}, "overall_passed": False})
    for number, message in [(1, "obsoleteHelper is not defined"), (2, "Current list root is null")]:
        (file_comm.dir / f"repair_packet_round_{number}.json").write_text(json.dumps({
            "failed_checks": [{"check_id": "flow", "console_errors": [message]}],
        }))
    (file_comm.dir / "minimal_path_plan_round_3.json").write_text(json.dumps({
        "route_scope": {"target_routes": ["/"]},
        "source_change_cone": {"initial_paths": ["frontend/app.js"]},
        "budgets": {"max_patch_lines": 30, "max_touched_files": 1},
    }))
    (file_comm.dir / "edit_context_round_3.json").write_text(json.dumps({
        "schema_version": "edit-context-v1", "round": 3,
        "source_windows": [{"path": "frontend/app.js", "start_line": 1,
                            "end_line": 1, "content": "const list = null;\n"}],
        "exposure": {"full_source_chars": 19, "exposed_source_chars": 19},
    }))
    prompt = _build_generator_prompt(
        mode="repair", file_comm=file_comm, round_num=3, sprint_num=1,
        sprint_context={"goal": "Repair list", "feature_ids": []},
        accepted_sprints={"accepted": []}, frozen_compound=True,
    )
    assert "Current list root is null" in prompt
    assert "obsoleteHelper" not in prompt
    assert "a later candidate may have masked" not in prompt


def test_repair_packet_derives_exact_scroll_restore_instruction():
    checks = [{
        "id": "restore",
        "actions": [{"action": "assert_scroll", "y": 500}],
    }]
    packet = {"failed_checks": [{
        "check_id": "restore",
        "steps": [{
            "action": "assert_scroll",
            "ok": False,
            "output": {"actual": 375, "expected": 500},
        }],
    }]}

    directives = _render_failed_action_directives(packet, checks)

    assert "window.scrollY was 375, expected 500" in directives
    assert "after the prior filter/layout is visible and stable" in directives


def test_repair_packet_derives_reload_safe_value_restore_instruction():
    checks = [{
        "id": "filter-context",
        "actions": [
            {"action": "reload"},
            {"action": "assert_value", "selector": "#filter", "value": "Document"},
        ],
    }]
    packet = {"failed_checks": [{
        "check_id": "filter-context",
        "steps": [
            {"action": "reload", "ok": True},
            {
                "action": "assert_value", "ok": False,
                "output": {"actual": "All", "expected": "Document"},
            },
        ],
    }]}

    directives = _render_failed_action_directives(packet, checks)

    assert "#filter had value 'All', expected 'Document'" in directives
    assert "addressable route" in directives


def test_repair_packet_preserves_existing_target_that_disappears_after_reload():
    selector = "[data-testid='reorder-history-item']"
    checks = [{"id": "persistence", "actions": [
        {"action": "click", "selector": "#reset"},
        {"action": "assert_visible", "selector": selector},
        {"action": "reload"},
        {"action": "assert_visible", "selector": selector},
    ]}]
    packet = {"failed_checks": [{"check_id": "persistence", "steps": [
        {"action": "click", "ok": True},
        {"action": "assert_visible", "ok": True},
        {"action": "reload", "ok": True},
        {"action": "assert_visible", "ok": False},
    ]}]}

    directives = _render_failed_action_directives(packet, checks)

    assert "was visible before reload and disappeared only after reload" in directives
    assert "Preserve the existing target markup" in directives
    assert "do not add a duplicate target or replace the router" in directives


def test_repair_context_reports_exact_and_unwired_near_duplicate_functions():
    context = {"source_windows": [{"content": """
function handleRoute() { renderReorderHistory(); }
function handleRouteChange() { renderGallery(); }
function setupRouter() { window.addEventListener('hashchange', handleRouteChange); }
function renderGallery() {}
function renderReorderHistory() {}
"""}]}

    assert _declared_source_functions(context) == [
        "handleRoute",
        "handleRouteChange",
        "renderGallery",
        "renderReorderHistory",
        "setupRouter",
    ]
    assert _unreferenced_near_duplicate_functions(context) == [
        ("handleRoute", "handleRouteChange")
    ]


def test_repair_packet_derives_navigation_instruction_from_failed_wait():
    checks = [{
        "id": "gallery",
        "actions": [
            {"action": "click", "selector": "a[href='#gallery']"},
            {"action": "wait_for", "selector": "#gallery-grid"},
        ],
    }]
    packet = {"failed_checks": [{
        "check_id": "gallery",
        "steps": [
            {"action": "click", "ok": True},
            {"action": "wait_for", "ok": False},
        ],
    }]}

    directives = _render_failed_action_directives(packet, checks)

    assert "after clicking a[href='#gallery'], #gallery-grid never became visible" in directives
    assert "navigation/state transition" in directives


def test_repair_packet_derives_missing_stable_selector_instruction():
    checks = [{
        "id": "direct-link",
        "actions": [{"action": "wait_for", "selector": "[data-testid='driver']"}],
    }]
    packet = {"failed_checks": [{
        "check_id": "direct-link",
        "steps": [{"action": "wait_for", "ok": False}],
    }]}

    directives = _render_failed_action_directives(packet, checks)

    assert "required target [data-testid='driver'] never became visible" in directives
    assert "do not create a duplicate UI surface" in directives


def test_repair_packet_routes_missing_state_class_to_behavior_not_markup():
    selector = 'a.sidebar-item[data-name="Audio Studio Driver"][class~="highlighted"]'
    base = 'a.sidebar-item[data-name="Audio Studio Driver"]'
    checks = [{"id": "deep-link", "actions": [
        {"action": "set_hash", "value": "#audio-studio-driver"},
        {"action": "assert_visible", "selector": selector},
    ]}]
    packet = {"failed_checks": [{"check_id": "deep-link", "steps": [
        {"action": "set_hash", "ok": True},
        {
            "action": "assert_visible", "ok": False,
            "visibility_diagnostic": {
                "matched_count": 0,
                "base_selector": base,
                "base_matched_count": 1,
            },
        },
    ]}]}

    directives = _render_failed_action_directives(packet, checks)

    assert "Repair the event/hash state transition in behavior code" in directives
    assert "do not add or duplicate the existing DOM item" in directives
    assert "after setting hash #audio-studio-driver" in directives
    assert "Attribute selector values are case-sensitive" in directives


def test_repair_packet_explains_hidden_ancestor_not_target_style():
    checks = [{
        "id": "detail",
        "actions": [
            {"action": "click", "selector": ".artifact-card"},
            {"action": "assert_visible", "selector": "#detail"},
        ],
    }]
    packet = {"failed_checks": [{
        "check_id": "detail",
        "steps": [
            {"action": "click", "ok": True},
            {
                "action": "assert_visible",
                "ok": False,
                "visibility_diagnostic": {
                    "matched_count": 1,
                    "hidden_ancestors": [{
                        "label": "#view-gallery",
                        "hidden_reasons": ["display=none", "zero-geometry"],
                    }],
                },
            },
        ],
    }]}

    directives = _render_failed_action_directives(packet, checks)

    assert "ancestor #view-gallery is hidden" in directives
    assert "display=none" in directives
    assert "changing only the target's own display" in directives


def test_atomic_executor_supports_preloaded_multi_file_dependency_and_new_page(
    tmp_path: Path,
):
    harness = tmp_path / ".harness"
    harness.mkdir()
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "app.js").write_text("const app = true;", encoding="utf-8")
    (frontend / "index.html").write_text("<main></main>", encoding="utf-8")
    (frontend / "unseen.js").write_text("const unseen = true;", encoding="utf-8")
    (harness / "edit_context_round_1.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-context-v1",
                "source_windows": [
                    {"path": "frontend/app.js", "content": "const app = true;"},
                    {"path": "frontend/index.html", "content": "<main></main>"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (harness / "minimal_path_plan_round_1.json").write_text(
        json.dumps(
            {
                "source_change_cone": {
                    "local_paths": ["frontend/app.js", "frontend/index.html"],
                    "dependency_paths": ["frontend/styles.css"],
                    "planned_new_paths": ["frontend/settings.html"],
                }
            }
        ),
        encoding="utf-8",
    )

    assert _atomic_executor_eligible(
        config=HarnessConfig(agent_runtime="openai", generator_model="qwen-test"),
        workdir=tmp_path,
        round_num=1,
    ) is True

    # Every existing mutation candidate must already be visible in the bounded
    # context. A planned new file needs no source window.
    (harness / "minimal_path_plan_round_1.json").write_text(
        json.dumps(
            {
                "source_change_cone": {
                    "local_paths": [
                        "frontend/app.js",
                        "frontend/index.html",
                        "frontend/unseen.js",
                    ],
                    "dependency_paths": [],
                    "planned_new_paths": ["frontend/settings.html"],
                }
            }
        ),
        encoding="utf-8",
    )
    assert _atomic_executor_eligible(
        config=HarnessConfig(agent_runtime="openai", generator_model="qwen-test"),
        workdir=tmp_path,
        round_num=1,
    ) is False


@pytest.mark.anyio
async def test_generator_generate_mode_reads_previous_feedback_when_present(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_feedback(1, "Preserve the accepted nav spacing.")
    file_comm.write_grades(
        1,
        {
            "round": 1,
            "overall_passed": True,
            "criteria": {
                "design_quality": {"score": 7.0, "passed": True},
                "functionality": {"score": 7.0, "passed": True},
                "originality": {"score": 6.0, "passed": True},
                "craft": {"score": 7.0, "passed": True},
            },
            "mode_recommendation": "generate_next_sprint",
        },
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    captured: dict = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=2,
        sprint_num=2,
        mode="generate",
    )

    assert ".harness/feedback_round_1.md" in captured["prompt"]
    assert ".harness/grade_round_1.json" in captured["prompt"]
    assert "avoid regressions" in captured["prompt"]
    assert "without re-opening already accepted sprint scope" in captured["prompt"]


@pytest.mark.anyio
async def test_generator_exposes_repo_local_claude_skills_to_workdir(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")

    source_skills = tmp_path / "source-skills"
    (source_skills / "ui-ux-pro-max").mkdir(parents=True)
    (source_skills / "ui-ux-pro-max" / "SKILL.md").write_text("# local skill\n")
    monkeypatch.setattr("src.agents.generator._LOCAL_CLAUDE_SKILLS_DIR", source_skills)

    async def fake_run_sdk_agent(**kwargs):
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        sprint_num=1,
        mode="generate",
    )

    exposed = tmp_path / ".claude" / "skills"
    assert exposed.exists()
    assert not exposed.is_symlink()
    assert (exposed / "ui-ux-pro-max" / "SKILL.md").read_text() == "# local skill\n"


@pytest.mark.anyio
async def test_generator_raises_when_expected_dirs_are_missing(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)

    async def fake_run_sdk_agent(**kwargs):
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                result="I created some files elsewhere.",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="frontend"):
        await run_generator(
            HarnessConfig(),
            file_comm,
            tmp_path,
            round_num=1,
            sprint_num=1,
            mode="generate",
        )

    assert "I created some files elsewhere." in file_comm.read_build_log()


# --- empty frontend dir must not be treated as success ---


@pytest.mark.anyio
async def test_generator_raises_when_frontend_dir_is_empty(monkeypatch, tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    # Frontend dir exists but contains no package.json — agent exited too early.
    (tmp_path / "frontend").mkdir()

    async def fake_run_sdk_agent(**kwargs):
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                result="created folder",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="package.json"):
        await run_generator(
            HarnessConfig(),
            file_comm,
            tmp_path,
            round_num=1,
            sprint_num=1,
            mode="generate",
        )


# --- repair mode must not silently degrade to no-direction generate ---


@pytest.mark.anyio
async def test_generator_repair_raises_when_previous_grade_missing(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    # NB: grade_round_1.json deliberately not written.

    sdk_called = {"value": False}

    async def fake_run_sdk_agent(**kwargs):
        sdk_called["value"] = True
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.0,
                result="should not run",
            ),
            0.0,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    with pytest.raises(RuntimeError, match="grade_round_1"):
        await run_generator(
            HarnessConfig(),
            file_comm,
            tmp_path,
            round_num=2,
            sprint_num=1,
            mode="repair",
        )

    assert sdk_called["value"] is False, "Repair must not call the SDK when its inputs are missing"


# --- _describe_failures unit tests ---


def test_describe_failures_includes_failed_criterion():
    grades = {
        "criteria": {
            "design_quality": {"score": 4, "notes": "weak hero"},
            "functionality": {"score": 8, "notes": "ok"},
        }
    }
    text = _describe_failures(grades, sprint_context={"feature_ids": []})
    assert "design_quality" in text
    assert "weak hero" in text
    # Passing criterion not included.
    assert "functionality" not in text


def test_describe_failures_includes_failed_ui_check():
    grades = {
        "criteria": {},
        "ui_checks": [
            {"feature_id": "F-001", "status": "fail", "notes": "broken"},
            {"feature_id": "F-001", "status": "pass", "notes": "fine"},
        ],
    }
    text = _describe_failures(grades, sprint_context={"feature_ids": ["F-001"]})
    assert "F-001" in text
    assert "broken" in text
    assert "fine" not in text


def test_describe_failures_filters_ui_checks_outside_sprint_feature_ids():
    grades = {
        "criteria": {},
        "ui_checks": [
            {"feature_id": "F-001", "status": "fail", "notes": "in scope"},
            {"feature_id": "F-999", "status": "fail", "notes": "future sprint"},
        ],
    }
    text = _describe_failures(grades, sprint_context={"feature_ids": ["F-001"]})
    assert "in scope" in text
    assert "future sprint" not in text


def test_describe_failures_includes_failed_exit_criterion():
    grades = {
        "criteria": {},
        "target_exit_criteria_results": [
            {"feature_id": "F-001", "passed": False, "notes": "regression"},
            {"feature_id": "F-001", "passed": True, "notes": "ok"},
        ],
    }
    text = _describe_failures(grades, sprint_context={"feature_ids": ["F-001"]})
    assert "regression" in text
    assert "ok" not in text


def test_describe_failures_empty_returns_fallback():
    text = _describe_failures({}, sprint_context={"feature_ids": []})
    assert "no specific failures" in text


def test_describe_failures_includes_counterfactual_regression_and_repair_action():
    grades = {
        "criteria": {},
        "regressions_found": [
            "Counterfactual patch guard found removable atom p006."
        ],
        "repair_instructions": [
            "Remove p006 while preserving the passing target contract."
        ],
    }

    text = _describe_failures(grades, sprint_context={"feature_ids": []})

    assert "p006" in text
    assert "Counterfactual patch guard" in text
    assert "Remove p006" in text


def test_repair_prompt_reads_non_minimal_certificate_artifact(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_grades(1, {
        "round": 1,
        "sprint": 1,
        "criteria": {},
        "overall_passed": False,
        "minimality_certificate": {
            "edit": {
                "status": "non_minimal",
                "artifact": ".harness/minimality_round_1_edit.json",
            }
        },
        "regressions_found": ["Atom p006 is removable."],
        "repair_instructions": ["Remove p006."],
    })

    prompt = _build_generator_prompt(
        mode="repair",
        file_comm=file_comm,
        round_num=2,
        sprint_num=1,
        sprint_context={
            "title": "Core counter",
            "feature_ids": ["F001"],
            "goal": "Ship the primary counter flow.",
            "deliverables": [],
            "exit_criteria": [],
        },
        accepted_sprints={"accepted": []},
    )

    assert "- .harness/minimality_round_1_edit.json" in prompt
    assert "Atom p006 is removable." in prompt
    assert "failed source did not render" in prompt


def test_minimal_path_repair_uses_independent_preloaded_short_context(tmp_path: Path):
    import json

    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_grades(
        1,
        {
            "round": 1,
            "sprint": 1,
            "criteria": {},
            "overall_passed": False,
            "bugs_found": ["The save button leaves the status text unchanged."],
            "repair_instructions": ["Update the status text after save."],
        },
    )
    (file_comm.dir / "minimal_path_plan_round_2.json").write_text(
        json.dumps(
            {
                "schema_version": "minimal-path-plan-v4",
                "round": 2,
                "route_scope": {"target_routes": ["/settings.html"]},
                "source_change_cone": {
                    "initial_paths": ["frontend/settings.js"],
                    "dependency_paths": [],
                    "guarded_shared_regions": [],
                },
                "budgets": {"max_patch_lines": 20, "max_touched_files": 1},
            }
        ),
        encoding="utf-8",
    )
    (file_comm.dir / "repair_packet_round_1.json").write_text(
        json.dumps({
            "status": "repairable",
            "failed_checks": ["UI-SAVE"],
            "regressions": [
                "Edit guard failed: Alternative OS changed to Audio Studio Driver"
            ],
        }),
        encoding="utf-8",
    )
    (file_comm.dir / "edit_context_round_2.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-context-v1",
                "round": 2,
                "source_windows": [
                    {
                        "path": "frontend/settings.js",
                        "start_line": 10,
                        "end_line": 12,
                        "content": "save.addEventListener('click', () => status.textContent = 'Old');\n",
                    }
                ],
                "exposure": {
                    "full_source_chars": 10000,
                    "exposed_source_chars": 70,
                    "ratio": 0.007,
                },
            }
        ),
        encoding="utf-8",
    )

    prompt = _build_generator_prompt(
        mode="repair",
        file_comm=file_comm,
        round_num=2,
        sprint_num=1,
        sprint_context={
            "title": "Save status",
            "feature_ids": ["F001"],
            "goal": "Repair status feedback",
            "deliverables": [],
            "exit_criteria": [],
        },
        accepted_sprints={"accepted": []},
    )

    assert "Harness-selected source context" in prompt
    assert "save.addEventListener" in prompt
    assert "0.7%" in prompt
    assert "Do not reread planning, grade, or shown code" in prompt
    assert "Alternative OS changed to Audio Studio Driver" in prompt
    assert "### Failed criteria" not in prompt
    assert '"task":' not in prompt
    assert "Required minimal reads:" not in prompt
    assert "pending asynchronous work" not in prompt


def test_non_forward_repair_prompt_uses_failed_source_semantic_frame(tmp_path: Path):
    import json

    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_grades(1, {
        "round": 1, "criteria": {}, "overall_passed": False,
        "repair_instructions": ["Fix the broken control."],
    })
    (file_comm.dir / "repair_dom_source_round_2.json").write_text(json.dumps({
        "roots": [{"key": "main", "fingerprint": "abc"}]
    }))

    prompt = _build_generator_prompt(
        mode="repair", file_comm=file_comm, round_num=2, sprint_num=1,
        sprint_context={
            "title": "Repair", "feature_ids": ["F001"], "goal": "Repair",
            "deliverables": [], "exit_criteria": [],
        },
        accepted_sprints={"accepted": []},
    )

    assert "- .harness/repair_dom_source_round_2.json" in prompt
    assert "write `.harness/edit_scope_round_2.json`" in prompt
    assert "main" in prompt


def test_forward_prompt_consumes_harness_owned_minimal_path_plan(tmp_path: Path):
    import json

    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    (tmp_path / "seed_manifest.json").write_text("{}")
    (file_comm.dir / "edit_dom_baseline.json").write_text(
        json.dumps({"roots": [{"key": "main", "fingerprint": "x"}]})
    )
    (file_comm.dir / "minimal_path_plan_round_1.json").write_text(
        json.dumps(
            {
                "schema_version": "minimal-path-plan-v1",
                "owner": "harness",
                "round": 1,
                "source_change_cone": {
                    "local_paths": ["frontend/src/App.jsx"],
                    "dependency_paths": ["frontend/src/app.css"],
                },
                "budgets": {"max_patch_lines": 120, "max_touched_files": 3},
            }
        )
    )
    (file_comm.dir / "edit_scope_round_1.json").write_text(
        json.dumps(
            {
                "owner": "harness",
                "allowed_root_keys": ["main"],
                "allow_new_roots": False,
            }
        )
    )

    prompt = _build_generator_prompt(
        mode="generate",
        file_comm=file_comm,
        round_num=1,
        sprint_num=1,
        sprint_context={
            "title": "Scoped edit",
            "feature_ids": ["F001"],
            "goal": "Update search",
            "deliverables": ["Search update"],
            "exit_criteria": ["Search works"],
        },
        accepted_sprints={"accepted": []},
    )

    assert ".harness/minimal_path_plan_round_1.json" in prompt
    assert "harness already materialized" in prompt
    assert "Existing source overwrites are rejected" in prompt
    assert "route_scope.target_routes" in prompt
    assert "guarded_shared_regions" in prompt
    assert "route_isolation_strategy" in prompt
    assert "planned_companion_path" in prompt
    assert "do not wrap or rewrite an accepted page script" in prompt
    assert "write `.harness/edit_scope_round_1.json`" not in prompt


def test_explicit_edit_uses_compact_preloaded_implementation_prompt(tmp_path: Path):
    import json

    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    (tmp_path / "seed_manifest.json").write_text("{}", encoding="utf-8")
    (file_comm.dir / "minimal_path_plan_round_1.json").write_text(
        json.dumps(
            {
                "schema_version": "minimal-path-plan-v3",
                "round": 1,
                "route_scope": {"target_routes": ["/"]},
                "source_change_cone": {
                    "initial_paths": ["frontend/index.html"],
                    "dependency_paths": [],
                    "guarded_shared_regions": [],
                },
                "budgets": {"max_patch_lines": 20, "max_touched_files": 1},
                "design_system_context": {"css_custom_properties": []},
            }
        ),
        encoding="utf-8",
    )
    (file_comm.dir / "edit_context_round_1.json").write_text(
        json.dumps(
            {
                "schema_version": "edit-context-v1",
                "source_windows": [],
                "source_outlines": [
                    {
                        "path": "frontend/index.html",
                        "entries": [{"line": 90, "content": "<h2>Public Transit</h2>"}],
                    }
                ],
                "exposure": {
                    "full_source_chars": 10000,
                    "exposed_source_chars": 23,
                    "ratio": 0.0023,
                },
            }
        ),
        encoding="utf-8",
    )

    prompt = _build_generator_prompt(
        mode="generate",
        file_comm=file_comm,
        round_num=1,
        sprint_num=1,
        sprint_context={
            "title": "Transit toggle",
            "goal": "Toggle Public Transit",
            "feature_ids": ["EDIT-001"],
            "deliverables": ["Toggle control"],
            "exit_criteria": ["Show and hide"],
        },
        accepted_sprints={"accepted": []},
    )

    assert "Mode: edit" in prompt
    assert "<h2>Public Transit</h2>" in prompt
    assert "Do not read planning artifacts" in prompt
    assert "Deliverables:" not in prompt
    assert "Exit criteria:" not in prompt
    assert "expected_result" not in prompt
    assert "existing_css_tokens" not in prompt
    assert "must remain outside that hidden target" in prompt
    assert "Required Reads:" not in prompt


def test_scope_contract_only_repair_requires_all_product_checks_to_pass():
    grades = {
        "sprint_passed": True,
        "regression_passed": False,
        "edit_scope_audit": "fail",
        "ui_checks": [{"status": "pass"}],
        "target_exit_criteria_results": [{"passed": True}],
    }

    assert _is_scope_contract_only_repair(grades) is True
    grades["ui_checks"] = [{"status": "fail"}]
    assert _is_scope_contract_only_repair(grades) is False


def test_generator_system_prompt_requires_agent_owned_feat_fix_commits():
    assert "feat(scope):" in GENERATOR_SYSTEM_PROMPT
    assert "fix(scope):" in GENERATOR_SYSTEM_PROMPT
    assert "Create one final atomic commit" in GENERATOR_SYSTEM_PROMPT
    assert "Do not amend or rewrite Git history" in GENERATOR_SYSTEM_PROMPT


def test_generator_commit_gate_requires_mode_prefix_and_clean_tree(tmp_path: Path):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=frontend, check=True)
    (frontend / "app.js").write_text("base\n")
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "chore: baseline"], cwd=frontend, check=True, capture_output=True)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True, text=True, capture_output=True
    ).stdout.strip()

    assert "feat" in _validate_generator_commits(frontend, baseline, "generate")
    (frontend / "app.js").write_text("feature\n")
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "feat(core): add timer"], cwd=frontend, check=True, capture_output=True)
    assert _validate_generator_commits(frontend, baseline, "generate") is None


def test_repair_scope_gate_rejects_large_diff(tmp_path: Path):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=frontend, check=True)
    (frontend / "app.js").write_text("base\n")
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "chore: baseline"], cwd=frontend, check=True, capture_output=True)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    (frontend / "app.js").write_text("".join(f"line {i}\n" for i in range(1001)))
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "fix(core): broad rewrite"], cwd=frontend, check=True, capture_output=True)

    error = _validate_repair_scope(
        frontend, baseline, max_files=2, max_changed_lines=120
    )
    assert error is not None
    assert "too broad" in error
    assert "1002 changed lines" in error


def test_repair_scope_gate_accepts_small_diff(tmp_path: Path):
    import subprocess

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=frontend, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=frontend, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=frontend, check=True)
    (frontend / "app.js").write_text("const active = false;\n")
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "chore: baseline"], cwd=frontend, check=True, capture_output=True)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=frontend, check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    (frontend / "app.js").write_text("const active = true;\n")
    subprocess.run(["git", "add", "app.js"], cwd=frontend, check=True)
    subprocess.run(["git", "commit", "-m", "fix(core): enable interaction"], cwd=frontend, check=True, capture_output=True)

    assert _validate_repair_scope(
        frontend, baseline, max_files=2, max_changed_lines=120
    ) is None


def test_generator_runnable_files_gate_requires_package_json(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()

    assert "package.json" in _validate_generator_runnable_files(frontend, tmp_path)
    (frontend / "package.json").write_text('{"scripts":{"dev":"vite"}}')
    assert _validate_generator_runnable_files(frontend, tmp_path) is None


@pytest.mark.anyio
async def test_generator_includes_design_stage_reads_and_guidance_in_generate_mode(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_design_brief(
        {
            "requested_mode": "image-first",
            "visual_strategy": "image_backed_ui",
            "reference_files": {"background_ui": ".harness/design/background_ui.png"},
            "aesthetic_intent": {
                "design_hypothesis": "Use poster-like asymmetry.",
                "distinctive_features_to_preserve": ["poster-like asymmetry"],
                "generic_patterns_to_avoid": ["centered card grid"],
            },
            "responsive_strategy": {"desktop": "Layered", "mobile": "Stacked"},
            "overlay_regions": [{"id": "hero"}],
            "visual_success_criteria": ["Preserve hierarchy."],
            "implementation_rules": ["Keep text in HTML."],
        }
    )
    file_comm.write_layout_contract(
        {
            "viewport_targets": ["1440x900"],
            "regions": [{"id": "hero"}],
            "safe_zones": [],
            "forbidden_overlay_zones": [],
            "asset_fit": {"background_ui": "cover"},
            "responsive_rules": ["Keep controls visible."],
        }
    )
    file_comm.write_asset_manifest(
        {
            "assets": [{"id": "background_ui"}],
            "generation_records": [],
            "implementation_notes": ["Copy production assets."],
        }
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=1,
        sprint_num=1,
        mode="generate",
    )

    assert "- .harness/design/design_brief.json" in captured["prompt"]
    assert "- .harness/design/layout_contract.json" in captured["prompt"]
    assert "- .harness/design/asset_manifest.json" in captured["prompt"]
    assert "Design Stage Guidance:" in captured["prompt"]
    assert "Copy required production assets from `.harness/design/`" in captured["prompt"]
    assert "centered card grid" in captured["prompt"]


@pytest.mark.anyio
async def test_generator_includes_design_stage_reads_in_repair_mode(
    monkeypatch, tmp_path: Path
):
    file_comm = FileComm(tmp_path / ".harness")
    _write_generator_context(file_comm)
    file_comm.write_feedback(1, "Fix reset interaction.")
    file_comm.write_grades(
        1,
        {
            "round": 1,
            "overall_passed": False,
            "criteria": {
                "design_quality": {"score": 6.0, "passed": True},
                "functionality": {"score": 5.0, "passed": False, "notes": "increment broken"},
                "originality": {"score": 5.0, "passed": True},
                "craft": {"score": 6.0, "passed": True},
            },
        },
    )
    file_comm.write_design_brief(
        {
            "requested_mode": "image-first",
            "visual_strategy": "text_only_fallback",
            "reference_files": {},
            "aesthetic_intent": {"design_hypothesis": "Use asymmetry."},
            "responsive_strategy": {"desktop": "Layered", "mobile": "Stacked"},
            "overlay_regions": [{"id": "hero"}],
            "visual_success_criteria": ["Preserve hierarchy."],
            "implementation_rules": ["Keep text in HTML."],
            "fallback_reason": "image_assets_unavailable",
        }
    )
    file_comm.write_layout_contract(
        {
            "viewport_targets": ["1440x900"],
            "regions": [{"id": "hero"}],
            "safe_zones": [],
            "forbidden_overlay_zones": [],
            "asset_fit": {},
            "responsive_rules": ["Keep controls visible."],
        }
    )
    file_comm.write_asset_manifest(
        {
            "assets": [],
            "generation_records": [],
            "implementation_notes": ["Copy production assets."],
        }
    )
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text("{}")
    captured: dict[str, str] = {}

    async def fake_run_sdk_agent(**kwargs):
        captured["prompt"] = kwargs["prompt"]
        return (
            ResultMessage(
                subtype="result",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="session",
                total_cost_usd=0.2,
                usage={"input_tokens": 100_000},
                result="done",
            ),
            0.2,
            "",
            [],
        )

    monkeypatch.setattr("src.agents.generator.run_sdk_agent", fake_run_sdk_agent)

    await run_generator(
        HarnessConfig(generator_model="claude-sonnet-4-6"),
        file_comm,
        tmp_path,
        round_num=2,
        sprint_num=1,
        mode="repair",
    )

    assert ".harness/design/design_brief.json" in captured["prompt"]
    assert ".harness/design/layout_contract.json" in captured["prompt"]
    assert ".harness/design/asset_manifest.json" in captured["prompt"]
    assert "The design stage fell back to text-only" in captured["prompt"]


def test_partial_source_uses_tool_enabled_generator(tmp_path):
    harness=tmp_path/".harness"; harness.mkdir()
    frontend=tmp_path/"frontend"; frontend.mkdir()
    (frontend/"app.js").write_text("const first = 1;\nfunction render() {}\n")
    context={"schema_version":"edit-context-v1","source_windows":[{"path":"frontend/app.js","start_line":1,"end_line":1,"content":"const first = 1;\n"}]}
    (harness/"edit_context_round_1.json").write_text(json.dumps(context))
    (harness/"minimal_path_plan_round_1.json").write_text(json.dumps({"source_change_cone":{"local_paths":["frontend/app.js"],"initial_paths":["frontend/app.js"]}}))
    config=HarnessConfig(agent_runtime="openai")
    assert not _atomic_executor_eligible(config=config,workdir=tmp_path,round_num=1)
    context["source_windows"].append({"path":"frontend/app.js","start_line":2,"end_line":2,"content":"function render() {}\n"})
    (harness/"edit_context_round_1.json").write_text(json.dumps(context))
    assert _atomic_executor_eligible(config=config,workdir=tmp_path,round_num=1)


def test_supplied_line_operations_reach_browser_without_html_heuristics():
    source = '<main>\n<section>content</section>\n</main>\n'
    digest = hashlib.sha256(source.encode()).hexdigest()
    operation = {"op": "replace_lines", "path": "frontend/index.html",
                 "file_sha256": digest, "start_line": 2, "end_line": 2,
                 "replacement": '<section>new content'}
    updated, _ = _apply_sha_line_operations(
        source, [operation], expected_path="frontend/index.html",
        expected_sha256=digest, preserve_operations=True,
    )
    assert updated == '<main>\n<section>new content\n</main>\n'
    with pytest.raises(ValueError, match="SHA mismatch"):
        _apply_sha_line_operations(source, [operation], expected_path="frontend/index.html",
                                   expected_sha256="outdated", preserve_operations=True)


def test_product_patch_hashes_are_bound_to_supplied_source_revision():
    source = '<main>Before</main>\n'
    digest = hashlib.sha256(source.encode()).hexdigest()
    operations = _normalize_atomic_operations({"operations":[{
        "op":"replace_lines", "path":"index.html", "file_sha256":"model-typo",
        "start_line":1, "end_line":1, "replacement":"<main>After</main>"}]},
        {"frontend/index.html":digest})
    assert operations[0]["file_sha256"] == digest
    updated, _ = _apply_sha_line_operations(source, operations, expected_path="frontend/index.html",
                                            expected_sha256=digest, preserve_operations=True)
    assert 'After' in updated
    with pytest.raises(ValueError, match="SHA mismatch"):
        _apply_sha_line_operations(source + '<footer/>', operations, expected_path="frontend/index.html",
                                  expected_sha256=digest, preserve_operations=True)


def test_atomic_executor_accepts_nested_exact_patches_and_rejects_conflicting_path():
    payload = {'operations': [{'op': 'patches', 'path': 'game.js', 'patches': [
        {'old_text': 'before', 'new_text': 'after'},
        {'search': 'second', 'replace': 'third'},
    ]}]}
    assert _normalize_atomic_patch_response(payload) == [
        {'path': 'frontend/game.js', 'old_text': 'before', 'new_text': 'after'},
        {'path': 'frontend/game.js', 'old_text': 'second', 'new_text': 'third'},
    ]
    payload['operations'][0]['patches'][0]['path'] = 'other.js'
    with pytest.raises(ValueError, match='conflicts'):
        _normalize_atomic_patch_response(payload)


def test_skill_exact_schema_uses_copy_and_search_replace_without_line_numbers():
    from src.agents.generator import _atomic_exact_response_format
    schema=_atomic_exact_response_format('generate')['json_schema']['schema']
    fields=schema['properties']
    assert fields['operations']['items']['properties']['op']['enum']==['copy_from']
    assert set(fields['patches']['items']['required'])=={'path','old_text','new_text'}
    assert fields['operations']['items']['properties']['line_edits']['maxItems']==0
    assert schema['additionalProperties'] is False
    assert 'repair_task_descriptions' in _atomic_exact_response_format('repair')['json_schema']['schema']['required']


def test_exact_patch_recovers_only_unique_whole_line_indentation():
    old,new=_uniquify_first_exact_patch('  activate();\n', '   activate();', '   openPanel();')
    assert '  activate();\n'.replace(old,new)=='  openPanel();\n'
    with pytest.raises(ValueError,match='absent'):
        _uniquify_first_exact_patch('if (ready) activate();\n','   activate();','   openPanel();')
    with pytest.raises(ValueError,match='absent'):
        _uniquify_first_exact_patch('  activate();\n  activate();\n','   activate();','   openPanel();')


def test_exact_patch_recovers_a_unique_section_comment_delimiter_only():
    source='/* ===== SECTION ===== */\n.rule { color: red; }\n'
    old,new=_uniquify_first_exact_patch(source,'/* ===== SECTION =====\n','.added { color: blue; }\n')
    assert source.replace(old,new)=='.added { color: blue; }\n.rule { color: red; }\n'
    with pytest.raises(ValueError,match='absent'):
        _uniquify_first_exact_patch(source+source,'/* ===== SECTION =====\n','x')


def test_exact_patch_recovers_unique_multiline_indentation_without_code_fuzzing():
    source = '  <section>\n    <p>Original</p>\n  </section>\n'
    old, new = _uniquify_first_exact_patch(source, '<section>\n  <p>Original</p>\n</section>', '<section>Updated</section>')
    assert old == source.rstrip('\n')
    assert new == '<section>Updated</section>'
    with pytest.raises(ValueError, match='absent'):
        _uniquify_first_exact_patch(source + source, '<section>\n<p>Original</p>\n</section>', 'x')
    with pytest.raises(ValueError, match='absent'):
        _uniquify_first_exact_patch(source, '<section>\n<p>Different</p>\n</section>', 'x')


def test_html_nesting_diagnostic_ignores_void_self_closing_tags():
    from src.agents.generator import _HTMLBalanceParser
    parser = _HTMLBalanceParser()
    parser.feed('<html><head><meta charset="utf-8"/><link href="x.css"/></head><body><section><input/><div>Text</div></section></body></html>')
    assert parser.error_score == 0
    assert parser.issues == []
    broken = _HTMLBalanceParser()
    broken.feed('<html><body><div>\n</section></div></body></html>')
    assert broken.issues[0] == 'line 2: </section> while the open element is <div>'
