from scripts.recover_accepted_tapes import replay_evidence_is_complete


def test_replay_evidence_requires_exact_order_and_all_checks_passing():
    checks = [{"id": "UI-001"}, {"id": "UI-002"}]
    passing = {
        "checks": [
            {"check_id": "UI-001", "status": "ok"},
            {"check_id": "UI-002", "status": "ok"},
        ]
    }

    assert replay_evidence_is_complete(passing, checks) is True
    passing["checks"].reverse()
    assert replay_evidence_is_complete(passing, checks) is False
    passing["checks"][0]["status"] = "action_failed"
    assert replay_evidence_is_complete(passing, checks) is False
