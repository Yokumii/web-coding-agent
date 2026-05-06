from __future__ import annotations

import asyncio

import pytest

from src.bench.concurrent_runner import WorkerPortPool


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_port_pool_serves_distinct_ports() -> None:
    pool = WorkerPortPool(base=5173, size=4)
    seen = {await pool.acquire() for _ in range(4)}
    assert seen == {5173, 5174, 5175, 5176}


@pytest.mark.anyio
async def test_port_pool_blocks_when_exhausted_until_release() -> None:
    pool = WorkerPortPool(base=5173, size=2)
    a = await pool.acquire()
    b = await pool.acquire()
    # Now drained; the next acquire should block.
    blocked = asyncio.create_task(pool.acquire())
    await asyncio.sleep(0.05)
    assert not blocked.done()
    pool.release(a)
    got = await asyncio.wait_for(blocked, timeout=1.0)
    assert got == a
    pool.release(b)
    pool.release(got)


def test_port_pool_rejects_size_zero_or_negative() -> None:
    with pytest.raises(ValueError):
        WorkerPortPool(base=5173, size=0)
    with pytest.raises(ValueError):
        WorkerPortPool(base=5173, size=-1)


import json

from src.bench.manifest import (
    HarnessRecord,
    Manifest,
    ManifestStore,
    SampleRecord,
)


def _make_manifest(ids: list[str]) -> Manifest:
    samples = [
        SampleRecord(id=i, instruction=f"build {i}",
                     harness=HarnessRecord(workdir=f"samples/{i}"))
        for i in ids
    ]
    return Manifest(
        run_id="r1",
        created_at="2026-05-06T00:00:00",
        jsonl_source="t.jsonl",
        strata=None,
        samples=samples,
    )


@pytest.mark.anyio
async def test_run_harness_phase_assigns_distinct_ports_within_range(
    tmp_path, monkeypatch,
) -> None:
    from src.bench.concurrent_runner import run_harness_phase

    manifest = _make_manifest(["000001", "000002", "000003", "000004"])
    store = ManifestStore(tmp_path / "manifest.json")

    seen_ports: list[int] = []

    async def fake_arun(sample, *, run_dir, project_root, extra_args, log_dir):
        port_idx = extra_args.index("--frontend-port")
        seen_ports.append(int(extra_args[port_idx + 1]))
        # Yield to let other tasks run before returning, so we exercise true
        # concurrency rather than serial completion.
        await asyncio.sleep(0.01)
        return HarnessRecord(
            status="completed", workdir=f"samples/{sample.id}",
            last_verdict="completed", rounds=1, cost_usd=2.0,
        )

    monkeypatch.setattr("src.bench.concurrent_runner.arun_harness_for_sample", fake_arun)

    await run_harness_phase(
        manifest, store,
        concurrency=4,
        frontend_port_base=5173,
        run_dir=tmp_path,
        project_root=tmp_path,
        extra_args=[],
        log_dir=tmp_path / "logs",
    )

    assert sorted(seen_ports) == [5173, 5174, 5175, 5176]
    for s in manifest.samples:
        assert s.harness.status == "completed"
        assert s.harness.cost_usd == 2.0


@pytest.mark.anyio
async def test_run_harness_phase_reuses_ports_after_release(tmp_path, monkeypatch) -> None:
    from src.bench.concurrent_runner import run_harness_phase

    manifest = _make_manifest([f"00000{i}" for i in range(1, 7)])  # 6 samples
    store = ManifestStore(tmp_path / "manifest.json")

    seen_ports: list[int] = []

    async def fake_arun(sample, *, run_dir, project_root, extra_args, log_dir):
        port_idx = extra_args.index("--frontend-port")
        seen_ports.append(int(extra_args[port_idx + 1]))
        await asyncio.sleep(0.01)
        return HarnessRecord(
            status="completed", workdir=f"samples/{sample.id}",
            last_verdict="completed", rounds=1, cost_usd=1.0,
        )

    monkeypatch.setattr("src.bench.concurrent_runner.arun_harness_for_sample", fake_arun)

    await run_harness_phase(
        manifest, store,
        concurrency=2, frontend_port_base=5173,
        run_dir=tmp_path, project_root=tmp_path,
        extra_args=[], log_dir=tmp_path / "logs",
    )

    # 6 samples on 2 ports → each port used at least twice
    assert len(seen_ports) == 6
    assert set(seen_ports) == {5173, 5174}
    assert seen_ports.count(5173) >= 2
    assert seen_ports.count(5174) >= 2


@pytest.mark.anyio
async def test_run_harness_phase_save_count_is_one_plus_n(tmp_path, monkeypatch) -> None:
    """Spec contract: store.save() called exactly 1 (dispatch) + N (per completion)."""
    from src.bench.concurrent_runner import run_harness_phase

    manifest = _make_manifest(["000001", "000002", "000003"])
    store = ManifestStore(tmp_path / "manifest.json")

    save_count = {"n": 0}
    real_save = store.save
    def counting_save(m):
        save_count["n"] += 1
        real_save(m)
    monkeypatch.setattr(store, "save", counting_save)

    async def fake_arun(sample, **kw):
        return HarnessRecord(
            status="completed", workdir=f"samples/{sample.id}",
            last_verdict="completed", rounds=1, cost_usd=1.0,
        )
    monkeypatch.setattr("src.bench.concurrent_runner.arun_harness_for_sample", fake_arun)

    await run_harness_phase(
        manifest, store,
        concurrency=2, frontend_port_base=5173,
        run_dir=tmp_path, project_root=tmp_path,
        extra_args=[], log_dir=tmp_path / "logs",
    )

    assert save_count["n"] == 1 + 3


@pytest.mark.anyio
async def test_run_harness_phase_skips_already_completed_samples(
    tmp_path, monkeypatch,
) -> None:
    from src.bench.concurrent_runner import run_harness_phase

    manifest = _make_manifest(["000001", "000002"])
    manifest.samples[0].harness.status = "completed"
    store = ManifestStore(tmp_path / "manifest.json")

    dispatched: list[str] = []

    async def fake_arun(sample, **kw):
        dispatched.append(sample.id)
        return HarnessRecord(
            status="completed", workdir=f"samples/{sample.id}",
            last_verdict="completed", rounds=1, cost_usd=1.0,
        )
    monkeypatch.setattr("src.bench.concurrent_runner.arun_harness_for_sample", fake_arun)

    await run_harness_phase(
        manifest, store,
        concurrency=2, frontend_port_base=5173,
        run_dir=tmp_path, project_root=tmp_path,
        extra_args=[], log_dir=tmp_path / "logs",
    )

    assert dispatched == ["000002"]
