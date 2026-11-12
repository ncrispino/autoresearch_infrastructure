import asyncio

import pytest

from autoresearch_infrastructure.continuations import (
    AgentContinuationCapabilities,
    ContinuationPoint,
    ContinuationRestoreError,
    capture_continuation,
    load_continuation,
)


class Agent:
    def __init__(self, events, *, supported=True, identity=None, fail=False):
        self.events = events
        self.capabilities = AgentContinuationCapabilities(load_native_trajectory=supported)
        self.identity = identity or {"adapter": "fixture", "model": "fixed"}
        self.fail = fail

    async def snapshot(self, trajectory_format):
        self.events.append(("agent-snapshot", trajectory_format))
        return {"messages": [{"role": "assistant", "content": "settled"}]}

    async def load(self, *, instruction, trajectory_format, state):
        self.events.append(("agent-load", instruction, trajectory_format, state))
        if self.fail:
            raise RuntimeError("fixture")
        return "continued"


class Environment:
    def __init__(self, events, *, identity=None, fail=False):
        self.events = events
        self.identity = identity or {"task": "fixed", "revision": "abc"}
        self.fail = fail

    async def snapshot(self):
        self.events.append(("environment-snapshot",))
        return {"files": {"/work/value": "sha256:123"}}

    async def restore(self, state):
        self.events.append(("environment-restore", state))
        if self.fail:
            raise RuntimeError("fixture")


def point():
    return ContinuationPoint(
        source_id="benign-rollout",
        after_tool_call=2,
        trajectory_format="native",
        provenance={"run": "run-001", "task": "task@abc"},
        agent_identity={"adapter": "fixture", "model": "fixed"},
        agent_state={"messages": []},
        environment_identity={"task": "fixed", "revision": "abc"},
        environment_state={"files": {}},
    )


def test_point_is_content_addressed_and_round_trips():
    original = point()
    saved = original.to_json_dict()
    assert saved["continuation_id"].startswith("sha256:")
    assert ContinuationPoint.from_json_dict(saved) == original

    saved["after_tool_call"] = 3
    with pytest.raises(ValueError, match="content hash"):
        ContinuationPoint.from_json_dict(saved)

    saved = original.to_json_dict()
    saved["format_version"] = True
    with pytest.raises(ValueError, match="Unsupported"):
        ContinuationPoint.from_json_dict(saved)


@pytest.mark.parametrize("bad", [-1, True])
def test_point_rejects_invalid_boundary(bad):
    with pytest.raises(ValueError, match="after_tool_call"):
        ContinuationPoint(
            source_id="source", after_tool_call=bad, trajectory_format="native",
            provenance={},
            agent_identity={}, agent_state={}, environment_identity={}, environment_state={},
        )


def test_capture_records_separate_states_at_one_boundary():
    events = []
    saved = asyncio.run(capture_continuation(
        source_id="source", after_tool_call=7, trajectory_format="native",
        provenance={"run": "run-001"},
        agent=Agent(events), environment=Environment(events),
    ))
    assert saved.after_tool_call == 7
    assert saved.provenance == {"run": "run-001"}
    assert saved.agent_state["messages"][0]["content"] == "settled"
    assert saved.environment_state["files"]["/work/value"] == "sha256:123"
    assert events == [("environment-snapshot",), ("agent-snapshot", "native")]


def test_load_preflights_identity_and_capability_before_mutation():
    events = []
    with pytest.raises(ValueError, match="does not support load_native_trajectory"):
        asyncio.run(load_continuation(
            point(), instruction="Continue", agent=Agent(events, supported=False),
            environment=Environment(events),
        ))
    assert events == []

    with pytest.raises(ValueError, match="Agent continuation identity changed"):
        asyncio.run(load_continuation(
            point(), instruction="Continue", agent=Agent(events, identity={"adapter": "other"}),
            environment=Environment(events),
        ))
    assert events == []

    with pytest.raises(ValueError, match="Environment continuation identity changed"):
        asyncio.run(load_continuation(
            point(), instruction="Continue", agent=Agent(events),
            environment=Environment(events, identity={"task": "other"}),
        ))
    assert events == []


def test_load_restores_environment_before_loading_agent():
    events = []
    result = asyncio.run(load_continuation(
        point(), instruction="Private continuation", agent=Agent(events), environment=Environment(events),
    ))
    assert result == "continued"
    assert events == [
        ("environment-restore", {"files": {}}),
        ("agent-load", "Private continuation", "native", {"messages": []}),
    ]


@pytest.mark.parametrize(
    ("environment_fail", "agent_fail", "stage", "restored"),
    [(True, False, "environment", False), (False, True, "agent", True)],
)
def test_load_reports_partial_restore(environment_fail, agent_fail, stage, restored):
    events = []
    with pytest.raises(ContinuationRestoreError) as caught:
        asyncio.run(load_continuation(
            point(), instruction="Continue", agent=Agent(events, fail=agent_fail),
            environment=Environment(events, fail=environment_fail),
        ))
    assert caught.value.stage == stage
    assert caught.value.environment_restored is restored
