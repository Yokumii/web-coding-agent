from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_g2_compound_batch.py"
SPEC = importlib.util.spec_from_file_location("g2_compound_batch", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_selection_round_robins_task_count_buckets():
    plan = {
        "jobs": [
            {"instance_id": f"{count}-{index}", "task_count": count, "case": "x"}
            for count in (4, 5, 6, 7)
            for index in range(8)
        ]
    }

    selected = MODULE._select_jobs(plan, 20)

    assert len(selected) == 20
    assert {
        count: sum(item["task_count"] == count for item in selected)
        for count in (4, 5, 6, 7)
    } == {4: 5, 5: 5, 6: 5, 7: 5}


def test_source_observation_uses_first_real_page_route():
    from scripts.run_g2_compound_edit import _source_observation_url

    assert _source_observation_url(
        "http://127.0.0.1:19000",
        {"pages": [{"route": "/catalog.html"}]},
    ) == "http://127.0.0.1:19000/catalog.html"
    assert _source_observation_url("http://127.0.0.1:19000/", {"pages": []}).endswith("/")


def test_openai_profile_routes_all_roles_to_luna(monkeypatch):
    from scripts.run_g2_compound_edit import _provider_config

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = SimpleNamespace(
        provider_profile="openai",
        model="gpt-5.6-luna",
        review_model="gpt-5.6-luna",
        budget_usd=5,
        request_timeout=120,
        port=19000,
        debug_max_rounds=8,
    )

    config = _provider_config(args)

    assert config.planner_model == "gpt-5.6-luna"
    assert config.generator_model == "gpt-5.6-luna"
    assert config.evaluator_model == "gpt-5.6-luna"
    assert config.evaluator_vision_model == "gpt-5.6-luna"
    assert config.openai_base_url == "https://api.nju-link.com/v1"
    assert config.openai_wire_api == "responses"
    assert config.openai_stream_read_retries == 2
    assert config.minimal_path_guidance_enabled is True
    assert config.lightweight_edit_production is False
    assert config.edit_max_rounds == 9
    assert config.openai_extra_headers == {
        "x-openai-actor-authorization": "local-image-extension"
    }


def test_production_default_allows_ten_repairs(monkeypatch):
    from scripts.run_g2_compound_edit import _provider_config, parser

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    args = parser().parse_args(["--case", "case.json", "--output", "output"])
    args.provider_profile = "openai"

    assert args.debug_max_rounds == 10
    assert _provider_config(args).edit_max_rounds == 11


def test_source_observation_skips_a_surface_redirected_during_navigation():
    from scripts.run_g2_compound_edit import _navigate_source_surface

    class Page:
        url = "http://127.0.0.1:19000/index.html"

        async def goto(self, *_args, **_kwargs):
            raise RuntimeError("Navigation is interrupted by another navigation")

        async def wait_for_load_state(self, *_args, **_kwargs):
            return None

    assert not asyncio.run(
        _navigate_source_surface(Page(), "http://127.0.0.1:19000/watchlist.html")
    )


def test_source_observation_keeps_the_requested_surface():
    from scripts.run_g2_compound_edit import _navigate_source_surface

    class Page:
        url = "http://127.0.0.1:19000/catalog.html#filters"

        async def goto(self, *_args, **_kwargs):
            return None

    assert asyncio.run(
        _navigate_source_surface(Page(), "http://127.0.0.1:19000/catalog.html#filters")
    )


def test_response_stream_usage_counts_each_completed_request_once(tmp_path):
    stream = tmp_path / "response_stream.jsonl"
    stream.write_text("\n".join([
        '{"event":"terminal_event","usage":{"input_tokens":10,"output_tokens":4}}',
        '{"event":"request_completed","usage":{"input_tokens":10,"output_tokens":4}}',
        '{"event":"request_completed","usage":{"prompt_tokens":7,"completion_tokens":3}}',
        '{"event":"request_completed"}',
        'truncated',
    ]))

    assert MODULE._response_stream_usage(stream) == {
        "input_tokens": 17,
        "output_tokens": 7,
        "calls": 3,
        "unknown_usage_calls": 1,
    }


def test_cancelled_worker_is_recorded_without_orphaning_process(monkeypatch, tmp_path):
    class Process:
        pid = 12345
        returncode = -15
        polls = 0

        def poll(self):
            self.polls += 1
            return None if self.polls == 1 else self.returncode

        def wait(self, timeout=None):
            return self.returncode

    process = Process()
    monkeypatch.setattr(MODULE.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(MODULE.os, "killpg", lambda *args: None)
    MODULE.CANCELLED.clear()
    def cancel(_timeout):
        MODULE.CANCELLED.set()
        return True
    monkeypatch.setattr(MODULE.CANCELLED, "wait", cancel)
    args = SimpleNamespace(
        fast_gt=False,
        output=tmp_path,
        model="gpt-5.6-luna",
        base_port=20000,
        budget_usd=20,
        request_timeout=600,
        case_timeout=60,
        debug_max_rounds=8,
        provider_profile="njulink-degraded",
    )

    result = MODULE._run_one(
        args,
        0,
        {"instance_id": "case", "case": str(tmp_path / "case.json")},
    )

    assert result["status"] == "cancelled"
    MODULE.CANCELLED.clear()
