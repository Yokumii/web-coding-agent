import pytest
from src.utils.llm_json import extract_json_object, LLMJSONError


def test_extracts_plain_json_object():
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extracts_from_fenced_block():
    text = "Here is the result:\n```json\n{\"x\": 2}\n```\nDone."
    assert extract_json_object(text) == {"x": 2}


def test_prefers_longer_balanced_block():
    text = '{"example": "short"} ... {"real": "answer", "extra": [1,2,3]}'
    assert extract_json_object(text) == {"real": "answer", "extra": [1, 2, 3]}


def test_relaxes_trailing_commas_and_comments():
    text = '''
    ```json
    {
      "a": 1, // a comment
      "b": [1, 2,],
    }
    ```
    '''
    assert extract_json_object(text) == {"a": 1, "b": [1, 2]}


def test_raises_when_no_json_present():
    with pytest.raises(LLMJSONError) as exc_info:
        extract_json_object("no braces here")
    assert "did not contain" in str(exc_info.value).lower()


def test_raises_when_malformed():
    with pytest.raises(LLMJSONError):
        extract_json_object('{"unclosed": ')


def test_strict_mode_rejects_relaxations():
    with pytest.raises(LLMJSONError):
        extract_json_object('{"a": 1,}', allow_relaxed=False)


def test_handles_braces_inside_string_values():
    text = '{"text": "this } is inside a string", "ok": true}'
    assert extract_json_object(text) == {"text": "this } is inside a string", "ok": True}


def test_error_snippet_is_truncated():
    long_garbage = "x" * 1000
    with pytest.raises(LLMJSONError) as exc_info:
        extract_json_object(long_garbage)
    message = str(exc_info.value)
    # Truncated to <= 400 chars worth of snippet, ending with the ellipsis marker.
    assert "..." in message
    # Sanity: the full 1000-char garbage MUST NOT appear verbatim.
    assert long_garbage not in message


def test_strict_mode_accepts_valid_json():
    assert extract_json_object('{"a": 1, "b": [2, 3]}', allow_relaxed=False) == {"a": 1, "b": [2, 3]}


def test_extracts_from_prose_without_fence():
    text = "Sure! Here is the JSON: {\"answer\": 42}. Hope this helps."
    assert extract_json_object(text) == {"answer": 42}
