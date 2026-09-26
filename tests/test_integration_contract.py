import json

from src.orchestration.integration_contract import build_integration_contract


def test_contract_binds_route_host_state_skill_and_accepted_prefix(tmp_path):
    harness = tmp_path / ".harness"
    harness.mkdir()
    (tmp_path / "frontend/src/pages").mkdir(parents=True)
    (tmp_path / "frontend/package.json").write_text(
        json.dumps({"dependencies": {"react": "latest"}}), encoding="utf-8"
    )
    (harness / "edit_task_contract.json").write_text(json.dumps({
        "schema_version": "edit-task-contract-v1",
        "requested_target_routes": ["/compare"],
        "chain_metadata": {
            "task_type": "Data Table",
            "instruction": "Add a data table to the Compare page using comparison items.",
            "accepted_state_summary": [{"edit_id": "q0", "task_type": "Shopping Cart"}],
        },
    }), encoding="utf-8")
    (harness / "edit_context_round_1.json").write_text(json.dumps({
        "source_windows": [{
            "path": "frontend/src/pages/ComparePage.tsx",
            "content": "const [comparisonItems, setComparisonItems] = useState(PRODUCTS);",
        }],
    }), encoding="utf-8")
    contract = build_integration_contract(
        workdir=tmp_path,
        harness_dir=harness,
        round_num=1,
        plan={"route_scope": {
            "target_routes": ["/compare"],
            "target_page_entries": ["frontend/src/pages/ComparePage.tsx"],
        }},
    )
    assert contract["target_route"] == ["/compare"]
    assert any("comparisonItems" in item for item in contract["host_data_state_sources"])
    assert any("mountDataTable" in item for item in contract["skill_entry_api"])
    assert contract["accepted_state_summary"][0]["edit_id"] == "q0"
    assert len(contract["required_wiring"]) == 4
