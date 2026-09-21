import pytest

from inspiration_library.dynamic_capability_retrieval import validate_capability_extraction
from inspiration_library.product_sessions import extract_product_pattern


@pytest.mark.parametrize("source", [{"source_url": "https://example.org/demo"},
                                    {"source_project": "/tmp/demo"}])
def test_fragment_only_source_can_return_no_features(source):
    result = validate_capability_extraction(
        {"seed_summary": "Only a documentation shell and initial static component label were observed.",
         "business_objects": [], "capabilities": [],
         "abstention_reason": "No complete user action and resulting feature state were observed."},
        seed_id="fragment_demo", **source,
    )
    assert result["capabilities"] == []
    assert result["abstention_reason"]


def test_empty_feature_result_skips_product_pattern_without_model_call():
    pattern, labels = extract_product_pattern(
        {"seed_id": "fragment_demo"}, {"capabilities": []}, None, "unused"
    )
    assert pattern is None
    assert labels == {}
