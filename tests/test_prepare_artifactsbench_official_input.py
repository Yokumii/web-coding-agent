from pathlib import Path

from scripts.prepare_artifactsbench_official_input import official_answer


def test_official_answer_keeps_readable_source_before_renderable_preview(tmp_path: Path):
    frontend = tmp_path / "frontend"
    dist = frontend / "dist"
    submission = frontend / "submission"
    dist.mkdir(parents=True)
    submission.mkdir()
    (frontend / "src").mkdir()
    (submission / "main.qml").write_text("QML_READABLE_SOURCE")
    (frontend / "src" / "main.js").write_text("WEB_READABLE_SOURCE")
    (dist / "index.html").write_text(
        '<html><head><script type="module" src="/assets/app.js"></script></head><body>preview</body></html>'
    )
    (dist / "assets").mkdir()
    (dist / "assets" / "app.js").write_text("BUNDLED_PREVIEW")

    answer = official_answer(frontend)

    assert answer.index("QML_READABLE_SOURCE") < answer.index("===== RENDERABLE PREVIEW =====")
    assert "WEB_READABLE_SOURCE" not in answer
    assert answer.rstrip().endswith("</html>")
    assert "BUNDLED_PREVIEW" in answer
