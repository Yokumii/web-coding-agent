from __future__ import annotations

from pathlib import Path

import pytest
from claude_agent_sdk.types import ResultMessage

from src.agents.generator import (
    _build_generator_prompt,
    _checkpoint_interrupted_model_work,
    _describe_failures,
    _is_scope_contract_only_repair,
    _is_harness_checkpoint_for_round,
    _validate_generator_commits,
    _validate_repair_scope,
    _validate_generator_runnable_files,
    _trace_confirms_commit,
    _trace_has_successful_validation,
    _trace_written_frontend_paths,
    run_generator,
)
from src.config import HarnessConfig
from src.orchestration.file_comm import FileComm
from src.prompts.generator import GENERATOR_SYSTEM_PROMPT
from src.agents._shared import expose_local_claude_skills


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
    assert "write `.harness/edit_scope_round_1.json`" not in prompt


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

    error = _validate_repair_scope(frontend, baseline)
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

    assert _validate_repair_scope(frontend, baseline) is None


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
