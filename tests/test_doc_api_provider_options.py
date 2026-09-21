from inspiration_library.doc_api import chat_extra_body


def test_glm_coding_enables_required_reasoning():
    assert chat_extra_body("https://open.bigmodel.cn/api/coding/paas/v4") == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }
