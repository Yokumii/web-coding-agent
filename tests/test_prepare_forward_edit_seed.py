from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.prepare_forward_edit_seed import compact_source_ui_contract, prepare_seed


def test_prepare_seed_copies_verified_frontend_with_single_baseline(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text("<main>accepted</main>")
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text(json.dumps({"functional_passed": True}))
    target = tmp_path / "edit_case"

    baseline = prepare_seed(source, target, evaluation)

    assert (target / "frontend" / "index.html").read_text() == "<main>accepted</main>"
    assert json.loads((target / "seed_manifest.json").read_text())["baseline_commit"] == baseline
    subjects = subprocess.run(
        ["git", "log", "--format=%s"], cwd=target / "frontend", text=True,
        check=True, capture_output=True,
    ).stdout.splitlines()
    assert subjects == ["chore: accepted forward-edit baseline"]
    assert subprocess.run(
        ["git", "config", "--get", "user.name"], cwd=target / "frontend",
        text=True, check=True, capture_output=True,
    ).stdout.strip() == "WebCoding Harness"


def test_prepare_seed_records_external_assets_without_rejecting_reverse_style_source(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text('<script src="https://cdn.example/app.js"></script>')
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}")

    prepare_seed(source, tmp_path / "target", evaluation)

    manifest = json.loads((tmp_path / "target" / "seed_manifest.json").read_text())
    assert manifest["asset_policy"] == "match_reverse_source"
    assert manifest["external_asset_urls"] == ["https://cdn.example/app.js"]


def test_compact_source_ui_contract_keeps_navigation_without_source_code(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text(
        '<nav><a href="#gallery">Exhibit <strong>Gallery</strong></a></nav>'
        '<section id="view-gallery" class="view-section"><script>secret()</script></section>'
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text(json.dumps({
        "baseline": {
            "interactive": [
                {"selector": "a:nth-of-type(1)", "tag": "a", "text": "Exhibit Gallery"}
            ]
        }
    }))

    contract = compact_source_ui_contract(source, evaluation)

    assert contract["pages"][0]["navigation"] == [
        {"href": "#gallery", "text": "Exhibit Gallery"}
    ]
    assert contract["pages"][0]["surfaces"] == [
        {"tag": "section", "id": "view-gallery", "class": "view-section"},
    ]
    assert "secret" not in json.dumps(contract)


def test_compact_source_ui_contract_keeps_repeated_runtime_collection_selector(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text(
        '<main><div id="gallery-grid"></div></main>', encoding="utf-8"
    )
    evaluation = tmp_path / "observation.json"
    evaluation.write_text(json.dumps({
        "baseline": {
            "html": (
                '<div id="gallery-grid">'
                '<article class="artifact-card">A</article>'
                '<article class="artifact-card">B</article>'
                '</div>'
            ),
            "interactive": [],
        }
    }), encoding="utf-8")

    contract = compact_source_ui_contract(source, evaluation)

    assert {"selector": ".artifact-card", "tag": "article", "count": 2} in (
        contract["observed_collections"]
    )
    assert any(
        surface.get("id") == "gallery-grid"
        for surface in contract["pages"][0]["surfaces"]
    )


def test_compact_source_ui_contract_keeps_dynamic_source_collection_selector(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text('<div id="gallery-grid"></div>')
    (source / "app.js").write_text(
        "const card = document.createElement('div');\n"
        "card.className = 'artifact-card';\n"
        "document.querySelectorAll('.artifact-card');\n"
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}")

    contract = compact_source_ui_contract(source, evaluation)

    assert {"selector": ".artifact-card", "source": "source-selector"} in (
        contract["observed_collections"]
    )


def test_compact_source_ui_contract_keeps_singular_source_control_selectors(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text('<button class="nav-toggle"></button>')
    (source / "app.js").write_text(
        "const toggle = document.querySelector('.nav-toggle');\n"
        "const download = document.getElementById('downloadBtn');\n"
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}")

    contract = compact_source_ui_contract(source, evaluation)

    assert {"selector": ".nav-toggle", "source": "source-selector"} in (
        contract["observed_controls"]
    )
    assert {"selector": "#downloadBtn", "source": "source-selector"} in (
        contract["observed_controls"]
    )


def test_compact_source_ui_contract_keeps_select_values_without_source_code(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text(
        '<select id="filter" data-testid="type-filter">'
        '<option value="All">All items</option>'
        '<option value="Timepiece">Timepieces</option>'
        '</select>',
        encoding="utf-8",
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}")

    contract = compact_source_ui_contract(source, evaluation)

    assert contract["pages"][0]["controls"] == [{
        "tag": "select",
        "selector": "[data-testid='type-filter']",
        "selector_aliases": ["#filter"],
        "options": [
            {"value": "All", "text": "All items"},
            {"value": "Timepiece", "text": "Timepieces"},
        ],
    }]


def test_compact_source_ui_contract_exposes_stable_public_outputs(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text(
        '<span data-testid="total-downloads">0</span>'
        '<span data-testid="total-volume">0 MB</span>'
        '<div data-testid="private-container"></div>',
        encoding="utf-8",
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}", encoding="utf-8")

    contract = compact_source_ui_contract(source, evaluation)

    assert contract["pages"][0]["outputs"] == [
        {"tag": "span", "selector": "[data-testid='total-downloads']"},
        {"tag": "span", "selector": "[data-testid='total-volume']"},
    ]


def test_compact_source_ui_contract_keeps_stable_named_item_addresses(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "index.html").write_text(
        '<ul><li><a class="sidebar-item card" data-name="Audio Studio Driver">'
        'Audio</a></li></ul>',
        encoding="utf-8",
    )
    evaluation = tmp_path / "evaluation.json"
    evaluation.write_text("{}", encoding="utf-8")

    contract = compact_source_ui_contract(source, evaluation)

    assert contract["pages"][0]["addressable_items"] == [{
        "tag": "a",
        "data_name": "Audio Studio Driver",
        "selector": 'a.sidebar-item[data-name="Audio Studio Driver"]',
    }]
