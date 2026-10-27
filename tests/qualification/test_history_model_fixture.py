"""Test the controlled model script, not the native continuation acceptance."""

import json

import pytest

from tests._boot_model import HISTORY_FIRST, HISTORY_SECOND, _history_answer

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3.3")
NONCE = "proof-" + "a" * 32


def test_planner_output_is_only_untrusted_single_task_json():
    prompt = HISTORY_FIRST + " " + NONCE
    answer, call = _history_answer(
        {
            "messages": [
                {
                    "role": "system",
                    "content": "Return only a JSON object with tasks and output_task_id",
                },
                {"role": "user", "content": prompt},
            ]
        }
    )
    assert call is None
    assert json.loads(answer) == {
        "tasks": [{"task_id": "answer", "prompt": prompt, "depends_on": []}],
        "output_task_id": "answer",
    }


def test_first_turn_uses_real_act_and_does_not_fabricate_tool_result():
    request = {
        "messages": [{"role": "user", "content": HISTORY_FIRST + " " + NONCE}],
        "tools": [{"function": {"name": "act"}}],
    }
    answer, call = _history_answer(request)
    assert answer == "" and call["function"]["name"] == "act"
    assert json.loads(call["function"]["arguments"])["params"]["arguments"] == {
        "text": NONCE
    }
    request["messages"].append(
        {
            "role": "tool",
            "tool_call_id": "history-effect-call",
            "content": "echo:" + NONCE,
        }
    )
    answer, call = _history_answer(request)
    assert answer.startswith("history-first-reply") and call is None


def test_second_turn_does_not_repeat_prior_tool_call():
    answer, call = _history_answer(
        {
            "messages": [
                {"role": "user", "content": HISTORY_FIRST + " " + NONCE},
                {"role": "assistant", "content": "prior reply"},
                {"role": "user", "content": HISTORY_SECOND + " " + NONCE},
            ]
        }
    )
    assert answer.startswith("history-second-reply") and call is None


def test_missing_actual_tool_refuses_instead_of_claiming_effect():
    with pytest.raises(ValueError, match="real admitted act"):
        _history_answer(
            {"messages": [{"role": "user", "content": HISTORY_FIRST + " " + NONCE}]}
        )
