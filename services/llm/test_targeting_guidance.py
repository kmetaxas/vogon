"""Keep the default troubleshooting instructions aligned with targeting tools."""

from services.temporal_workers.activities import SYSTEM_PROMPT


def test_system_prompt_explains_safe_marvin_targeting():
    assert '"hostname": ["kill9eu"]' in SYSTEM_PROMPT
    assert '"labels": {"team": "platform"}' in SYSTEM_PROMPT
    assert "find_targets" in SYSTEM_PROMPT
    assert "marvin_ids" in SYSTEM_PROMPT
    assert "expected_target_count=1" in SYSTEM_PROMPT
    assert "Do not broaden" in SYSTEM_PROMPT
