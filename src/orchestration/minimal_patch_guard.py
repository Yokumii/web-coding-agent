"""Counterfactual certificates for minimal edit and repair patches.

The guard treats a candidate patch as a set of ordered, exact change atoms.  It
does not infer minimality from line counts or an agent self-report.  Instead it
replays subsets against an external oracle with two independent obligations:

* the requested target behavior still passes; and
* the protected UI frame still passes.

An accepted certificate is one-minimal: deleting any surviving atom makes at
least one obligation fail.  Infrastructure failures are never accepted as
evidence that an atom was necessary.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import math
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


OracleStatus = Literal["ok", "candidate_failed", "infrastructure_error"]


@dataclass(frozen=True)
class AtomicPatch:
    """One exact, ordered source transformation."""

    change_id: str
    path: str
    search: str
    replace: str

    def payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class OracleOutcome:
    """Result of executing target and preservation contracts on one subset."""

    status: OracleStatus
    target_passed: bool
    preservation_passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def fully_passed(self) -> bool:
        return (
            self.status == "ok"
            and self.target_passed
            and self.preservation_passed
        )


PatchOracle = Callable[[tuple[str, ...]], Awaitable[OracleOutcome]]


def patch_set_fingerprint(patches: Iterable[AtomicPatch]) -> str:
    payload = [patch.payload() for patch in patches]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def apply_atomic_patches(
    source: Mapping[str, str],
    patches: Iterable[AtomicPatch],
    kept_change_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, str]:
    """Apply an ordered patch subset, rejecting ambiguous or stale searches."""
    output = dict(source)
    selected = None if kept_change_ids is None else set(kept_change_ids)
    for patch in patches:
        if selected is not None and patch.change_id not in selected:
            continue
        current = output.get(patch.path)
        if patch.search == "":
            if current is not None:
                raise ValueError(
                    f"file-creation patch targets an existing path: {patch.path}"
                )
            output[patch.path] = patch.replace
            continue
        if current is None:
            raise ValueError(f"patch targets a missing path: {patch.path}")
        occurrences = current.count(patch.search)
        if occurrences != 1:
            raise ValueError(
                f"patch search must match exactly once: {patch.path} "
                f"({occurrences} matches)"
            )
        output[patch.path] = current.replace(patch.search, patch.replace, 1)
    return output


def build_atomic_patches(
    source: Mapping[str, str],
    destination: Mapping[str, str],
    *,
    context_lines: int = 1,
) -> list[AtomicPatch]:
    """Decompose a code transition into ordered, replayable exact hunks."""
    deleted = sorted(set(source) - set(destination))
    if deleted:
        raise ValueError(f"minimality guard does not support deleted files: {deleted}")
    raw: list[tuple[str, str, str]] = []
    for path in sorted(destination):
        before = source.get(path)
        after = destination[path]
        if before == after:
            continue
        if before is None:
            raw.append((path, "", after))
            continue
        old_lines = before.splitlines(keepends=True)
        new_lines = after.splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        file_changes: list[tuple[str, str]] = []
        for group in matcher.get_grouped_opcodes(n=context_lines):
            old_start, old_end = group[0][1], group[-1][2]
            new_start, new_end = group[0][3], group[-1][4]
            search = "".join(old_lines[old_start:old_end])
            replace = "".join(new_lines[new_start:new_end])
            if not search or before.count(search) != 1:
                file_changes = [(before, after)]
                break
            file_changes.append((search, replace))
        if not file_changes:
            file_changes = [(before, after)]
        raw.extend((path, search, replace) for search, replace in file_changes)
    return [
        AtomicPatch(
            change_id=f"p{index:03d}",
            path=path,
            search=search,
            replace=replace,
        )
        for index, (path, search, replace) in enumerate(raw, start=1)
    ]


def _certificate_base(patches: list[AtomicPatch]) -> dict[str, Any]:
    return {
        "schema_version": "counterfactual-patch-certificate-v1",
        "patch_fingerprint": patch_set_fingerprint(patches),
        "original_change_ids": [patch.change_id for patch in patches],
        "kept_change_ids": [],
        "redundant_change_ids": [],
        "necessity": [],
        "oracle_attempts": [],
    }


def _attempt_payload(change_ids: tuple[str, ...], outcome: OracleOutcome) -> dict[str, Any]:
    return {
        "kept_change_ids": list(change_ids),
        "status": outcome.status,
        "target_passed": outcome.target_passed,
        "preservation_passed": outcome.preservation_passed,
        "evidence": outcome.evidence,
    }


async def certify_patch_minimality(
    patches: Iterable[AtomicPatch],
    oracle: PatchOracle,
    *,
    max_atoms: int = 12,
) -> dict[str, Any]:
    """Return a deterministic counterfactual certificate for ``patches``.

    The source and full destination are checked first.  A ddmin-style pass then
    removes groups, followed by an exhaustive one-atom deletion pass.  The
    original destination is certified only if no atom can be removed.
    """
    ordered = list(patches)
    certificate = _certificate_base(ordered)
    ids = [patch.change_id for patch in ordered]
    if len(ids) != len(set(ids)):
        certificate.update(status="invalid_patch", reason="duplicate_change_ids")
        return certificate
    if not ordered:
        certificate.update(status="not_applicable", reason="empty_patch")
        return certificate
    if len(ordered) > max_atoms:
        certificate.update(
            status="inconclusive",
            reason="too_many_atomic_changes",
            max_atoms=max_atoms,
            atom_count=len(ordered),
        )
        return certificate

    cache: dict[tuple[str, ...], OracleOutcome] = {}

    async def evaluate(candidate: list[str] | tuple[str, ...]) -> OracleOutcome:
        selected = set(candidate)
        key = tuple(change_id for change_id in ids if change_id in selected)
        if key not in cache:
            cache[key] = await oracle(key)
            certificate["oracle_attempts"].append(_attempt_payload(key, cache[key]))
        return cache[key]

    source = await evaluate(())
    if source.status == "infrastructure_error":
        certificate.update(
            status="inconclusive", reason="source_oracle_infrastructure_error"
        )
        return certificate
    if source.target_passed:
        certificate.update(
            status="invalid_contract",
            reason="source_already_satisfies_target_contract",
        )
        return certificate
    if not source.preservation_passed:
        certificate.update(
            status="invalid_contract",
            reason="source_violates_preservation_contract",
        )
        return certificate

    full = await evaluate(tuple(ids))
    if full.status == "infrastructure_error":
        certificate.update(
            status="inconclusive",
            reason="full_candidate_oracle_infrastructure_error",
        )
        return certificate
    if not full.fully_passed:
        certificate.update(
            status="candidate_failed",
            reason=(
                "full_candidate_failed_target_contract"
                if not full.target_passed
                else "full_candidate_failed_preservation_contract"
            ),
        )
        return certificate

    current = list(ids)
    granularity = 2
    while len(current) >= 2:
        chunk_size = math.ceil(len(current) / granularity)
        reduced = False
        for start in range(0, len(current), chunk_size):
            removed_chunk = set(current[start : start + chunk_size])
            candidate = [item for item in current if item not in removed_chunk]
            outcome = await evaluate(candidate)
            if outcome.fully_passed:
                current = candidate
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if reduced:
            continue
        if granularity >= len(current):
            break
        granularity = min(len(current), granularity * 2)

    # ddmin is one-minimal for deterministic predicates, but the explicit pass
    # makes the certificate self-evident and catches flaky/changed outcomes.
    index = 0
    while index < len(current):
        candidate = current[:index] + current[index + 1 :]
        outcome = await evaluate(candidate)
        if outcome.fully_passed:
            current = candidate
            index = 0
        else:
            index += 1

    necessity: list[dict[str, Any]] = []
    for change_id in current:
        candidate = [item for item in current if item != change_id]
        outcome = await evaluate(candidate)
        if outcome.status == "infrastructure_error":
            certificate.update(
                status="inconclusive",
                reason="necessity_oracle_infrastructure_error",
                kept_change_ids=current,
            )
            return certificate
        if outcome.fully_passed:
            certificate.update(
                status="inconclusive",
                reason="oracle_was_not_deterministic_during_necessity_check",
                kept_change_ids=current,
            )
            return certificate
        dimensions = []
        if not outcome.target_passed:
            dimensions.append("target")
        if not outcome.preservation_passed:
            dimensions.append("preservation")
        necessity.append(
            {
                "change_id": change_id,
                "failure_dimension": "+".join(dimensions) or "candidate",
                "counterfactual_kept_change_ids": candidate,
            }
        )

    redundant = [change_id for change_id in ids if change_id not in set(current)]
    certificate.update(
        status="certified" if not redundant else "non_minimal",
        reason=(
            "every_atomic_change_is_counterfactually_necessary"
            if not redundant
            else "original_candidate_contains_removable_changes"
        ),
        kept_change_ids=current,
        redundant_change_ids=redundant,
        necessity=necessity,
        atom_count=len(ids),
        minimal_atom_count=len(current),
    )
    return certificate


__all__ = [
    "AtomicPatch",
    "OracleOutcome",
    "apply_atomic_patches",
    "build_atomic_patches",
    "certify_patch_minimality",
    "patch_set_fingerprint",
]
