import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from pathlib import Path

import pytest
import httpx
from cryptography.fernet import Fernet

from component.config import settings

settings.MAIL_SECRET_KEY = Fernet.generate_key().decode()
from services.assistants.orchestration.guarded_tools import (
    build_tool_confirmation_pending_action,
    tool_call_hash,
    verify_pending_tool_confirmation,
)
from services.assistants.orchestration.pending import PendingStore
from services.assistants.orchestration.pipeline_runner import AssistantPipelineRunner
from services.assistants.orchestration.tool_execution import ToolExecutionRunner
from services.assistants.runtime_config import AssistantConfig, SkillConfig, ToolConfig
from services.assistants.tool_guard import AssistantToolGuard
from services.workflows.workflow_executor import WorkflowExecutor
from services.workflows.workflow_run_service import WorkflowRunService


class FakeToolService:
    def __init__(self):
        self.calls = []

    async def execute_tool(self, *, tool_id, args):
        self.calls.append({"tool_id": tool_id, "args": args})
        return {"status": "success", "echo": args, "tool_id": tool_id}


class FakeOpenAIResponse:
    def __init__(self, text):
        self.text = text


class FakeOpenAI:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    async def ask_orchestration_async(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if isinstance(self.plan, list):
            index = min(len(self.calls) - 1, len(self.plan) - 1)
            return FakeOpenAIResponse(json.dumps(self.plan[index]))
        return FakeOpenAIResponse(json.dumps(self.plan))


class FakeAssistantRuntime:
    def __init__(self, config):
        self.config = config
        self.name = config.name
        self.instructions = config.instruction

    def prompt(self, **kwargs):
        return "prompt"

    def extract_first_json_object(self, text):
        return json.loads(text)


def trace_fn(trace, **kwargs):
    trace.append(kwargs)


def make_runner(fake_service=None):
    return ToolExecutionRunner(
        tool_execution_service=fake_service or FakeToolService(),
        ingest_wait_timeout_s=0.01,
        ingest_poll_interval_s=0.01,
        max_tool_calls_per_turn=5,
    )


def shell_call(command="printf hello", timeout=60):
    return {
        "tool_id": 293,
        "tool": "system__shell_exec",
        "kind": "code",
        "args": {"command": command, "timeout": timeout},
        "reason": "test",
    }


def test_pending_action_contains_display_and_stable_hash():
    pending = build_tool_confirmation_pending_action(shell_call("ls -la", 60))

    assert pending["type"] == "tool_confirmation"
    assert pending["guard_type"] == "confirmation_required"
    assert pending["risk_level"] == "high"
    assert pending["display"] == {"command": "ls -la", "timeout": 60.0}
    assert pending["tool_call_hash"] == tool_call_hash(pending["tool_call"])
    assert pending["tool_call_hash"] == tool_call_hash(shell_call("ls -la", 60.0))


def test_hash_changes_when_command_changes_and_mismatch_is_rejected():
    pending = build_tool_confirmation_pending_action(shell_call("echo safe"))
    pending["tool_call"]["args"]["command"] = "echo changed"

    with pytest.raises(ValueError, match="hash mismatch"):
        verify_pending_tool_confirmation(pending)


def test_invalid_empty_command_is_rejected():
    with pytest.raises(ValueError, match="non-empty string"):
        build_tool_confirmation_pending_action(shell_call("   "))


def test_timeout_above_cap_is_rejected():
    with pytest.raises(ValueError, match="<= 120 seconds"):
        build_tool_confirmation_pending_action(shell_call("echo hi", 121))


def test_guarded_shell_exec_without_confirmation_does_not_execute():
    async def run_case():
        fake_service = FakeToolService()
        runner = make_runner(fake_service)
        trace = []

        with pytest.raises(PermissionError):
            await runner.execute_tool_calls(
                tool_calls=[shell_call("echo blocked")],
                session_id="thread-1",
                turn_id=1,
                trace=trace,
                assistant_name="test",
                trace_fn=trace_fn,
                preview_fn=lambda x: x,
            )

        assert fake_service.calls == []
        assert any(t.get("type") == "guarded_tool_confirmation_required" for t in trace)

    asyncio.run(run_case())


def test_confirmed_exact_tool_call_executes():
    async def run_case():
        fake_service = FakeToolService()
        runner = make_runner(fake_service)
        tc = shell_call("echo confirmed", 10)
        confirmed_hash = tool_call_hash(tc)

        results = await runner.execute_tool_calls(
            tool_calls=[tc],
            session_id="thread-1",
            turn_id=1,
            trace=[],
            assistant_name="test",
            trace_fn=trace_fn,
            preview_fn=lambda x: x,
            confirmed_tool_call_hashes={confirmed_hash},
        )

        assert len(fake_service.calls) == 1
        assert fake_service.calls[0]["args"]["command"] == "echo confirmed"
        assert results[0]["status"] == "success"

    asyncio.run(run_case())


def test_modified_command_after_confirmation_is_blocked():
    async def run_case():
        fake_service = FakeToolService()
        runner = make_runner(fake_service)
        original = shell_call("echo original", 10)
        confirmed_hash = tool_call_hash(original)
        modified = shell_call("echo modified", 10)

        with pytest.raises(PermissionError):
            await runner.execute_tool_calls(
                tool_calls=[modified],
                session_id="thread-1",
                turn_id=1,
                trace=[],
                assistant_name="test",
                trace_fn=trace_fn,
                preview_fn=lambda x: x,
                confirmed_tool_call_hashes={confirmed_hash},
            )

        assert fake_service.calls == []

    asyncio.run(run_case())


def test_non_guarded_tools_still_execute_normally():
    async def run_case():
        fake_service = FakeToolService()
        runner = make_runner(fake_service)
        tc = {"tool_id": 11, "tool": "normal_tool", "args": {"value": 1}}

        results = await runner.execute_tool_calls(
            tool_calls=[tc],
            session_id="thread-1",
            turn_id=1,
            trace=[],
            assistant_name="test",
            trace_fn=trace_fn,
            preview_fn=lambda x: x,
        )

        assert fake_service.calls == [{"tool_id": 11, "args": {"value": 1}}]
        assert results[0]["status"] == "success"

    asyncio.run(run_case())


def test_pipeline_returns_pending_action_for_guarded_shell_without_execution():
    async def run_case():
        fake_service = FakeToolService()
        pending = PendingStore()
        plan = {"action": "tool_calls", "tool_calls": [shell_call("pwd", 30)]}
        config = AssistantConfig(
            id=1,
            name="assistant",
            instruction="x",
            skills=[SkillConfig(id=1, name="domain", tools=[ToolConfig(id=293, name="system__shell_exec")])],
        )
        pipeline = AssistantPipelineRunner(
            openai_service=FakeOpenAI(plan),
            runtime_resolver=None,
            tool_runner=make_runner(fake_service),
            tool_guard=AssistantToolGuard(),
            assistant_output_store_service=None,
            trace_fn=trace_fn,
            pending_store=pending,
        )

        result = await pipeline.run(
            assistant=FakeAssistantRuntime(config),
            question="run pwd",
            payload={"_selected_skill_names": ["domain"]},
            session_id="thread-guard",
            turn_id=1,
            trace=[],
        )

        assert result["mode"] == "confirm_action"
        assert result["pending_action"]["type"] == "tool_confirmation"
        assert result["pending_action"]["display"] == {"command": "pwd", "timeout": 30.0}
        assert fake_service.calls == []
        assert pending.get("thread-guard")["tool_call_hash"] == result["pending_action"]["tool_call_hash"]
        assert any(t.get("type") == "guarded_tool_confirmation_required" for t in result["trace"])

    asyncio.run(run_case())


def workflow_policy(*, allow=None, deny=None, on_denied="fail", allowed_working_dirs=None, max_timeout_seconds=120):
    return {
        "guarded_tools": {
            "system__shell_exec": {
                "auto_confirm": True,
                "on_denied": on_denied,
                "allowed_working_dirs": allowed_working_dirs or [],
                "max_timeout_seconds": max_timeout_seconds,
                "allow": allow or [],
                "deny": deny or [],
            }
        }
    }


def make_workflow_pipeline(plan, fake_service=None, *, skill_files_root="/tmp/skill-files"):
    fake_service = fake_service or FakeToolService()
    config = AssistantConfig(
        id=1,
        name="assistant",
        instruction="x",
        skills=[
            SkillConfig(
                id=1,
                name="domain",
                tools=[ToolConfig(id=293, name="system__shell_exec")],
                skill_files_root=skill_files_root,
            )
        ],
    )
    pipeline = AssistantPipelineRunner(
        openai_service=FakeOpenAI(plan),
        runtime_resolver=None,
        tool_runner=make_runner(fake_service),
        tool_guard=AssistantToolGuard(),
        assistant_output_store_service=None,
        trace_fn=trace_fn,
        pending_store=None,
    )
    return pipeline, FakeAssistantRuntime(config), fake_service


async def run_workflow_pipeline(plan, policy, *, skill_files_root="/tmp/skill-files"):
    plan = dict(plan)
    plan.setdefault("response_mode", "emit_handoff")
    final_plan = {
        "action": "final",
        "final_answer": "done",
        "response_mode": "emit_handoff",
        "downstream_handoff": {"summary": "done", "status": "success"},
    }
    plan_input = [plan, final_plan] if (plan.get("action") == "tool_calls" and "other.py" not in str(plan)) else plan
    pipeline, assistant, fake_service = make_workflow_pipeline(plan_input, skill_files_root=skill_files_root)
    result = await pipeline.run(
        assistant=assistant,
        question="run workflow tool",
        payload={
            "_selected_skill_names": ["domain"],
            "_workflow_background": True,
            "_workflow_execution_policy": policy,
        },
        session_id="workflow:1:operation:1",
        turn_id=1,
        trace=[],
    )
    return result, fake_service


def test_workflow_allowed_equals_command_auto_executes():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("python fabric_collect.py", 30)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(allow=[{"operator": "equals", "value": "python fabric_collect.py"}]),
        )

        assert result["mode"] == "final"
        assert fake_service.calls[0]["args"]["command"] == "python fabric_collect.py"
        trace_event = next(t for t in result["trace"] if t.get("type") == "guarded_tool_policy_evaluated")
        assert trace_event["data"]["decision"] == "allowed"
        assert trace_event["data"]["auto_confirmed"] is True

    asyncio.run(run_case())


def test_workflow_allowed_starts_with_command_auto_executes():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("bash collect.sh", 30)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(allow=[{"operator": "starts_with", "value": "bash "}]),
        )

        assert result["mode"] == "final"
        assert fake_service.calls[0]["args"]["command"] == "bash collect.sh"

    asyncio.run(run_case())


def test_workflow_contains_rule_works():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("python fabric_collect.py --check", 30)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(allow=[{"operator": "contains", "value": "--check"}]),
        )

        assert result["mode"] == "final"
        assert len(fake_service.calls) == 1

    asyncio.run(run_case())


def test_workflow_does_not_contain_rule_works():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("python fabric_collect.py", 30)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(allow=[{"operator": "does_not_contain", "value": "--danger"}]),
        )

        assert result["mode"] == "final"
        assert len(fake_service.calls) == 1

    asyncio.run(run_case())


def test_workflow_deny_rule_wins_over_allow():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("bash sudo_collect.sh", 30)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(
                allow=[{"operator": "starts_with", "value": "bash "}],
                deny=[{"operator": "contains", "value": "sudo"}],
            ),
        )

        assert result["mode"] == "error"
        assert result["answer"] == "policy_denied: deny rule matched"
        assert fake_service.calls == []
        trace_event = next(t for t in result["trace"] if t.get("type") == "guarded_tool_policy_evaluated")
        assert trace_event["data"]["matched_deny_rule"] == {"operator": "contains", "value": "sudo"}

    asyncio.run(run_case())


def test_workflow_no_allow_match_denies():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("python other.py", 30)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(allow=[{"operator": "equals", "value": "python fabric_collect.py"}]),
        )

        assert result["mode"] == "error"
        assert result["answer"] == "policy_denied: no allow rule matched"
        assert fake_service.calls == []

    asyncio.run(run_case())


def test_workflow_working_dir_mismatch_denies():
    async def run_case():
        tc = shell_call("python fabric_collect.py", 30)
        tc["args"]["working_dir"] = "/tmp/not-skill-files"
        plan = {"action": "tool_calls", "tool_calls": [tc]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(
                allow=[{"operator": "equals", "value": "python fabric_collect.py"}],
                allowed_working_dirs=["${skill_files_root}"],
            ),
        )

        assert result["mode"] == "error"
        assert result["answer"] == "policy_denied: working_dir is not allowed by workflow policy"
        assert fake_service.calls == []

    asyncio.run(run_case())


def test_workflow_timeout_above_max_denies():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("python fabric_collect.py", 90)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(
                allow=[{"operator": "equals", "value": "python fabric_collect.py"}],
                max_timeout_seconds=30,
            ),
        )

        assert result["mode"] == "error"
        assert result["answer"] == "policy_denied: timeout exceeds workflow policy maximum"
        assert fake_service.calls == []

    asyncio.run(run_case())


def test_workflow_unresolved_skill_files_root_denies():
    async def run_case():
        tc = shell_call("python fabric_collect.py", 30)
        tc["args"]["working_dir"] = "/tmp/skill-files"
        plan = {"action": "tool_calls", "tool_calls": [tc]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(
                allow=[{"operator": "equals", "value": "python fabric_collect.py"}],
                allowed_working_dirs=["${skill_files_root}"],
            ),
            skill_files_root=None,
        )

        assert result["mode"] == "error"
        assert result["answer"] == "policy_denied: working_dir is not allowed by workflow policy"
        assert fake_service.calls == []

    asyncio.run(run_case())


def test_workflow_auto_approved_call_still_uses_confirmed_tool_call_hash():
    async def run_case():
        class HashAssertingRunner(ToolExecutionRunner):
            async def execute_tool_calls(self, *args, **kwargs):
                tool_calls = kwargs["tool_calls"]
                confirmed = kwargs.get("confirmed_tool_call_hashes") or set()
                assert tool_call_hash(tool_calls[0]) in confirmed
                return await super().execute_tool_calls(*args, **kwargs)

        fake_service = FakeToolService()
        plan = [
            {"action": "tool_calls", "response_mode": "emit_handoff", "tool_calls": [shell_call("python fabric_collect.py", 30)]},
            {"action": "final", "final_answer": "done", "response_mode": "emit_handoff", "downstream_handoff": {"summary": "done", "status": "success"}},
        ]
        config = AssistantConfig(
            id=1,
            name="assistant",
            instruction="x",
            skills=[SkillConfig(id=1, name="domain", tools=[ToolConfig(id=293, name="system__shell_exec")])],
        )
        pipeline = AssistantPipelineRunner(
            openai_service=FakeOpenAI(plan),
            runtime_resolver=None,
            tool_runner=HashAssertingRunner(
                tool_execution_service=fake_service,
                ingest_wait_timeout_s=0.01,
                ingest_poll_interval_s=0.01,
                max_tool_calls_per_turn=5,
            ),
            tool_guard=AssistantToolGuard(),
            assistant_output_store_service=None,
            trace_fn=trace_fn,
            pending_store=None,
        )

        result = await pipeline.run(
            assistant=FakeAssistantRuntime(config),
            question="run workflow tool",
            payload={
                "_selected_skill_names": ["domain"],
                "_workflow_background": True,
                "_workflow_execution_policy": workflow_policy(
                    allow=[{"operator": "equals", "value": "python fabric_collect.py"}]
                ),
            },
            session_id="workflow:1:operation:1",
            turn_id=1,
            trace=[],
        )

        assert result["mode"] == "final"
        assert len(fake_service.calls) == 1

    asyncio.run(run_case())


def test_workflow_denied_pause_policy_waits_for_approval():
    async def run_case():
        plan = {"action": "tool_calls", "tool_calls": [shell_call("python other.py", 30)]}
        result, fake_service = await run_workflow_pipeline(
            plan,
            workflow_policy(
                allow=[{"operator": "equals", "value": "python fabric_collect.py"}],
                on_denied="pause",
            ),
        )

        assert result["mode"] == "workflow_waiting"
        assert result["terminal_state"] == "waiting_for_confirmation"
        assert result["pending_action"]["type"] == "workflow_tool_approval"
        assert result["pending_action"]["display"]["command"] == "python other.py"
        assert result["pending_action"]["policy_decision"]["denial_reason"] == "no allow rule matched"
        assert fake_service.calls == []

    asyncio.run(run_case())


class SequencedToolService:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def execute_tool(self, *, tool_id, args):
        self.calls.append({"tool_id": tool_id, "args": args})
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]


def make_chat_pipeline(plan, fake_service=None, *, max_tool_calls_per_turn=5):
    fake_service = fake_service or FakeToolService()
    config = AssistantConfig(
        id=2,
        name="chat_assistant",
        instruction="x",
        skills=[SkillConfig(id=2, name="domain", tools=[ToolConfig(id=11, name="normal_tool")])],
    )
    openai = FakeOpenAI(plan)
    pipeline = AssistantPipelineRunner(
        openai_service=openai,
        runtime_resolver=None,
        tool_runner=make_runner(fake_service),
        tool_guard=AssistantToolGuard(),
        assistant_output_store_service=None,
        trace_fn=trace_fn,
        pending_store=PendingStore(),
        max_tool_calls_per_turn=max_tool_calls_per_turn,
    )
    return pipeline, FakeAssistantRuntime(config), fake_service, openai


def normal_call(value=1):
    return {"tool_id": 11, "tool": "normal_tool", "args": {"value": value}, "reason": "test"}


def test_chat_recoverable_tool_error_is_fed_back_to_same_assistant():
    async def run_case():
        plan = [
            {"action": "tool_calls", "tool_calls": [normal_call(1)]},
            {"action": "final", "final_answer": "Recovered after observing the tool error."},
        ]
        service = SequencedToolService([
            {"exit_code": 1, "stdout": "", "stderr": "missing file", "command": "fake"},
        ])
        pipeline, assistant, fake_service, openai = make_chat_pipeline(plan, service)

        result = await pipeline.run(
            assistant=assistant,
            question="recover",
            payload={"_selected_skill_names": ["domain"]},
            session_id="chat-recover",
            turn_id=1,
            trace=[],
        )

        assert result["mode"] == "final"
        assert result["terminal_state"] == "completed"
        assert result["answer"] == "Recovered after observing the tool error."
        assert len(fake_service.calls) == 1
        assert len(openai.calls) == 2
        assert any(t.get("type") == "tool_execution_failed_recoverable" for t in result["trace"])
        assert any(t.get("type") == "agent_loop_continue_after_tool_result" for t in result["trace"])

    asyncio.run(run_case())


def test_chat_assistant_can_recover_after_failed_tool_call_and_complete():
    async def run_case():
        plan = [
            {"action": "tool_calls", "tool_calls": [normal_call(1)]},
            {"action": "tool_calls", "tool_calls": [normal_call(2)]},
            {"action": "final", "final_answer": "Second attempt worked."},
        ]
        service = SequencedToolService([
            {"exit_code": 1, "stdout": "", "stderr": "command not found", "command": "bad"},
            {"status": "success", "stdout": "ok", "command": "good"},
        ])
        pipeline, assistant, fake_service, _ = make_chat_pipeline(plan, service)

        result = await pipeline.run(
            assistant=assistant,
            question="recover with second command",
            payload={"_selected_skill_names": ["domain"]},
            session_id="chat-recover-second",
            turn_id=1,
            trace=[],
        )

        assert result["mode"] == "final"
        assert result["answer"] == "Second attempt worked."
        assert len(fake_service.calls) == 2

    asyncio.run(run_case())


def test_chat_budget_exceeded_stops_loop_with_terminal_state():
    async def run_case():
        old_value = settings.CHAT_AGENT_MAX_ITERATIONS_PER_STEP
        settings.CHAT_AGENT_MAX_ITERATIONS_PER_STEP = 2
        try:
            plan = {"action": "tool_calls", "tool_calls": [normal_call(1)]}
            pipeline, assistant, fake_service, _ = make_chat_pipeline(plan)
            result = await pipeline.run(
                assistant=assistant,
                question="loop forever",
                payload={"_selected_skill_names": ["domain"]},
                session_id="chat-budget",
                turn_id=1,
                trace=[],
            )
        finally:
            settings.CHAT_AGENT_MAX_ITERATIONS_PER_STEP = old_value

        assert result["mode"] == "error"
        assert result["terminal_state"] == "budget_exceeded"
        assert result["budget_reason"] == "max_iterations"
        assert any(t.get("type") == "agent_loop_budget_exceeded" for t in result["trace"])
        assert len(fake_service.calls) == 2

    asyncio.run(run_case())


def test_same_repeated_error_stops_after_repeat_limit():
    async def run_case():
        old_value = settings.CHAT_AGENT_MAX_SAME_ERROR_REPEATS
        settings.CHAT_AGENT_MAX_SAME_ERROR_REPEATS = 1
        try:
            plan = {"action": "tool_calls", "tool_calls": [normal_call(1)]}
            service = SequencedToolService([
                {"exit_code": 1, "stdout": "", "stderr": "same error", "command": "bad"},
            ])
            pipeline, assistant, fake_service, _ = make_chat_pipeline(plan, service)
            result = await pipeline.run(
                assistant=assistant,
                question="same error",
                payload={"_selected_skill_names": ["domain"]},
                session_id="chat-same-error",
                turn_id=1,
                trace=[],
            )
        finally:
            settings.CHAT_AGENT_MAX_SAME_ERROR_REPEATS = old_value

        assert result["mode"] == "error"
        assert result["terminal_state"] == "budget_exceeded"
        assert result["budget_reason"] == "same_error_repeated"
        assert any(t.get("type") == "agent_loop_same_error_repeated" for t in result["trace"])
        assert len(fake_service.calls) == 2

    asyncio.run(run_case())


def test_chat_ask_user_still_returns_user_facing_clarification():
    async def run_case():
        plan = {"action": "ask_user", "final_answer": "Which file should I inspect?"}
        pipeline, assistant, _fake_service, _ = make_chat_pipeline(plan)
        result = await pipeline.run(
            assistant=assistant,
            question="inspect it",
            payload={"_selected_skill_names": ["domain"]},
            session_id="chat-ask-user",
            turn_id=1,
            trace=[],
        )

        assert result["mode"] == "ask_user"
        assert result["terminal_state"] == "waiting_for_user"
        assert result["answer"] == "Which file should I inspect?"

    asyncio.run(run_case())


def test_final_emit_handoff_returns_completed_downstream_handoff():
    async def run_case():
        plan = {
            "action": "final",
            "response_mode": "emit_handoff",
            "final_answer": "done",
            "downstream_handoff": {"summary": "done", "status": "success", "facts": {"ok": True}},
        }
        pipeline, assistant, _fake_service, _ = make_chat_pipeline(plan)
        result = await pipeline.run(
            assistant=assistant,
            question="finish",
            payload={"_selected_skill_names": ["domain"]},
            session_id="chat-final-handoff",
            turn_id=1,
            trace=[],
        )

        assert result["mode"] == "final"
        assert result["terminal_state"] == "completed"
        assert result["downstream_handoff"]["summary"] == "done"

    asyncio.run(run_case())


def test_shell_exit_code_nonzero_normalizes_as_recoverable_tool_result():
    async def run_case():
        service = SequencedToolService([
            {"exit_code": 2, "stdout": "out", "stderr": "bad", "command": "fake"},
        ])
        runner = make_runner(service)
        tc = normal_call(1)
        results = await runner.execute_tool_calls(
            tool_calls=[tc],
            session_id="tool-error",
            turn_id=1,
            trace=[],
            assistant_name="test",
            trace_fn=trace_fn,
            preview_fn=lambda x: x,
        )

        assert results[0]["status"] == "failed"
        assert results[0]["error_type"] == "command_failed"
        assert results[0]["recoverable"] is True
        assert results[0]["exit_code"] == 2
        assert results[0]["stderr_preview"] == "bad"

    asyncio.run(run_case())


def test_workflow_recoverable_tool_error_continues_same_operation_until_final():
    async def run_case():
        plan = [
            {"action": "tool_calls", "tool_calls": [normal_call(1)]},
            {"action": "final", "final_answer": "workflow recovered", "downstream_handoff": {"summary": "workflow recovered", "status": "success"}},
        ]
        service = SequencedToolService([
            {"exit_code": 1, "stdout": "", "stderr": "missing workflow file", "command": "fake"},
        ])
        pipeline, assistant, fake_service, openai = make_chat_pipeline(plan, service)

        result = await pipeline.run(
            assistant=assistant,
            question="workflow recover",
            payload={"_selected_skill_names": ["domain"], "_workflow_background": True},
            session_id="workflow-recover",
            turn_id=1,
            trace=[],
        )

        assert result["mode"] == "final"
        assert result["terminal_state"] == "completed"
        assert result["downstream_handoff"]["summary"] == "workflow recovered"
        assert result["tool_results"] == []
        assert len(fake_service.calls) == 1
        assert len(openai.calls) == 2
        assert any(t.get("type") == "tool_execution_failed_recoverable" for t in result["trace"])
        assert any(t.get("type") == "agent_loop_continue_after_tool_result" for t in result["trace"])

    asyncio.run(run_case())


def test_workflow_ask_user_always_fails_never_waits():
    # Workflows are fully autonomous: ask_user never pauses, it fails with the question.
    async def run_case():
        plan = {"action": "ask_user", "final_answer": "Which workspace should I use?"}
        pipeline, assistant, _fake_service, _ = make_chat_pipeline(plan)
        result = await pipeline.run(
            assistant=assistant,
            question="run workflow",
            payload={"_selected_skill_names": ["domain"], "_workflow_background": True},
            session_id="workflow-ask-user",
            turn_id=1,
            trace=[],
        )
        assert result["mode"] == "error"
        assert result["terminal_state"] == "failed"
        assert result.get("pending_action") is None
        assert "Which workspace should I use?" in (result.get("answer") or "")

    asyncio.run(run_case())


def test_workflow_ask_user_autonomous_default_fails_instead_of_waiting():
    # §5: without _allow_user_questions, a workflow ask_user must NOT park — it fails
    # the operation with the question as the reason (autonomous by default).
    async def run_case():
        plan = {"action": "ask_user", "final_answer": "Which workspace should I use?"}
        pipeline, assistant, _fake_service, _ = make_chat_pipeline(plan)
        result = await pipeline.run(
            assistant=assistant,
            question="run workflow",
            payload={"_selected_skill_names": ["domain"], "_workflow_background": True},
            session_id="workflow-ask-user-autonomous",
            turn_id=1,
            trace=[],
        )
        assert result["mode"] == "error"
        assert result["terminal_state"] == "failed"
        assert result.get("pending_action") is None
        assert "Which workspace should I use?" in (result.get("answer") or "")

    asyncio.run(run_case())


def test_workflow_ask_user_empty_question_fails_even_when_allowed():
    # §5: an empty question never parks, even with questions allowed.
    async def run_case():
        plan = {"action": "ask_user", "final_answer": "   "}
        pipeline, assistant, _fake_service, _ = make_chat_pipeline(plan)
        result = await pipeline.run(
            assistant=assistant,
            question="run workflow",
            payload={"_selected_skill_names": ["domain"], "_workflow_background": True, "_allow_user_questions": True},
            session_id="workflow-ask-user-empty",
            turn_id=1,
            trace=[],
        )
        assert result["mode"] == "error"
        assert result["terminal_state"] == "failed"

    asyncio.run(run_case())

class FakeWorkflowRunRepoForResume:
    def __init__(self, pending_state, *, run_status="waiting"):
        self.db = None
        self.run = SimpleNamespace(id=456, workflow_id=789, status=run_status, input_payload={}, operation_runs=[])
        self.operation_run = SimpleNamespace(
            id=321,
            workflow_run_id=456,
            workflow_operation_id=123,
            status="waiting_for_user_input",
            progress_payload={"pending_state": pending_state, "resume_history": []},
            output_payload=None,
            error=None,
            trace=[],
            operation=SimpleNamespace(name="Collect"),
        )
        self.run.operation_runs = [self.operation_run]
        self.finished = None
        self.failed = None
        self.cancelled = None
        self.run_failed = None
        self.run_cancelled = None
        self.child_runs = []
        self.progress_updates = []
        self.cancel_child_calls = []
        self.cancel_sibling_calls = []

    def get_run(self, run_id):
        return self.run

    def get_run_with_operations(self, run_id):
        return self.run

    def get_waiting_operation_run(self, *, run_id, operation_id=None):
        if self.operation_run.status.startswith("waiting") and (operation_id is None or operation_id == self.operation_run.workflow_operation_id):
            return self.operation_run
        return None

    def get_operation_run(self, operation_run_id):
        return self.operation_run

    def create_run(self, **kwargs):
        run = SimpleNamespace(
            id=1000 + len(self.child_runs),
            workflow_id=kwargs.get("workflow_id"),
            input_payload=kwargs.get("input_payload") or {},
            status="queued",
            parent_run_id=kwargs.get("parent_run_id"),
            parent_operation_run_id=kwargs.get("parent_operation_run_id"),
            parent_item_index=kwargs.get("parent_item_index"),
        )
        self.child_runs.append(run)
        return run

    def update_operation_run_progress(self, operation_run_id, progress_payload):
        self.progress_updates.append(progress_payload)
        return self.operation_run

    def request_cancel_child_runs(self, parent_run_id):
        self.cancel_child_calls.append(parent_run_id)
        return []

    def request_cancel_for_each_sibling_runs(self, **kwargs):
        self.cancel_sibling_calls.append(kwargs)
        return []

    def mark_running(self, run_id):
        self.run.status = "running"
        return self.run

    def mark_operation_running(self, operation_run_id):
        self.operation_run.status = "running"
        return self.operation_run

    def mark_waiting_operation_run(self, operation_run_id, *, status, pending_state, trace=None, output_payload=None):
        self.operation_run.status = status
        self.operation_run.progress_payload = {"pending_state": pending_state}
        self.operation_run.trace = trace or []
        self.operation_run.output_payload = output_payload
        return self.operation_run

    def append_operation_resume_history(self, operation_run_id, item):
        progress = dict(self.operation_run.progress_payload or {})
        progress.setdefault("resume_history", []).append(item)
        self.operation_run.progress_payload = progress
        return self.operation_run

    def create_operation_run(self, *, workflow_run_id, workflow_operation_id, input_payload=None):
        self.operation_run = SimpleNamespace(
            id=999,
            workflow_run_id=workflow_run_id,
            workflow_operation_id=workflow_operation_id,
            status="running",
            input_payload=input_payload or {},
            output_payload=None,
            error=None,
            trace=[],
            progress_payload={},
            operation=SimpleNamespace(name="Collect"),
        )
        self.run.operation_runs = [self.operation_run]
        return self.operation_run

    def finish_operation_run(self, operation_run_id, *, output_payload=None, trace=None):
        self.operation_run.status = "success"
        self.operation_run.output_payload = output_payload
        self.operation_run.trace = trace
        self.finished = output_payload
        return self.operation_run

    def mark_finished(self, run_id, *, result_payload=None):
        self.run.status = "success"
        self.run.result_payload = result_payload
        return self.run

    def mark_waiting(self, run_id, *, result_payload=None):
        self.run.status = "waiting"
        self.run.result_payload = result_payload
        return self.run

    def fail_operation_run(self, operation_run_id, *, error, output_payload=None, trace=None):
        self.operation_run.status = "failed"
        self.operation_run.error = error
        self.operation_run.output_payload = output_payload
        self.operation_run.trace = trace
        self.failed = {"error": error, "output_payload": output_payload, "trace": trace}
        return self.operation_run

    def mark_failed(self, run_id, *, error, result_payload=None):
        self.run.status = "failed"
        self.run.error = error
        self.run.result_payload = result_payload
        self.run_failed = {"error": error, "result_payload": result_payload}
        return self.run

    def mark_operation_cancelled(self, operation_run_id, *, error="cancelled", output_payload=None, trace=None):
        self.operation_run.status = "cancelled"
        self.operation_run.error = error
        self.operation_run.output_payload = output_payload
        self.operation_run.trace = trace
        self.cancelled = {"error": error, "output_payload": output_payload, "trace": trace}
        return self.operation_run

    def mark_cancelled(self, run_id, result_payload=None, error=None):
        self.run.status = "cancelled"
        self.run.error = error
        self.run.result_payload = result_payload
        self.run_cancelled = {"error": error, "result_payload": result_payload}
        return self.run

    def is_cancel_requested(self, run_id):
        return False


class FakeWorkflowRepoForResume:
    def __init__(self, operation):
        self.workflow = SimpleNamespace(id=789, operations=[operation])

    def get_with_operations(self, workflow_id):
        return self.workflow


class FakeAssistantRunnerForResume:
    def __init__(self, result=None):
        self.calls = []
        self.pipeline_runner = SimpleNamespace(
            tool_runner=None,
            trace_fn=trace_fn,
        )
        self.result = result or {"mode": "final", "answer": "done", "trace": [], "terminal_state": "completed"}

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class FakeToolRunnerForApproval:
    def __init__(self):
        self.calls = []

    async def execute_tool_calls(self, **kwargs):
        self.calls.append(kwargs)
        return [{"status": "success", "tool": "system__shell_exec"}]


def make_resume_executor(pending_state, *, operation_config=None, assistant_runner=None):
    operation = SimpleNamespace(id=123, name="Collect", operation_ref_id=10, config=operation_config or {})
    repo = FakeWorkflowRunRepoForResume(pending_state)
    workflow_repo = FakeWorkflowRepoForResume(operation)
    runner = assistant_runner or FakeAssistantRunnerForResume()
    executor = WorkflowExecutor(
        workflow_repository=workflow_repo,
        run_repository=repo,
        assistant_runner=runner,
    )
    return executor, repo, runner, operation


def test_workflow_user_input_resume_appends_truncated_history_item():
    async def run_case():
        long_answer = "x" * 400
        pending_state = {
            "type": "workflow_user_input",
            "question": "Which workspace?",
            "resume_payload": {"question": "continue", "payload": {}, "agent_loop_state": {}},
        }
        executor, repo, runner, _operation = make_resume_executor(pending_state)

        await executor.resume_waiting_operation(
            run_id=456,
            operation_id=123,
            resume={"type": "user_input", "answer": long_answer},
            resume_by={"email": "operator@example.com"},
        )

        history = repo.operation_run.progress_payload["resume_history"]
        assert history[0]["type"] == "user_input"
        assert history[0]["by"] == "operator@example.com"
        assert len(history[0]["answer_preview"]) <= 300
        assert history[0]["answer_preview"] != long_answer
        assert runner.calls[0]["payload"]["_workflow_user_answer"] == long_answer

    asyncio.run(run_case())


def test_workflow_approval_resume_appends_approved_history_and_uses_hash_gate():
    async def run_case():
        tool = shell_call("python fabric_collect.py", 30)
        expected_hash = tool_call_hash(tool)
        pending_state = {
            "type": "workflow_tool_approval",
            "tool": "system__shell_exec",
            "display": {"command": "python fabric_collect.py", "timeout": 30},
            "tool_call_hash": expected_hash,
            "tool_call": tool,
            "resume_payload": {"question": "continue", "payload": {}, "agent_loop_state": {}},
        }
        tool_runner = FakeToolRunnerForApproval()
        assistant_runner = FakeAssistantRunnerForResume()
        assistant_runner.pipeline_runner.tool_runner = tool_runner
        executor, repo, _runner, _operation = make_resume_executor(pending_state, assistant_runner=assistant_runner)

        await executor.resume_waiting_operation(
            run_id=456,
            operation_id=123,
            resume={"type": "approval", "approved": True},
            resume_by={"id": 7},
        )

        history = repo.operation_run.progress_payload["resume_history"]
        assert history[0]["status"] == "approved"
        assert history[0]["command_preview"] == "python fabric_collect.py"
        assert history[0]["tool_call_hash"] == expected_hash
        assert tool_runner.calls[0]["confirmed_tool_call_hashes"] == {expected_hash}

    asyncio.run(run_case())


def test_workflow_approval_rejection_sets_metadata_and_history():
    async def run_case():
        tool = shell_call("python dangerous.py", 30)
        pending_state = {
            "type": "workflow_tool_approval",
            "tool": "system__shell_exec",
            "display": {"command": "python dangerous.py", "timeout": 30},
            "tool_call_hash": tool_call_hash(tool),
            "tool_call": tool,
            "resume_payload": {"question": "continue", "payload": {}, "agent_loop_state": {}},
        }
        executor, repo, _runner, _operation = make_resume_executor(pending_state)

        result = await executor.resume_waiting_operation(
            run_id=456,
            operation_id=123,
            resume={"type": "approval", "approved": False, "reason": "Not allowed"},
            resume_by={"email": "expert@example.com"},
        )

        history = repo.operation_run.progress_payload["resume_history"]
        assert history[0]["status"] == "rejected"
        assert history[0]["reason"] == "Not allowed"
        assert repo.failed["output_payload"]["type"] == "workflow_tool_approval_rejected"
        assert repo.failed["output_payload"]["status"] == "rejected"
        assert repo.failed["output_payload"]["command_preview"] == "python dangerous.py"
        assert repo.failed["error"] == "Workflow approval rejected: Not allowed"
        assert repo.run_failed["error"] == "Workflow approval rejected: Not allowed"
        assert result["status"] == "failed"
        assert any(t.get("type") == "workflow_rejection_handled" for t in repo.failed["trace"])

    asyncio.run(run_case())


def test_workflow_approval_rejection_unsupported_mode_fails_clearly():
    async def run_case():
        tool = shell_call("python dangerous.py", 30)
        pending_state = {
            "type": "workflow_tool_approval",
            "tool": "system__shell_exec",
            "display": {"command": "python dangerous.py", "timeout": 30},
            "tool_call_hash": tool_call_hash(tool),
            "tool_call": tool,
            "resume_payload": {"question": "continue", "payload": {}, "agent_loop_state": {}},
        }
        executor, _repo, _runner, _operation = make_resume_executor(
            pending_state,
            operation_config={"on_approval_rejected": {"mode": "branch"}},
        )

        with pytest.raises(RuntimeError, match="unsupported rejection handling mode"):
            await executor.resume_waiting_operation(
                run_id=456,
                operation_id=123,
                resume={"type": "approval", "approved": False, "reason": "No"},
            )

    asyncio.run(run_case())


def test_waiting_policy_sets_expiration_on_pending_state():
    pending_state = {"type": "workflow_user_input"}
    executor, _repo, _runner, operation = make_resume_executor(
        pending_state,
        operation_config={"waiting_policy": {"timeout_minutes": 10, "on_timeout": "keep_waiting"}},
    )
    trace = []

    prepared = executor._prepare_waiting_pending_state(operation, pending_state, trace)

    assert prepared["created_at"]
    assert prepared["expires_at"]
    assert prepared["waiting_policy"]["timeout_minutes"] == 10.0
    assert prepared["waiting_policy"]["on_timeout"] == "keep_waiting"
    assert any(t.get("type") == "workflow_waiting_timeout_set" for t in trace)


def test_pending_response_sanitizes_resume_payload_and_shows_expired_keep_waiting():
    service = WorkflowRunService.__new__(WorkflowRunService)
    pending_state = {
        "type": "workflow_user_input",
        "question": "Which workspace?",
        "resume_payload": {"payload": {"secret": "do-not-return"}},
        "created_at": (datetime.utcnow() - timedelta(minutes=2)).replace(microsecond=0).isoformat() + "Z",
        "expires_at": (datetime.utcnow() - timedelta(minutes=1)).replace(microsecond=0).isoformat() + "Z",
        "waiting_policy": {"timeout_minutes": 1, "on_timeout": "keep_waiting"},
    }
    repo = FakeWorkflowRunRepoForResume(pending_state)
    service.run_repository = repo

    result = service.get_pending(456, 123)

    assert result["expired"] is True
    assert result["pending"]["expired"] is True
    assert "resume_payload" not in result["pending"]
    assert result["pending"]["question"] == "Which workspace?"


def test_waiting_timeout_fail_marks_run_and_operation_failed():
    service = WorkflowRunService.__new__(WorkflowRunService)
    pending_state = {
        "type": "workflow_user_input",
        "expires_at": (datetime.utcnow() - timedelta(minutes=1)).replace(microsecond=0).isoformat() + "Z",
        "waiting_policy": {"timeout_minutes": 1, "on_timeout": "fail"},
    }
    repo = FakeWorkflowRunRepoForResume(pending_state)
    service.run_repository = repo

    with pytest.raises(Exception) as exc:
        service.get_pending(456, 123)

    assert getattr(exc.value, "status_code", None) == 409
    assert repo.operation_run.status == "failed"
    assert repo.run.status == "failed"
    assert repo.failed["output_payload"]["type"] == "workflow_waiting_timeout_expired"
    assert "waiting timeout expired" in repo.run.error


def test_waiting_timeout_cancel_marks_run_and_operation_cancelled():
    service = WorkflowRunService.__new__(WorkflowRunService)
    pending_state = {
        "type": "workflow_tool_approval",
        "expires_at": (datetime.utcnow() - timedelta(minutes=1)).replace(microsecond=0).isoformat() + "Z",
        "waiting_policy": {"timeout_minutes": 1, "on_timeout": "cancel"},
    }
    repo = FakeWorkflowRunRepoForResume(pending_state)
    service.run_repository = repo

    with pytest.raises(Exception) as exc:
        service.get_pending(456, 123)

    assert getattr(exc.value, "status_code", None) == 409
    assert repo.operation_run.status == "cancelled"
    assert repo.run.status == "cancelled"
    assert repo.cancelled["output_payload"]["status"] == "cancelled"


def test_malformed_waiting_policy_does_not_crash_and_fails_safe():
    pending_state = {"type": "workflow_user_input"}
    executor, _repo, _runner, operation = make_resume_executor(
        pending_state,
        operation_config={"waiting_policy": {"timeout_minutes": "bad", "on_timeout": "unsupported"}},
    )

    prepared = executor._prepare_waiting_pending_state(operation, pending_state, [])

    assert prepared["waiting_policy"]["on_timeout"] == "fail"
    assert prepared["waiting_policy"]["invalid"] is True
    assert "expires_at" not in prepared


def wf_context():
    return {
        "workflow_id": 789,
        "workflow_run_id": 456,
        "input": {"analysis_date": "2026-06-01", "flag": True},
        "workflow_variables": {},
        "operation_outputs": {
            1: {"downstream_handoff": {"status": "success", "facts": {"report_name": "r.md", "ok": True}}, "items": [1]},
            2: {"downstream_handoff": {"status": "failed", "facts": {"other": 2}}, "items": [2]},
        },
        "operation_statuses": {1: "success", 2: "success"},
        "operations": [SimpleNamespace(id=1, name="collect"), SimpleNamespace(id=2, name="analyse")],
    }


def wf_operation(operation_type, config=None, *, op_id=10, depends_on=None, timeout_seconds=None, retry_policy=None):
    return SimpleNamespace(
        id=op_id,
        name=f"op_{op_id}",
        operation_type=operation_type,
        operation_ref_id=0,
        config=config or {},
        depends_on=depends_on or [],
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy or {},
        on_success_follow_up=None,
        on_failure_follow_up=None,
    )


def make_executor_for_ops():
    pending_state = {"type": "workflow_user_input"}
    executor, repo, runner, _ = make_resume_executor(pending_state)
    return executor, repo, runner


def test_condition_equals_true_selects_then_branch_and_skips_else():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        op = wf_operation("condition", {
            "condition": {"source": "operation_output", "operation_id": 1, "path": "downstream_handoff.status", "operator": "equals", "value": "success"},
            "then_operation_ids": [20],
            "else_operation_ids": [30],
        })
        result = await executor._execute_condition_operation(op, {}, wf_context())
        assert result["result"] is True
        assert result["selected_branch"] == "then"
        assert result["skipped_operation_ids"] == [30]
        assert any(t.get("type") == "workflow_condition_evaluated" for t in result["trace"])
    asyncio.run(run_case())


def test_condition_false_exists_not_exists_and_unsupported_operator():
    executor, _repo, _runner = make_executor_for_ops()
    context = wf_context()
    op = wf_operation("condition", {"condition": {"source": "workflow_input", "path": "missing", "operator": "not_exists"}})
    found, value = executor._condition_source_value(op.config["condition"], op, context)
    assert executor._evaluate_condition_operator(found, value, "not_exists", None) is True
    assert executor._evaluate_condition_operator(found, value, "exists", None) is False
    with pytest.raises(ValueError, match="Unsupported condition operator"):
        executor._evaluate_condition_operator(True, "x", "bad", "x")


def test_condition_branch_ids_integer_only_and_workflow_variables_source():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        context = wf_context()
        context["workflow_variables"] = {"var": "ja"}
        op = wf_operation("condition", {
            "condition": {"source": "workflow_variables", "path": "var", "operator": "equals", "value": "ja"},
            "then_operation_ids": [140],
            "else_operation_ids": ["141"],
        })
        result = await executor._execute_condition_operation(op, {}, context)
        assert result["result"] is True
        assert result["selected_operation_ids"] == [140]
        assert result["skipped_operation_ids"] == [141]
        assert all(isinstance(value, int) for value in result["selected_operation_ids"] + result["skipped_operation_ids"])
        assert any(t.get("type") == "workflow_condition_branch_ids_validated" for t in result["trace"])

        bad = wf_operation("condition", {
            "condition": {"source": "workflow_variables", "path": "var", "operator": "equals", "value": "ja"},
            "then_operation_ids": ["tmp-1780307590701-4"],
            "else_operation_ids": [],
        })
        with pytest.raises(ValueError, match="condition branch operation id must be an integer: tmp-1780307590701-4"):
            await executor._execute_condition_operation(bad, {}, context)
    asyncio.run(run_case())


def test_set_variable_then_condition_without_output_contract_selects_branch():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        context = wf_context()
        set_op = wf_operation("set_variable", {"variables": {"var": "ja"}}, op_id=139)
        set_result = await executor._execute_set_variable_operation(set_op, {}, context)
        assert set_result["variables_set"] == {"var": "ja"}

        condition_op = wf_operation("condition", {
            "condition": {"source": "workflow_variables", "path": "var", "operator": "equals", "value": "ja"},
            "then_operation_ids": [140],
            "else_operation_ids": [],
        })
        result = await executor._execute_condition_operation(condition_op, {}, context)
        assert result["selected_branch"] == "then"
        assert result["selected_operation_ids"] == [140]

        context["workflow_variables"] = {"var": "nee"}
        result = await executor._execute_condition_operation(condition_op, {}, context)
        assert result["selected_branch"] == "else"
        assert result["selected_operation_ids"] == []
    asyncio.run(run_case())


def test_set_variable_static_input_and_previous_output_resolution():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        context = wf_context()
        op = wf_operation("set_variable", {
            "variables": {
                "report_name": "fabric_capacity_analysis.md",
                "analysis_date": "${workflow_input.analysis_date}",
                "handoff_status": "${operation.collect.downstream_handoff.status}",
            }
        }, depends_on=[1])
        result = await executor._execute_set_variable_operation(op, {}, context)
        assert result["variables_set"]["report_name"] == "fabric_capacity_analysis.md"
        assert result["variables_set"]["analysis_date"] == "2026-06-01"
        assert result["variables_set"]["handoff_status"] == "success"
        assert context["workflow_variables"]["handoff_status"] == "success"
    asyncio.run(run_case())


def test_set_variable_missing_path_fails_clearly():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        op = wf_operation("set_variable", {"variables": {"x": "${workflow_input.missing}"}})
        with pytest.raises(ValueError, match="Unable to resolve"):
            await executor._execute_set_variable_operation(op, {}, wf_context())
    asyncio.run(run_case())


def test_merge_collect_handoffs_merge_objects_and_missing_modes():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        collect = wf_operation("merge", {"inputs": [{"operation_id": 1}, {"operation_id": 2}], "strategy": "collect_handoffs", "output_key": "handoffs"})
        result = await executor._execute_merge_operation(collect, {}, wf_context())
        assert [x["status"] for x in result["handoffs"]] == ["success", "failed"]
        merge = wf_operation("merge", {"inputs": [{"operation_id": 1, "path": "downstream_handoff.facts"}, {"operation_id": 2, "path": "downstream_handoff.facts"}], "strategy": "merge_objects"})
        merged = await executor._execute_merge_operation(merge, {}, wf_context())
        assert merged["merged_results"]["report_name"] == "r.md"
        assert merged["merged_results"]["other"] == 2
        warn = wf_operation("merge", {"inputs": [{"operation_id": 99, "path": "x"}], "strategy": "collect"})
        warned = await executor._execute_merge_operation(warn, {}, wf_context())
        assert warned["warnings"]
        strict = wf_operation("merge", {"inputs": [{"operation_id": 99, "path": "x"}], "strategy": "collect", "strict": True})
        with pytest.raises(ValueError, match="missing input"):
            await executor._execute_merge_operation(strict, {}, wf_context())
    asyncio.run(run_case())


def test_wait_operation_small_duration_and_invalid_values():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        result = await executor._execute_wait_operation(wf_operation("wait", {"duration_seconds": 0.001}), {}, wf_context())
        assert result["status"] == "success"
        with pytest.raises(ValueError, match="positive"):
            await executor._execute_wait_operation(wf_operation("wait", {"duration_seconds": 0}), {}, wf_context())
        with pytest.raises(ValueError):
            await executor._execute_wait_operation(wf_operation("wait", {"until": "not-a-date"}), {}, wf_context())
    asyncio.run(run_case())


def test_notification_trace_channel_does_not_send_and_templates_resolve(monkeypatch):
    async def run_case():
        calls = []
        monkeypatch.setattr("services.workflows.workflow_executor.send_system_notification", lambda **kwargs: calls.append(kwargs) or True)
        executor, _repo, _runner = make_executor_for_ops()
        op = wf_operation("notification", {"channel": "trace", "subject": "Done ${workflow_input.analysis_date}", "message": "Report ${operation.collect.downstream_handoff.facts.report_name}", "severity": "info"})
        result = await executor._execute_notification_operation(op, {}, wf_context())
        assert result["channel"] == "trace"
        assert result["subject"] == "Done 2026-06-01"
        assert result["message"] == "Report r.md"
        assert result["sent"] is False
        assert calls == []
        assert any(t.get("type") == "workflow_notification_created" for t in result["trace"])
        assert "Authorization" not in json.dumps(result)
    asyncio.run(run_case())


def test_notification_ui_channel_sends_system_notification(monkeypatch):
    async def run_case():
        calls = []
        def fake_send(**kwargs):
            calls.append(kwargs)
            return True
        monkeypatch.setattr("services.workflows.workflow_executor.send_system_notification", fake_send)
        executor, repo, _runner = make_executor_for_ops()
        repo.db = "db-session"
        op = wf_operation("notification", {"channel": "ui", "subject": "Done", "message": "Report ${operation.collect.downstream_handoff.facts.report_name}", "severity": "info"})
        result = await executor._execute_notification_operation(op, {}, wf_context())
        assert result["channel"] == "ui"
        assert result["message"] == "Report r.md"
        assert result["sent"] is True
        assert calls[0]["db"] == "db-session"
        assert calls[0]["subject"] == "Done"
        assert calls[0]["title"] == "Done"
        assert calls[0]["message"] == "Report r.md"
        assert calls[0]["data"] == {"Workflow ID": 789, "Workflow run ID": 456, "Operation ID": 10, "Severity": "info", "Channel": "ui"}
        assert calls[0]["action_url"] == "https://www.nd3x.nl/workflows/runs/456"
        assert any(t.get("type") == "workflow_notification_sent" for t in result["trace"])
    asyncio.run(run_case())


def test_notification_ui_send_failure_warning_or_fail(monkeypatch):
    async def run_case():
        monkeypatch.setattr("services.workflows.workflow_executor.send_system_notification", lambda **kwargs: False)
        executor, _repo, _runner = make_executor_for_ops()
        op = wf_operation("notification", {"channel": "ui", "subject": "Done", "message": "Message"})
        result = await executor._execute_notification_operation(op, {}, wf_context())
        assert result["sent"] is False
        assert result["warning"] == "workflow notification failed"
        assert any(t.get("type") == "workflow_notification_failed" for t in result["trace"])

        failing = wf_operation("notification", {"channel": "ui", "subject": "Done", "message": "Message", "fail_on_notification_error": True})
        with pytest.raises(ValueError, match="workflow notification failed"):
            await executor._execute_notification_operation(failing, {}, wf_context())

        def raise_send(**kwargs):
            raise RuntimeError("mail down")
        monkeypatch.setattr("services.workflows.workflow_executor.send_system_notification", raise_send)
        result = await executor._execute_notification_operation(op, {}, wf_context())
        assert result["sent"] is False
        assert "mail down" in result["warning"]
        assert any(t.get("type") == "workflow_notification_failed" for t in result["trace"])
    asyncio.run(run_case())


class FakeHTTPResponse:
    def __init__(self, status_code=200, text='{"ok": true}', headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "application/json", "set-cookie": "secret"}
    def json(self):
        return json.loads(self.text)


class FakeHTTPClient:
    calls = []
    response = FakeHTTPResponse()
    exc = None
    def __init__(self, timeout=None):
        self.timeout = timeout
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def request(self, *args, **kwargs):
        FakeHTTPClient.calls.append({"args": args, "kwargs": kwargs, "timeout": self.timeout})
        if FakeHTTPClient.exc:
            raise FakeHTTPClient.exc
        return FakeHTTPClient.response


def test_http_request_mocked_success_failure_redaction_and_invalid_url(monkeypatch):
    async def run_case():
        monkeypatch.setattr("services.workflows.workflow_executor.httpx.AsyncClient", FakeHTTPClient)
        FakeHTTPClient.calls = []
        FakeHTTPClient.response = FakeHTTPResponse(200, '{"ok": true}', {"authorization": "secret", "x-api-key": "k"})
        FakeHTTPClient.exc = None
        executor, _repo, _runner = make_executor_for_ops()
        op = wf_operation("http_request", {"method": "GET", "url": "https://api.example.com/status", "headers": {"Authorization": "secret"}, "response_mode": "json"})
        result = await executor._execute_http_request_operation(op, {}, wf_context())
        assert result["response"] == {"ok": True}
        assert result["headers"]["authorization"] == "[redacted]"
        FakeHTTPClient.response = FakeHTTPResponse(500, "bad", {})
        bad = wf_operation("http_request", {"url": "https://api.example.com/status", "fail_on_non_2xx": True, "response_mode": "text"})
        with pytest.raises(ValueError, match="status 500"):
            await executor._execute_http_request_operation(bad, {}, wf_context())
        with pytest.raises(ValueError, match=r"http\(s\)"):
            await executor._execute_http_request_operation(wf_operation("http_request", {"url": "file:///tmp/x"}), {}, wf_context())
        with pytest.raises(ValueError, match="internal"):
            await executor._execute_http_request_operation(wf_operation("http_request", {"url": "http://127.0.0.1/x"}), {}, wf_context())
        FakeHTTPClient.exc = httpx.TimeoutException("timeout")
        with pytest.raises(httpx.TimeoutException):
            await executor._execute_http_request_operation(op, {}, wf_context())
    asyncio.run(run_case())


def test_artifact_save_text_and_json(tmp_path):
    async def run_case():
        old_dir = settings.FILES_DIR
        settings.FILES_DIR = str(tmp_path)
        try:
            executor, _repo, _runner = make_executor_for_ops()
            text_op = wf_operation("artifact", {"action": "save_text", "name": "report.md", "content_from": "operation.collect.downstream_handoff.facts.report_name", "content_type": "text/markdown"})
            text_result = await executor._execute_artifact_operation(text_op, {}, wf_context())
            assert Path(text_result["artifact"]["path"]).read_text() == "r.md"
            json_op = wf_operation("artifact", {"action": "save_json", "name": "facts.json", "content_from": "operation.collect.downstream_handoff.facts"})
            json_result = await executor._execute_artifact_operation(json_op, {}, wf_context())
            assert json.loads(Path(json_result["artifact"]["path"]).read_text())["report_name"] == "r.md"
            with pytest.raises(ValueError, match="content_from"):
                await executor._execute_artifact_operation(wf_operation("artifact", {"action": "save_text"}), {}, wf_context())
        finally:
            settings.FILES_DIR = old_dir
    asyncio.run(run_case())


def test_artifact_content_modes_and_clear_missing_reference(tmp_path):
    async def run_case():
        old_dir = settings.FILES_DIR
        settings.FILES_DIR = str(tmp_path)
        try:
            executor, _repo, _runner = make_executor_for_ops()
            context = wf_context()
            context["workflow_variables"] = {"var": "ja"}

            cases = [
                ({"action": "save_text", "name": "from-ref.txt", "content_from": "workflow_variables.var"}, "ja", "content_from"),
                ({"action": "save_text", "name": "from-template.txt", "content": "${workflow_variables.var}"}, "ja", "content"),
                ({"action": "save_text", "name": "from-compat.txt", "content_from": "${workflow_variables.var}"}, "ja", "content_from_template_compat"),
                ({"action": "save_text", "name": "literal.txt", "content": "hello"}, "hello", "content"),
            ]
            for config, expected, mode in cases:
                result = await executor._execute_artifact_operation(wf_operation("artifact", config), {}, context)
                assert Path(result["artifact"]["path"]).read_text() == expected
                resolved = next(t for t in result["trace"] if t.get("type") == "workflow_artifact_content_resolved")
                assert resolved["data"]["mode"] == mode

            with pytest.raises(ValueError, match="artifact content_from not found: workflow_variables.missing"):
                await executor._execute_artifact_operation(wf_operation("artifact", {"action": "save_text", "content_from": "workflow_variables.missing"}), {}, context)
            with pytest.raises(ValueError, match="artifact operation requires content or content_from"):
                await executor._execute_artifact_operation(wf_operation("artifact", {"action": "save_text", "name": "missing.txt"}), {}, context)
        finally:
            settings.FILES_DIR = old_dir
    asyncio.run(run_case())


def test_workflow_variables_serialized_and_reconstructed_from_set_variable_runs():
    executor, repo, _runner = make_executor_for_ops()
    context = wf_context()
    context["workflow_variables"] = {"var": "ja"}
    assert executor._serializable_context(context)["workflow_variables"]["var"] == "ja"

    repo.run.result_payload = {"workflow_variables": {"var": "old", "kept": "yes"}}
    repo.run.operation_runs = [
        SimpleNamespace(id=1, workflow_operation_id=1, status="success", output_payload={"mode": "set_variable", "variables_set": {"var": "first"}}, error=None),
        SimpleNamespace(id=2, workflow_operation_id=2, status="success", output_payload={"mode": "set_variable", "variables_set": {"var": "second", "new": "value"}}, error=None),
    ]
    rebuilt = executor._context_from_operation_runs(repo.run, SimpleNamespace(id=789, operations=[]))
    assert rebuilt["workflow_variables"] == {"var": "second", "kept": "yes", "new": "value"}
    assert any(t.get("type") == "workflow_variables_reconstructed" for t in rebuilt["workflow_reconstruction_trace"])


def test_workflow_waiting_and_failure_results_include_workflow_variables():
    async def run_case():
        executor, _repo, _runner = make_executor_for_ops()
        context = wf_context()
        set_op = wf_operation("set_variable", {"variables": {"var": "ja"}}, op_id=101)
        wait_op = wf_operation("assistant", {}, op_id=102, depends_on=[101])
        async def waiting_operation(operation, input_payload, ctx):
            return {"mode": "workflow_waiting", "status": "waiting_for_user_input", "pending_state": {"type": "workflow_user_input"}, "trace": []}
        executor._execute_assistant_operation = waiting_operation
        result = await executor._execute_operations([set_op, wait_op], context)
        assert result["status"] == "waiting"
        assert result["workflow_variables"]["var"] == "ja"

        failed_context = wf_context()
        failed_context["workflow_variables"] = {"var": "ja"}
        assert executor._serializable_context(failed_context)["workflow_variables"]["var"] == "ja"
    asyncio.run(run_case())


def test_output_contract_validation_pass_and_fail():
    executor, _repo, _runner = make_executor_for_ops()
    op = wf_operation("notification", {"output_contract": {"required_paths": ["downstream_handoff.status"], "status_path": "downstream_handoff.status", "success_values": ["success"]}})
    output = {"downstream_handoff": {"status": "success"}, "trace": []}
    assert executor._validate_output_contract(op, output)["valid"] is True
    bad = {"downstream_handoff": {"status": "failed"}, "trace": []}
    with pytest.raises(ValueError, match="output_contract_violation"):
        executor._validate_output_contract(op, bad)


def test_retry_policy_retries_configured_failure_and_exhaustion():
    async def run_case():
        pending_state = {"type": "workflow_user_input"}
        executor, repo, _runner, _ = make_resume_executor(pending_state)
        repo.create_operation_run = lambda **kwargs: SimpleNamespace(id=999, status="running")
        attempts = {"n": 0}
        async def flaky(operation, input_payload, context):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ValueError("temporarily unavailable")
            return {"status": "success", "trace": []}
        executor._execute_notification_operation = flaky
        op = wf_operation("notification", {"retry_policy": {"max_attempts": 2, "backoff_seconds": 0, "retry_on_error_contains": ["temporarily"]}})
        result = await executor._execute_single_operation(op, wf_context())
        assert result["attempt_count"] == 2
        assert attempts["n"] == 2
        attempts["n"] = 0
        async def always_bad(operation, input_payload, context):
            attempts["n"] += 1
            raise ValueError("temporarily unavailable")
        executor._execute_notification_operation = always_bad
        with pytest.raises(ValueError, match="temporarily unavailable"):
            await executor._execute_single_operation(op, wf_context())
        assert attempts["n"] == 2
    asyncio.run(run_case())


def test_operation_timeout_fails_with_trace():
    async def run_case():
        pending_state = {"type": "workflow_user_input"}
        executor, repo, _runner, _ = make_resume_executor(pending_state)
        repo.create_operation_run = lambda **kwargs: SimpleNamespace(id=999, status="running")
        async def slow(operation, input_payload, context):
            await asyncio.sleep(0.05)
            return {"status": "success"}
        executor._execute_notification_operation = slow
        op = wf_operation("notification", {"timeout_seconds": 0.01})
        with pytest.raises(Exception):
            await executor._execute_single_operation(op, wf_context())
        assert repo.failed is not None
    asyncio.run(run_case())


def test_input_mapping_static_workflow_input_operation_output_and_optional_default():
    executor, _repo, _runner = make_executor_for_ops()
    context = wf_context()
    op = wf_operation("notification", {
        "input_mapping": {
            "report_name": {"source": "static", "value": "fabric_capacity_analysis.md"},
            "analysis_date": {"source": "workflow_input", "path": "analysis_date", "required": True},
            "workspace_id": {"source": "operation_output", "operation_key": "collect", "path": "downstream_handoff.facts.report_name", "required": True},
            "optional": {"source": "workflow_input", "path": "missing", "required": False, "default": "fallback"},
        }
    })

    payload = executor._build_operation_input(op, context)

    assert payload["mapped_inputs"] == {
        "report_name": "fabric_capacity_analysis.md",
        "analysis_date": "2026-06-01",
        "workspace_id": "r.md",
        "optional": "fallback",
    }
    assert any(t.get("type") == "workflow_input_mapping_resolved" for t in payload["input_mapping_trace"])


def test_input_mapping_previous_output_workflow_variables_and_for_each_item():
    executor, _repo, _runner = make_executor_for_ops()
    context = wf_context()
    context["workflow_variables"] = {"workspace": {"id": "ws-1"}}
    context["input"] = {"name": "item-a", "nested": {"count": 3}, "_workflow_stack": [1]}
    op = wf_operation("notification", {
        "input_mapping": {
            "prev_status": {"source": "previous_operation_output", "path": "downstream_handoff.status", "required": True},
            "workspace_id": {"source": "workflow_variables", "path": "workspace.id", "required": True},
            "item_name": {"source": "for_each_item", "path": "name", "required": True},
            "item_count": {"source": "for_each_item", "path": "nested.count", "required": True},
        }
    }, depends_on=[1])

    payload = executor._build_operation_input(op, context)

    assert payload["mapped_inputs"]["prev_status"] == "success"
    assert payload["mapped_inputs"]["workspace_id"] == "ws-1"
    assert payload["mapped_inputs"]["item_name"] == "item-a"
    assert payload["mapped_inputs"]["item_count"] == 3


def test_input_mapping_required_missing_fails_before_operation_execution():
    async def run_case():
        pending_state = {"type": "workflow_user_input"}
        executor, repo, _runner, _ = make_resume_executor(pending_state)
        calls = {"notification": 0}
        async def should_not_run(operation, input_payload, context):
            calls["notification"] += 1
            return {"status": "success"}
        executor._execute_notification_operation = should_not_run
        op = wf_operation("notification", {
            "input_mapping": {
                "required_value": {"source": "workflow_input", "path": "missing", "required": True}
            }
        })

        with pytest.raises(Exception, match="input_mapping_failed"):
            await executor._execute_single_operation(op, wf_context())

        assert calls["notification"] == 0
        assert repo.failed["output_payload"]["error_type"] == "input_mapping_failed"
        assert repo.failed["output_payload"]["missing_inputs"] == ["required_value"]
        assert any(t.get("type") == "workflow_input_mapping_failed" for t in repo.failed["trace"])

    asyncio.run(run_case())


def test_input_mapping_mapped_inputs_passed_to_assistant_payload():
    async def run_case():
        pending_state = {"type": "workflow_user_input"}
        executor, repo, runner, operation = make_resume_executor(pending_state)
        repo.create_operation_run = lambda **kwargs: SimpleNamespace(id=999, status="running", input_payload=kwargs.get("input_payload"))
        operation.operation_type = "assistant"
        operation.config = {
            "skill_names": ["domain"],
            "input_mapping": {
                "analysis_date": {"source": "workflow_input", "path": "analysis_date", "required": True}
            }
        }
        operation.timeout_seconds = None
        operation.depends_on = []
        operation.on_success_follow_up = None
        operation.on_failure_follow_up = None

        result = await executor._execute_single_operation(operation, wf_context())

        assert result["mode"] == "final"
        assert runner.calls[0]["payload"]["mapped_inputs"]["analysis_date"] == "2026-06-01"
        assert repo.finished["mode"] == "final"

    asyncio.run(run_case())


def for_each_operation(config=None, *, op_id=77):
    op = wf_operation("for_each", config or {}, op_id=op_id)
    op.operation_ref_id = 222
    op.position = 5
    return op


def test_for_each_legacy_iterable_source_still_resolves_items():
    executor, _repo, _runner = make_executor_for_ops()
    context = wf_context()
    context["operations"] = [SimpleNamespace(id=1, name="collect", position=1)]
    context["operation_outputs"][1]["downstream_handoff"]["iterables"] = {"items": [{"name": "A"}]}
    op = for_each_operation({"iterable_source": {"operation_position": 1, "name": "items"}})

    items, iterable_name, summary, trace = executor._resolve_for_each_items(op, {"mapped_inputs": {}}, context)

    assert items == [{"name": "A"}]
    assert iterable_name == "items"
    assert summary["mode"] == "iterable_source"
    assert any(t.get("type") == "workflow_for_each_items_resolved" for t in trace)


def test_for_each_items_source_resolves_supported_sources():
    executor, _repo, _runner = make_executor_for_ops()
    context = wf_context()
    context["operation_outputs"][1]["downstream_handoff"]["iterables"] = {"items": [{"name": "from-op"}]}
    context["workflow_variables"] = {"selected_items": [{"name": "from-var"}]}
    context["input"]["items"] = [{"name": "from-input"}]
    cases = [
        ({"source": "operation_output", "operation_key": "collect", "path": "downstream_handoff.iterables.items", "required": True}, "from-op"),
        ({"source": "workflow_input", "path": "items", "required": True}, "from-input"),
        ({"source": "workflow_variables", "path": "selected_items", "required": True}, "from-var"),
        ({"source": "previous_operation_output", "path": "downstream_handoff.iterables.items", "required": True}, "from-op"),
        ({"source": "static", "value": [{"name": "from-static"}]}, "from-static"),
        ({"source": "mapped_inputs", "path": "items", "required": True}, "from-mapped"),
    ]
    input_payload = {"mapped_inputs": {"items": [{"name": "from-mapped"}]}}
    for spec, expected_name in cases:
        op = for_each_operation({"items_source": spec}, op_id=77)
        op.depends_on = [1]
        items, _name, summary, trace = executor._resolve_for_each_items(op, input_payload, context)
        assert items[0]["name"] == expected_name
        assert any(t.get("type") == "workflow_for_each_items_resolved" for t in trace)


def test_for_each_items_source_non_array_and_missing_required_fail_clearly():
    executor, _repo, _runner = make_executor_for_ops()
    non_array = for_each_operation({"items_source": {"source": "workflow_input", "path": "analysis_date", "required": True}})
    with pytest.raises(Exception, match="for_each_items_not_array") as non_array_exc:
        executor._resolve_for_each_items(non_array, {"mapped_inputs": {}}, wf_context())
    assert non_array_exc.value.output_payload["error_type"] == "for_each_items_not_array"

    missing = for_each_operation({"items_source": {"source": "workflow_input", "path": "missing", "required": True}})
    with pytest.raises(Exception, match="for_each_items_resolution_failed") as missing_exc:
        executor._resolve_for_each_items(missing, {"mapped_inputs": {}}, wf_context())
    assert missing_exc.value.output_payload["error_type"] == "for_each_items_resolution_failed"


def test_for_each_child_payload_includes_structured_item_and_compact_output():
    async def run_case():
        executor, repo, _runner = make_executor_for_ops()
        context = wf_context()
        op = for_each_operation({"items_source": {"source": "static", "value": [{"name": "A", "path": "/a"}]}, "result_key": "results"})
        async def fake_execute_run(run_id):
            return {"status": "success", "downstream_handoff": {"summary": f"child {run_id}", "facts": {"ok": True}}}
        executor.execute_run = fake_execute_run

        result = await executor._execute_for_each_operation(op, {"mapped_inputs": {}}, context, parent_operation_run_id=999)

        assert repo.child_runs[0].input_payload["for_each_item"] == {"name": "A", "path": "/a"}
        assert repo.child_runs[0].input_payload["for_each"]["index"] == 0
        assert result["items_count"] == 1
        assert result["success_count"] == 1
        assert result["results_key"] == "results"
        assert result["results"][0]["item_preview"] == {"name": "A", "path": "/a"}
        assert result["results"][0]["downstream_handoff"]["facts"]["ok"] is True
        assert any(t.get("type") == "workflow_for_each_item_started" for t in result["trace"])
        assert any(t.get("type") == "workflow_for_each_item_completed" for t in result["trace"])
        assert any(t.get("type") == "workflow_for_each_completed" for t in result["trace"])
    asyncio.run(run_case())


def test_for_each_child_input_mapping_reads_structured_for_each_item():
    async def run_case():
        executor, repo, _runner = make_executor_for_ops()
        op = for_each_operation({"items_source": {"source": "static", "value": [{"name": "A", "path": "/a"}]}})
        async def fake_execute_run(run_id):
            return {"status": "success"}
        executor.execute_run = fake_execute_run
        await executor._execute_for_each_operation(op, {"mapped_inputs": {}}, wf_context(), parent_operation_run_id=999)
        child_context = wf_context()
        child_context["input"] = repo.child_runs[0].input_payload
        child_op = wf_operation("notification", {
            "input_mapping": {
                "entity_name": {"source": "for_each_item", "path": "name", "required": True},
                "entity_path": {"source": "for_each_item", "path": "path", "required": False},
            }
        })
        payload = executor._build_operation_input(child_op, child_context)
        assert payload["mapped_inputs"] == {"entity_name": "A", "entity_path": "/a"}
    asyncio.run(run_case())


def test_for_each_failure_strategy_continue_and_stop():
    async def continue_case():
        executor, repo, _runner = make_executor_for_ops()
        op = for_each_operation({"items_source": {"source": "static", "value": [{"name": "bad"}, {"name": "ok"}]}, "failure_strategy": "continue"})
        async def fake_execute_run(run_id):
            # Correlate the failure to THIS run's item index. for_each items run
            # in parallel, so inspecting child_runs[-1] (the last-appended run) is
            # racy and nondeterministic across schedulers.
            child = next(c for c in repo.child_runs if c.id == run_id)
            if child.parent_item_index == 0:
                raise RuntimeError("child failed")
            return {"status": "success"}
        executor.execute_run = fake_execute_run
        result = await executor._execute_for_each_operation(op, {"mapped_inputs": {}}, wf_context(), parent_operation_run_id=999)
        assert result["status"] == "partial_success"
        assert result["failed_count"] == 1
        assert result["success_count"] == 1
        assert any(t.get("type") == "workflow_for_each_item_failed" for t in result["trace"])

    async def stop_case():
        executor, repo, _runner = make_executor_for_ops()
        op = for_each_operation({"items_source": {"source": "static", "value": [{"name": "bad"}]}, "failure_strategy": "stop"})
        async def fake_execute_run(run_id):
            raise RuntimeError("child failed")
        executor.execute_run = fake_execute_run
        with pytest.raises(RuntimeError, match="child failed"):
            await executor._execute_for_each_operation(op, {"mapped_inputs": {}}, wf_context(), parent_operation_run_id=999)
        assert repo.cancel_sibling_calls

    asyncio.run(continue_case())
    asyncio.run(stop_case())


def test_ask_user_question_falls_back_to_reason_non_workflow():
    # The model put the clarification in `reason`, not final_answer → still surfaced.
    async def run_case():
        plan = {"action": "ask_user", "final_answer": "", "reason": "Which workspace should I use?"}
        pipeline, assistant, _fake_service, _ = make_chat_pipeline(plan)
        result = await pipeline.run(
            assistant=assistant, question="hi",
            payload={"_selected_skill_names": ["domain"]},
            session_id="ask-fallback", turn_id=1, trace=[],
        )
        assert result["mode"] == "ask_user"
        assert "Which workspace should I use?" in (result.get("answer") or "")

    asyncio.run(run_case())


def test_ask_user_reason_surfaces_in_autonomous_workflow_failure():
    # In an autonomous workflow the question (from reason) appears in the failure reason.
    async def run_case():
        plan = {"action": "ask_user", "final_answer": "", "reason": "Which workspace should I use?"}
        pipeline, assistant, _fake_service, _ = make_chat_pipeline(plan)
        result = await pipeline.run(
            assistant=assistant, question="run workflow",
            payload={"_selected_skill_names": ["domain"], "_workflow_background": True},
            session_id="ask-fallback-wf", turn_id=1, trace=[],
        )
        assert result["mode"] == "error"
        assert "Which workspace should I use?" in (result.get("answer") or "")
