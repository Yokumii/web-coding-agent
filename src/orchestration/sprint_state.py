from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.orchestration.file_comm import FileComm


@dataclass
class SprintRunContext:
    """当前或指定 sprint 的运行视图，供 agent prompt 与评估逻辑复用。"""

    sprint_num: int
    sprint_context: dict[str, Any]
    accepted_sprints: dict[str, Any]
    features: list[dict[str, Any]]
    ui_checks: list[dict[str, Any]]
    exit_criterion_map: list[dict[str, Any]]


@dataclass
class SprintState:
    """维护 sprint 推进状态，并同步落盘到 `.harness`。"""

    file_comm: FileComm
    current_target: int
    accepted: list[int] = field(default_factory=list)
    last_evaluated_round: int = 0
    total_sprints: int = 0

    @classmethod
    def load(cls, file_comm: FileComm) -> SprintState:
        accepted_payload = file_comm.read_accepted_sprints() or {
            "accepted": [],
            "current_target": 1,
            "last_evaluated_round": 0,
        }
        sprint_plan = file_comm.read_sprint_plan() or {}
        if isinstance(sprint_plan.get("total_sprints"), int):
            total = sprint_plan["total_sprints"]
        else:
            total = len(sprint_plan.get("sprints", []))
        return cls(
            file_comm=file_comm,
            current_target=int(accepted_payload.get("current_target", 1)),
            accepted=list(accepted_payload.get("accepted", [])),
            last_evaluated_round=int(accepted_payload.get("last_evaluated_round", 0)),
            total_sprints=total,
        )

    # ---- 读取能力 ----

    def sprint_context(self, sprint_num: int) -> dict[str, Any]:
        sprint_plan = self.file_comm.read_sprint_plan() or {}
        for sprint in sprint_plan.get("sprints", []):
            if sprint.get("number") == sprint_num:
                return sprint
        return {}

    def feature_ids_for_sprint(self, sprint_num: int) -> set[str]:
        return {
            str(fid) for fid in self.sprint_context(sprint_num).get("feature_ids", [])
        }

    def sprint_run_context(self, sprint_num: int) -> SprintRunContext:
        sprint_context = self.sprint_context(sprint_num)
        return SprintRunContext(
            sprint_num=sprint_num,
            sprint_context=sprint_context,
            accepted_sprints=self.accepted_payload(),
            features=self.features_for_sprint(sprint_num),
            ui_checks=self.ui_checks_for_sprint(sprint_num),
            exit_criterion_map=self.exit_criterion_feature_map(
                sprint_num,
                sprint_context,
            ),
        )

    def current_run_context(self) -> SprintRunContext:
        return self.sprint_run_context(self.current_target)

    def required_run_context(self, sprint_num: int, *, owner: str) -> SprintRunContext:
        accepted_sprints = self.file_comm.read_accepted_sprints()
        if accepted_sprints is None:
            raise RuntimeError(
                f"{owner} requires .harness/accepted_sprints.json, but it was not found."
            )

        sprint_context = self.required_sprint_context(sprint_num, owner=owner)
        return SprintRunContext(
            sprint_num=sprint_num,
            sprint_context=sprint_context,
            accepted_sprints=accepted_sprints,
            features=self.features_for_sprint(sprint_num),
            ui_checks=self.ui_checks_for_sprint(sprint_num),
            exit_criterion_map=self.exit_criterion_feature_map(
                sprint_num,
                sprint_context,
            ),
        )

    def required_sprint_context(self, sprint_num: int, *, owner: str) -> dict[str, Any]:
        sprint_plan = self.file_comm.read_sprint_plan()
        if sprint_plan is None:
            raise RuntimeError(
                f"{owner} requires .harness/sprint_plan.json, but it was not found."
            )

        sprints = sprint_plan.get("sprints")
        if not isinstance(sprints, list):
            raise RuntimeError(
                f"{owner} found invalid .harness/sprint_plan.json: sprints must be an array."
            )

        for sprint in sprints:
            if isinstance(sprint, dict) and sprint.get("number") == sprint_num:
                return sprint

        raise RuntimeError(
            f"{owner} could not find sprint {sprint_num} in .harness/sprint_plan.json."
        )

    def accepted_payload(self) -> dict[str, Any]:
        return {
            "accepted": list(self.accepted),
            "current_target": self.current_target,
            "last_evaluated_round": self.last_evaluated_round,
        }

    def features_for_sprint(self, sprint_num: int) -> list[dict[str, Any]]:
        feature_list = self.file_comm.read_feature_list() or {}
        features = feature_list.get("features", [])
        return [
            feature
            for feature in features
            if isinstance(feature, dict) and feature.get("sprint") == sprint_num
        ]

    def ui_checks_for_sprint(self, sprint_num: int) -> list[dict[str, Any]]:
        verification_plan = self.file_comm.read_ui_verification_plan() or {}
        for sprint in verification_plan.get("sprints", []):
            if sprint.get("sprint") == sprint_num:
                checks = sprint.get("checks", [])
                return [check for check in checks if isinstance(check, dict)]
        return []

    def exit_criterion_feature_map(
        self,
        sprint_num: int,
        sprint_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if sprint_context is None:
            sprint_context = self.sprint_context(sprint_num)
        exit_criteria = sprint_context.get("exit_criteria", [])
        sprint_features = self.features_for_sprint(sprint_num)
        feature_ids = [
            str(feature.get("id")) for feature in sprint_features if feature.get("id")
        ]
        single_feature_id = feature_ids[0] if len(feature_ids) == 1 else ""

        mappings: list[dict[str, Any]] = []
        for index, criterion in enumerate(exit_criteria, start=1):
            criterion_text = str(criterion).strip()
            if not criterion_text:
                continue

            matched_feature_id = ""
            for feature in sprint_features:
                acceptance = feature.get("acceptance_criteria", [])
                if criterion_text in [str(item).strip() for item in acceptance]:
                    matched_feature_id = str(feature.get("id", "")).strip()
                    break

            if not matched_feature_id and single_feature_id:
                matched_feature_id = single_feature_id

            mappings.append(
                {
                    "criterion_id": f"EXIT-{sprint_num:02d}-{index:02d}",
                    "feature_id": matched_feature_id or "unknown",
                    "criterion": criterion_text,
                    "critical": True,
                }
            )

        return mappings

    # ---- 状态修改 ----

    def _accepts_current_sprint(self, recommendation: str) -> bool:
        return recommendation in {"generate_next_sprint", "complete"}

    def _build_accepted_payload(
        self, *, sprint_num: int, round_num: int, recommendation: str
    ) -> dict[str, Any]:
        accepted = list(self.accepted)
        current_target = self.current_target
        if self._accepts_current_sprint(recommendation):
            if sprint_num not in accepted:
                accepted.append(sprint_num)
                accepted.sort()
            current_target = sprint_num + 1
        return {
            "accepted": accepted,
            "current_target": current_target,
            "last_evaluated_round": round_num,
        }

    def compute_advance(self, *, sprint_num: int, round_num: int, recommendation: str) -> dict[str, Any]:
        """预计算 `advance()` 将写入的 accepted_sprints 内容。"""
        return self._build_accepted_payload(
            sprint_num=sprint_num,
            round_num=round_num,
            recommendation=recommendation,
        )

    def advance(
        self,
        *,
        sprint_num: int,
        round_num: int,
        recommendation: str,
    ) -> None:
        payload = self._build_accepted_payload(
            sprint_num=sprint_num,
            round_num=round_num,
            recommendation=recommendation,
        )
        self.accepted = list(payload["accepted"])
        self.current_target = int(payload["current_target"])
        self.last_evaluated_round = int(payload["last_evaluated_round"])
        self._persist_accepted(payload)

    def mark_sprint_in_progress(self, sprint_num: int) -> None:
        self._update_feature_statuses(sprint_num, status="in_progress")

    def mark_sprint_outcome(
        self,
        sprint_num: int,
        *,
        recommendation: str,
        grades: dict[str, Any],
    ) -> None:
        target_ids = self.feature_ids_for_sprint(sprint_num)
        if not target_ids:
            return
        feature_list = self.file_comm.read_feature_list()
        if not feature_list:
            return
        failing = self._collect_failing_feature_ids(sprint_num, grades)
        changed = False
        for feature in feature_list.get("features", []):
            fid = feature.get("id")
            if fid not in target_ids:
                continue
            if recommendation in {"generate_next_sprint", "complete"}:
                next_status = "accepted"
            elif fid in failing:
                next_status = "repair_required"
            else:
                next_status = "implemented"
            if feature.get("status") != next_status:
                feature["status"] = next_status
                changed = True
        if changed:
            self.file_comm.write_feature_list(feature_list)

    # ---- 内部辅助 ----

    def _persist_accepted(self, payload: dict[str, Any] | None = None) -> None:
        if payload is None:
            payload = self.accepted_payload()
        self.file_comm.write_accepted_sprints(payload)

    def _update_feature_statuses(self, sprint_num: int, *, status: str) -> None:
        feature_list = self.file_comm.read_feature_list()
        if not feature_list:
            return
        target_ids = self.feature_ids_for_sprint(sprint_num)
        if not target_ids:
            return
        changed = False
        for feature in feature_list.get("features", []):
            if feature.get("id") in target_ids and feature.get("status") != status:
                feature["status"] = status
                changed = True
        if changed:
            self.file_comm.write_feature_list(feature_list)

    def _collect_failing_feature_ids(
        self, sprint_num: int, grades: dict[str, Any]
    ) -> set[str]:
        target_ids = self.feature_ids_for_sprint(sprint_num)
        if not target_ids:
            return set()

        failing: set[str] = set()
        for check in grades.get("ui_checks", []) or []:
            if not isinstance(check, dict):
                continue
            fid = str(check.get("feature_id", "")).strip()
            status = str(check.get("status", "")).strip().lower()
            if fid and fid in target_ids and status in {"fail", "partial"}:
                failing.add(fid)
        for result in grades.get("target_exit_criteria_results", []) or []:
            if not isinstance(result, dict):
                continue
            fid = str(result.get("feature_id", "")).strip()
            if fid and fid in target_ids and result.get("passed") is False:
                failing.add(fid)
        if failing:
            return failing
        if grades.get("overall_passed") is False:
            return set(target_ids)
        return set()
