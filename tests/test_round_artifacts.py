from pathlib import Path

from src.orchestration.file_comm import FileComm
from src.orchestration.round_artifacts import RoundArtifacts


def test_round_artifacts_names_paths_and_refs(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    artifacts = RoundArtifacts(file_comm, 3)

    assert artifacts.feedback_name == "feedback_round_3.md"
    assert artifacts.grade_name == "grade_round_3.json"
    assert artifacts.visual_manifest_name == "visual_manifest_round_3.json"
    assert artifacts.feedback_ref == ".harness/feedback_round_3.md"
    assert artifacts.grade_ref == ".harness/grade_round_3.json"
    assert artifacts.visual_manifest_ref == ".harness/visual_manifest_round_3.json"
    assert artifacts.feedback_path == file_comm.dir / "feedback_round_3.md"
    assert artifacts.grade_path == file_comm.dir / "grade_round_3.json"
    assert artifacts.visual_manifest_path == file_comm.dir / "visual_manifest_round_3.json"
    assert artifacts.trace_path("generator") == file_comm.dir / "traces" / "generator_round_3.jsonl"


def test_previous_existing_refs_only_returns_present_artifacts(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_feedback(1, "previous feedback")

    artifacts = RoundArtifacts(file_comm, 2)

    assert artifacts.previous_existing_refs() == [".harness/feedback_round_1.md"]


def test_previous_existing_refs_is_empty_for_first_round(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    file_comm.write_feedback(1, "current feedback")

    artifacts = RoundArtifacts(file_comm, 1)

    assert artifacts.previous_existing_refs() == []


def test_visual_screenshot_refs_prefers_manifest_then_grades_then_files(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")
    artifacts = RoundArtifacts(file_comm, 4)

    assert artifacts.visual_screenshot_refs(
        manifest={"screenshots": [" .harness/from_manifest.png ", ""]},
        grades={"appearance_review": {"screenshots": [".harness/from_grade.png"]}},
    ) == [".harness/from_manifest.png"]

    assert artifacts.visual_screenshot_refs(
        manifest={"screenshots": []},
        grades={"appearance_review": {"screenshots": [" .harness/from_grade.png "]}},
    ) == [".harness/from_grade.png"]

    (file_comm.dir / "visual_round_4_home.png").write_bytes(b"PNGFAKE")
    (file_comm.dir / "visual_round_4_bottom.png").write_bytes(b"PNGFAKE")

    assert artifacts.visual_screenshot_refs(manifest=None, grades=None) == [
        ".harness/visual_round_4_bottom.png",
        ".harness/visual_round_4_home.png",
    ]


def test_visual_capture_refs_are_standardized(tmp_path: Path):
    file_comm = FileComm(tmp_path / ".harness")

    artifacts = RoundArtifacts(file_comm, 2)

    assert artifacts.visual_capture_refs == [
        ".harness/visual_round_2_home.png",
        ".harness/visual_round_2_mid.png",
        ".harness/visual_round_2_bottom.png",
    ]
