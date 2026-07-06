"""System-cognition trace labels must read as natural human text, never the
internal class name (e.g. 'router_memory_retrieval_decision_assistant hop=0')."""
from __future__ import annotations

from services.system_cognition.system_pipeline_runner import _assistant_title


class _A:
    def __init__(self, name, title=None):
        self.name = name
        if title is not None:
            self.title = title


def test_known_assistants_get_natural_titles():
    assert _assistant_title(_A("router_memory_retrieval_decision_assistant")) == "Checking memories"
    assert _assistant_title(_A("planner_memory_retrieval_decision_assistant")) == "Checking memories"
    assert _assistant_title(_A("belief_system_assistant")) == "Updating beliefs"


def test_explicit_title_wins():
    assert _assistant_title(_A("whatever", title="Custom label")) == "Custom label"


def test_unknown_name_is_humanized_not_raw():
    out = _assistant_title(_A("some_new_system_assistant"))
    assert "_" not in out and out[0].isupper()
    assert "assistant" not in out.lower()


def test_never_contains_hop_or_raw_identifier():
    for name in (
        "router_memory_retrieval_decision_assistant",
        "curiosity_system_assistant",
        "turn_interpretation_system_assistant",
    ):
        title = _assistant_title(_A(name))
        assert "hop" not in title.lower()
        assert name not in title
