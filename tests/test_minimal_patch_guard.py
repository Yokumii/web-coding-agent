from __future__ import annotations

import pytest

from src.orchestration.minimal_patch_guard import (
    AtomicPatch,
    OracleOutcome,
    apply_atomic_patches,
    build_atomic_patches,
    certify_patch_minimality,
)


def _patch(change_id: str, search: str, replace: str) -> AtomicPatch:
    return AtomicPatch(
        change_id=change_id,
        path="index.html",
        search=search,
        replace=replace,
    )


def test_apply_atomic_patches_replays_an_ordered_subset():
    source = {"index.html": "<main>old</main>\n<footer>same</footer>\n"}
    patches = [
        _patch("p1", "old", "new"),
        _patch("p2", "same", "unchanged"),
    ]

    assert apply_atomic_patches(source, patches, {"p1"}) == {
        "index.html": "<main>new</main>\n<footer>same</footer>\n"
    }


def test_build_atomic_patches_uses_independent_local_hunks():
    source = {"app.js": "head\nold one\nkeep a\nkeep b\nold two\ntail\n"}
    destination = {"app.js": "head\nnew one\nkeep a\nkeep b\nnew two\ntail\n"}

    patches = build_atomic_patches(source, destination, context_lines=0)

    assert len(patches) == 2
    assert apply_atomic_patches(source, patches) == destination


@pytest.mark.anyio
async def test_certificate_rejects_an_original_patch_with_a_redundant_atom():
    patches = [_patch("required", "old", "new"), _patch("churn", "same", "pretty")]

    async def oracle(kept: tuple[str, ...]) -> OracleOutcome:
        return OracleOutcome(
            status="ok",
            target_passed="required" in kept,
            preservation_passed=True,
            evidence={"kept": list(kept)},
        )

    certificate = await certify_patch_minimality(patches, oracle)

    assert certificate["status"] == "non_minimal"
    assert certificate["kept_change_ids"] == ["required"]
    assert certificate["redundant_change_ids"] == ["churn"]
    assert certificate["necessity"][0]["change_id"] == "required"


@pytest.mark.anyio
async def test_certificate_accepts_when_target_and_frame_need_every_atom():
    patches = [_patch("target", "old", "new"), _patch("frame", "same", "safe")]

    async def oracle(kept: tuple[str, ...]) -> OracleOutcome:
        return OracleOutcome(
            status="ok",
            target_passed="target" in kept,
            preservation_passed="target" not in kept or "frame" in kept,
        )

    certificate = await certify_patch_minimality(patches, oracle)

    assert certificate["status"] == "certified"
    assert certificate["kept_change_ids"] == ["target", "frame"]
    assert {item["failure_dimension"] for item in certificate["necessity"]} == {
        "target",
        "preservation",
    }


@pytest.mark.anyio
async def test_certificate_rejects_a_non_discriminating_target_contract():
    patches = [_patch("p1", "old", "new")]

    async def oracle(_kept: tuple[str, ...]) -> OracleOutcome:
        return OracleOutcome(status="ok", target_passed=True, preservation_passed=True)

    certificate = await certify_patch_minimality(patches, oracle)

    assert certificate["status"] == "invalid_contract"
    assert certificate["reason"] == "source_already_satisfies_target_contract"


@pytest.mark.anyio
async def test_infrastructure_error_never_counts_as_patch_necessity():
    patches = [_patch("p1", "old", "new")]

    async def oracle(kept: tuple[str, ...]) -> OracleOutcome:
        if not kept:
            return OracleOutcome(status="ok", target_passed=False, preservation_passed=True)
        return OracleOutcome(
            status="infrastructure_error",
            target_passed=False,
            preservation_passed=False,
            evidence={"error": "browser unavailable"},
        )

    certificate = await certify_patch_minimality(patches, oracle)

    assert certificate["status"] == "inconclusive"
    assert certificate["reason"] == "full_candidate_oracle_infrastructure_error"
