from __future__ import annotations

from pathlib import Path
from typing import Any


PROFILE_CONTRACTS: dict[str, dict[str, Any]] = {
    "web": {
        "label": "Browser Web",
        "required_files": [],
        "required_globs": [],
        "guidance": "Build the requested browser application in frontend/.",
    },
    "wechat-miniapp": {
        "label": "WeChat Mini Program",
        "required_files": ["app.json"],
        "required_globs": ["**/*.wxml", "**/*.wxss", "**/*.js"],
        "guidance": "Provide genuine WeChat Mini Program source using WXML, WXSS, JavaScript, and app.json.",
    },
    "harmonyos": {
        "label": "HarmonyOS DevEco Entry",
        "required_files": ["module.json5"],
        "required_globs": ["**/*.ets"],
        "guidance": "Provide genuine HarmonyOS ArkTS/ETS Entry source and module.json5.",
    },
    "qml": {
        "label": "Qt QML",
        "required_files": ["main.qml"],
        "required_globs": ["**/*.qml"],
        "guidance": "Provide genuine Qt QML source with main.qml and reusable QML components where appropriate.",
    },
    "uniapp": {
        "label": "uni-app",
        "required_files": ["App.vue", "pages.json"],
        "required_globs": ["**/*.vue"],
        "guidance": "Provide genuine uni-app source including App.vue, pages.json, and page components.",
    },
}

DEMO_MARKERS = (
    "upload", "audio", "camera", "摄像", "hardware", "payment", "sensor",
    "microphone", "音频", "硬件", "支付",
)


def detect_target_profile(query: str) -> dict[str, Any]:
    normalized = query.lower()
    if "qml" in normalized:
        name = "qml"
    elif "uniapp" in normalized or "uni-app" in normalized or "hbuilder" in normalized:
        name = "uniapp"
    elif any(marker in normalized for marker in ("deveco", "harmonyos", "arkts", "entry framework")):
        name = "harmonyos"
    elif any(marker in normalized for marker in ("wechat mini program", "微信小程序", "mini program")):
        name = "wechat-miniapp"
    else:
        name = "web"
    contract = PROFILE_CONTRACTS[name]
    return {
        "profile": name,
        "label": contract["label"],
        "submission_dir": "frontend/submission",
        "required_files": list(contract["required_files"]),
        "required_globs": list(contract["required_globs"]),
        "guidance": contract["guidance"],
        "requires_browser_preview": True,
        "requires_preloaded_demo": any(marker in normalized for marker in DEMO_MARKERS),
    }


def target_profile_guidance(profile: dict[str, Any] | None) -> str:
    if not profile:
        return ""
    name = profile.get("profile", "web")
    lines = [
        "## Target delivery profile",
        f"- Requested target: {profile.get('label', name)} (`{name}`)",
    ]
    if name != "web":
        lines.extend([
            "- Keep `frontend/` as the runnable Vite browser preview used by the Harness.",
            "- Also place readable, genuine target-platform source under `frontend/submission/`.",
            f"- {profile.get('guidance', '')}",
            "- The browser preview and target source must represent the same product, states, and interactions.",
        ])
    if profile.get("requires_preloaded_demo"):
        lines.append(
            "- Provide a deterministic preloaded demo state visible without upload, device permission, "
            "hardware, payment, or network access. Preserve the real interaction entry point as well."
        )
    return "\n".join(lines) + "\n"


def validate_target_submission(frontend: Path, profile: dict[str, Any] | None) -> str | None:
    if not profile or profile.get("profile") == "web":
        return None
    submission = frontend / "submission"
    if not submission.is_dir():
        return "Missing frontend/submission directory containing requested target-platform source."
    missing_files = [
        name for name in profile.get("required_files", [])
        if not (submission / name).is_file()
    ]
    missing_globs = [
        pattern for pattern in profile.get("required_globs", [])
        if not any(path.is_file() for path in submission.glob(pattern))
    ]
    if missing_files or missing_globs:
        parts = []
        if missing_files:
            parts.append("missing files: " + ", ".join(missing_files))
        if missing_globs:
            parts.append("missing file patterns: " + ", ".join(missing_globs))
        return "Target-platform submission is incomplete (" + "; ".join(parts) + ")."
    return None
