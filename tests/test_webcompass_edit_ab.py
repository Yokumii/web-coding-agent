import inspect
from pathlib import Path

from scripts.run_webcompass_edit_ab import (
    actual_harness_source_exposure,
    apply_exact_patches,
    finalize_arm,
    make_harness_arm_runner,
    normalize_model_patches,
    shared_browser_checks,
)
from src.config import HarnessConfig


def test_model_patch_parser_and_exact_replay(tmp_path: Path):
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "index.html").write_text("<main>before</main>\n", encoding="utf-8")
    patches = normalize_model_patches(
        {
            "patches": [
                {
                    "path": "index.html",
                    "search": "<main>before</main>",
                    "replace": "<main>after</main>",
                }
            ]
        }
    )

    apply_exact_patches(frontend, patches)

    assert (frontend / "index.html").read_text(encoding="utf-8") == "<main>after</main>\n"


def test_shared_browser_contract_checks_actual_target_visibility_for_both_defaults():
    checks = shared_browser_checks()

    assert {item["id"] for item in checks} == {
        "TRANSIT-INITIAL-HIDDEN",
        "TRANSIT-INITIAL-VISIBLE",
    }
    assert all(
        any(
            action["action"] == "assert_hidden"
            and "Public Transit" in action["selector"]
            for action in item["actions"]
        )
        for item in checks
    )


def test_harness_runner_selection_always_returns_an_awaitable(tmp_path: Path):
    for completed in (None, tmp_path / "prior"):
        runner = make_harness_arm_runner(
            row={},
            run_dir=tmp_path,
            config=HarnessConfig(),
            prior_plan_run=tmp_path / "plan",
            prior_completed_run=completed,
        )
        awaitable = runner()
        assert inspect.isawaitable(awaitable)
        awaitable.close()


def test_finalizer_distinguishes_semantic_success_from_reference_identity(tmp_path: Path):
    source = tmp_path / "source"
    candidate = tmp_path / "candidate"
    reference = tmp_path / "reference"
    for root, text in (
        (source, "<button>Show</button>"),
        (candidate, "<button aria-expanded='true'>Show</button>"),
        (reference, "<button data-open='true'>Show</button>"),
    ):
        root.mkdir()
        (root / "index.html").write_text(text, encoding="utf-8")

    result = finalize_arm(
        {"status": "ok", "browser_passed": True},
        source=source,
        candidate=candidate,
        reference=reference,
    )

    assert result["success"] is True
    assert result["exact_reference_match"] is False
    assert result["unrelated_changed_paths"] == []


def test_actual_exposure_unions_preloaded_outline_and_focused_reads(tmp_path: Path):
    frontend = tmp_path / "frontend"
    harness = tmp_path / ".harness"
    trace = harness / "traces"
    frontend.mkdir()
    trace.mkdir(parents=True)
    (frontend / "index.html").write_text(
        "".join(f"line {index}\n" for index in range(1, 101)), encoding="utf-8"
    )
    (harness / "edit_context_round_1.json").write_text(
        '{"source_windows":[{"path":"frontend/index.html","start_line":1,"end_line":2}],'
        '"source_outlines":[{"path":"frontend/index.html","entries":[{"line":80,"content":"line 80"}]}]}',
        encoding="utf-8",
    )
    (trace / "generator_round_1.jsonl").write_text(
        '{"event":"assistant","message":{"tool_calls":[{"function":{"name":"read_file",'
        '"arguments":"{\\"path\\":\\"frontend/index.html\\",\\"start_line\\":78,\\"end_line\\":82}"}}]}}\n',
        encoding="utf-8",
    )

    exposure = actual_harness_source_exposure(tmp_path)

    assert exposure["ratio"] < 0.1
    assert exposure["paths"] == ["frontend/index.html"]
