import asyncio
import json
from types import SimpleNamespace

import pytest

import services.workflows.workflow_executor as workflow_executor_module
from component.config import settings
from services.workflows.workflow_executor import WorkflowExecutor


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    monkeypatch.setattr(workflow_executor_module, "send_system_notification", lambda *a, **k: True)


class FakeRunRepo:
    def __init__(self):
        self.db = None
        self.runs = {}
        self.op_runs = {}
        self._next_run_id = 100
        self._next_op_run_id = 5000
        self.progress_updates = []
        self.cancel_child_calls = []
        self.cancel_sibling_calls = []

    def create_run(self, *, workflow_id, trigger_type, input_payload=None,
                   parent_run_id=None, parent_operation_run_id=None, parent_item_index=None):
        run = SimpleNamespace(
            id=self._next_run_id,
            workflow_id=workflow_id,
            trigger_type=trigger_type,
            status="queued",
            input_payload=input_payload or {},
            result_payload=None,
            error=None,
            parent_run_id=parent_run_id,
            parent_operation_run_id=parent_operation_run_id,
            parent_item_index=parent_item_index,
            operation_runs=[],
        )
        self._next_run_id += 1
        self.runs[run.id] = run
        return run

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def get_run_with_operations(self, run_id):
        return self.runs.get(run_id)

    def mark_running(self, run_id):
        run = self.runs.get(run_id)
        if run:
            run.status = "running"
        return run

    def mark_finished(self, run_id, *, result_payload=None):
        run = self.runs[run_id]
        run.status = "success"
        run.result_payload = result_payload
        return run

    def mark_failed(self, run_id, *, error, result_payload=None):
        run = self.runs[run_id]
        run.status = "failed"
        run.error = error
        run.result_payload = result_payload
        return run

    def mark_waiting(self, run_id, *, result_payload=None):
        run = self.runs[run_id]
        run.status = "waiting"
        run.result_payload = result_payload
        return run

    def mark_cancelled(self, run_id, result_payload=None, error=None):
        run = self.runs[run_id]
        run.status = "cancelled"
        run.result_payload = result_payload
        run.error = error
        return run

    def create_operation_run(self, *, workflow_run_id, workflow_operation_id, input_payload=None):
        op_run = SimpleNamespace(
            id=self._next_op_run_id,
            workflow_run_id=workflow_run_id,
            workflow_operation_id=workflow_operation_id,
            status="running",
            input_payload=input_payload or {},
            output_payload=None,
            error=None,
            trace=[],
            progress_payload={},
        )
        self._next_op_run_id += 1
        self.op_runs[op_run.id] = op_run
        self.runs[workflow_run_id].operation_runs.append(op_run)
        return op_run

    def finish_operation_run(self, operation_run_id, *, output_payload=None, trace=None):
        op_run = self.op_runs[operation_run_id]
        op_run.status = "success"
        op_run.output_payload = output_payload
        op_run.trace = trace
        return op_run

    def fail_operation_run(self, operation_run_id, *, error, output_payload=None, trace=None):
        op_run = self.op_runs[operation_run_id]
        op_run.status = "failed"
        op_run.error = error
        op_run.output_payload = output_payload
        op_run.trace = trace
        return op_run

    def mark_operation_cancelled(self, operation_run_id, *, error="cancelled", output_payload=None, trace=None):
        op_run = self.op_runs[operation_run_id]
        op_run.status = "cancelled"
        op_run.error = error
        return op_run

    def mark_waiting_operation_run(self, operation_run_id, *, status, pending_state, trace=None, output_payload=None):
        op_run = self.op_runs[operation_run_id]
        op_run.status = status
        op_run.progress_payload = {"pending_state": pending_state}
        op_run.trace = trace or []
        op_run.output_payload = output_payload
        return op_run

    def update_operation_run_progress(self, operation_run_id, progress_payload):
        self.progress_updates.append(progress_payload)
        return self.op_runs.get(operation_run_id)

    def request_cancel_child_runs(self, parent_run_id):
        self.cancel_child_calls.append(parent_run_id)
        return []

    def request_cancel_for_each_sibling_runs(self, **kwargs):
        self.cancel_sibling_calls.append(kwargs)
        return []

    def is_cancel_requested(self, run_id):
        return False


class FakeWorkflowRepo:
    def __init__(self, workflows):
        self.workflows = {wf.id: wf for wf in workflows}

    def get_by_id(self, workflow_id):
        return self.workflows.get(workflow_id)

    def get_with_operations(self, workflow_id):
        return self.workflows.get(workflow_id)


class FakeAssistantRunner:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"mode": "final", "answer": "done", "trace": []}

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def make_operation(op_id, op_type, config=None, *, depends_on=None, ref_id=None, name=None):
    return SimpleNamespace(
        id=op_id,
        name=name or f"op_{op_id}",
        operation_type=op_type,
        operation_ref_id=ref_id,
        config=config or {},
        depends_on=depends_on or [],
        on_failure_follow_up=None,
        on_success_follow_up=None,
        timeout_seconds=None,
        retry_policy=None,
        position=op_id,
    )


def make_workflow(workflow_id, operations, *, name=None, is_enabled=True):
    return SimpleNamespace(id=workflow_id, name=name or f"workflow_{workflow_id}",
                           is_enabled=is_enabled, operations=operations)


def make_executor(workflows, *, assistant_result=None):
    repo = FakeRunRepo()
    workflow_repo = FakeWorkflowRepo(workflows)
    runner = FakeAssistantRunner(result=assistant_result)
    executor = WorkflowExecutor(
        workflow_repository=workflow_repo,
        run_repository=repo,
        assistant_runner=runner,
    )
    return executor, repo, runner


@pytest.fixture
def artifact_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "FILES_DIR", str(tmp_path))
    return tmp_path


def run_workflow(executor, repo, workflow_id, input_payload=None):
    run = repo.create_run(workflow_id=workflow_id, trigger_type="manual", input_payload=input_payload or {})
    result = asyncio.run(executor.execute_run(run.id))
    return run, result


def artifact_content(artifact_dir, run_id, filename):
    return (artifact_dir / "workflows" / str(run_id) / "artifacts" / filename).read_text(encoding="utf-8")


# --- Deel A1: artifact content / content_from -------------------------------

def test_artifact_content_from_reference_writes_variable(artifact_dir):
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "artifact", {
            "action": "save_text", "name": "test.txt",
            "content_from": "workflow_variables.var", "content_type": "text/plain",
        }, depends_on=[1]),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "success"
    assert artifact_content(artifact_dir, run.id, "test.txt") == "ja"


def test_artifact_content_template_writes_variable(artifact_dir):
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "artifact", {
            "action": "save_text", "name": "test.txt",
            "content": "${workflow_variables.var}", "content_type": "text/plain",
        }, depends_on=[1]),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "success"
    assert artifact_content(artifact_dir, run.id, "test.txt") == "ja"


def test_artifact_content_template_mixed_text(artifact_dir):
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "artifact", {
            "action": "save_text", "name": "test.txt",
            "content": "De waarde is ${workflow_variables.var}", "content_type": "text/plain",
        }, depends_on=[1]),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert artifact_content(artifact_dir, run.id, "test.txt") == "De waarde is ja"


def test_artifact_content_from_with_template_syntax_compat(artifact_dir):
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "artifact", {
            "action": "save_text", "name": "test.txt",
            "content_from": "${workflow_variables.var}", "content_type": "text/plain",
        }, depends_on=[1]),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "success"
    assert artifact_content(artifact_dir, run.id, "test.txt") == "ja"
    artifact_output = result["operation_outputs"][2]
    compat_events = [e for e in artifact_output["trace"]
                     if e["type"] == "workflow_artifact_content_resolved" and e["data"].get("compat_template_content_from")]
    assert compat_events, "expected compat trace marker for ${...} in content_from"
    assert compat_events[0]["level"] == "warn"
    assert "prefer content" in compat_events[0]["data"]["warning"]


def test_artifact_literal_content(artifact_dir):
    operations = [
        make_operation(1, "artifact", {"action": "save_text", "name": "test.txt", "content": "hello"}),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert artifact_content(artifact_dir, run.id, "test.txt") == "hello"


def test_artifact_save_json_from_reference(artifact_dir):
    operations = [
        make_operation(1, "set_variable", {"variables": {"obj": {"a": 1, "b": "x"}}}),
        make_operation(2, "artifact", {
            "action": "save_json", "name": "data.json", "content_from": "workflow_variables.obj",
        }, depends_on=[1]),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert json.loads(artifact_content(artifact_dir, run.id, "data.json")) == {"a": 1, "b": "x"}


def test_artifact_missing_content_and_content_from_fails_clearly(artifact_dir):
    operations = [make_operation(1, "artifact", {"action": "save_text", "name": "test.txt"})]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="requires content or content_from"):
        asyncio.run(executor.execute_run(run.id))


def test_artifact_missing_reference_fails_clearly(artifact_dir):
    operations = [make_operation(1, "artifact", {
        "action": "save_text", "name": "test.txt", "content_from": "workflow_variables.missing",
    })]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="artifact content_from not found: workflow_variables.missing"):
        asyncio.run(executor.execute_run(run.id))


# --- Deel A2: workflow_variables in result payloads -------------------------

def test_workflow_variables_present_after_failure(artifact_dir):
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "artifact", {
            "action": "save_text", "name": "test.txt", "content_from": "workflow_variables.missing",
        }, depends_on=[1]),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError):
        asyncio.run(executor.execute_run(run.id))
    assert run.status == "failed"
    assert run.result_payload["workflow_variables"]["var"] == "ja"


def test_workflow_variables_present_in_waiting_payload():
    waiting_result = {
        "mode": "workflow_waiting",
        "pending_action": {"type": "workflow_user_input", "question": "q", "resume_payload": {}},
        "trace": [],
    }
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "assistant", {"skill_names": ["demo"]}, depends_on=[1], ref_id=10),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)], assistant_result=waiting_result)
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "waiting"
    assert run.status == "waiting"
    assert run.result_payload["workflow_variables"]["var"] == "ja"


def test_workflow_variables_present_on_success():
    operations = [make_operation(1, "set_variable", {"variables": {"var": "ja"}})]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert run.status == "success"
    assert run.result_payload["workflow_variables"]["var"] == "ja"


# --- Deel A3: workflow_variables reconstruction -----------------------------

def test_context_rebuild_reconstructs_variables_from_operation_runs():
    operations = [make_operation(1, "set_variable", {"variables": {"var": "ja"}})]
    workflow = make_workflow(7, operations)
    executor, repo, _ = make_executor([workflow])
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    op_run = repo.create_operation_run(workflow_run_id=run.id, workflow_operation_id=1)
    repo.finish_operation_run(op_run.id, output_payload={
        "mode": "set_variable", "status": "success", "variables_set": {"var": "ja"},
    })
    run.result_payload = None
    context = executor._context_from_operation_runs(run, workflow)
    assert context["workflow_variables"]["var"] == "ja"


def test_context_rebuild_later_set_variable_overwrites_earlier():
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "eerste"}}),
        make_operation(2, "set_variable", {"variables": {"var": "tweede"}}, depends_on=[1]),
    ]
    workflow = make_workflow(7, operations)
    executor, repo, _ = make_executor([workflow])
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    op_run_1 = repo.create_operation_run(workflow_run_id=run.id, workflow_operation_id=1)
    repo.finish_operation_run(op_run_1.id, output_payload={
        "mode": "set_variable", "status": "success", "variables_set": {"var": "eerste"},
    })
    op_run_2 = repo.create_operation_run(workflow_run_id=run.id, workflow_operation_id=2)
    repo.finish_operation_run(op_run_2.id, output_payload={
        "mode": "set_variable", "status": "success", "variables_set": {"var": "tweede"},
    })
    context = executor._context_from_operation_runs(run, workflow)
    assert context["workflow_variables"]["var"] == "tweede"


# --- Deel A4: condition branch ID validation --------------------------------

def _condition_workflow(then_ids, else_ids, *, extra_ops=None, branch_extra=None):
    config = {
        "condition": {"source": "workflow_variables", "path": "var", "operator": "equals", "value": "ja"},
        "then_operation_ids": then_ids,
        "else_operation_ids": else_ids,
    }
    config.update(branch_extra or {})
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "condition", config, depends_on=[1]),
    ] + (extra_ops or [])
    return make_workflow(7, operations)


def test_condition_inline_integer_branch_ids(artifact_dir):
    extra = [
        make_operation(3, "artifact", {"action": "save_text", "name": "then.txt", "content": "then"}, depends_on=[2]),
        make_operation(4, "artifact", {"action": "save_text", "name": "else.txt", "content": "else"}, depends_on=[2]),
    ]
    workflow = _condition_workflow([3], [4], extra_ops=extra)
    executor, repo, _ = make_executor([workflow])
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "success"
    condition_output = result["operation_outputs"][2]
    assert condition_output["result"] is True
    assert condition_output["selected_branch"] == "then"
    assert condition_output["selected_operation_ids"] == [3]
    assert condition_output["skipped_operation_ids"] == [4]
    assert result["operation_statuses"][3] == "success"
    assert result["operation_statuses"][4] == "skipped"


def test_condition_numeric_string_branch_ids_are_cast(artifact_dir):
    extra = [
        make_operation(3, "artifact", {"action": "save_text", "name": "then.txt", "content": "then"}, depends_on=[2]),
    ]
    workflow = _condition_workflow(["3"], [], extra_ops=extra)
    executor, repo, _ = make_executor([workflow])
    run, result = run_workflow(executor, repo, 7)
    assert result["operation_outputs"][2]["selected_operation_ids"] == [3]


def test_condition_tmp_branch_id_gives_clear_error():
    workflow = _condition_workflow(["tmp-1780307590701-4"], [])
    executor, repo, _ = make_executor([workflow])
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="condition branch operation id must be an integer: tmp-1780307590701-4"):
        asyncio.run(executor.execute_run(run.id))


def test_condition_on_workflow_variables_without_output_contract():
    workflow = _condition_workflow([], [])
    executor, repo, _ = make_executor([workflow])
    run, result = run_workflow(executor, repo, 7)
    assert result["operation_outputs"][2]["result"] is True
    assert result["operation_outputs"][2]["matched_value"] == "ja"


# --- Deel B: condition branch_mode=sub_workflow ------------------------------

def _sub_workflow_condition_setup(*, var_value="ja", true_id=12, false_id=13, branch_extra=None,
                                  child_true_ops=None, child_false_ops=None):
    parent_config = {
        "condition": {"source": "workflow_variables", "path": "var", "operator": "equals", "value": "ja"},
        "branch_mode": "sub_workflow",
        "true_workflow_id": true_id,
        "false_workflow_id": false_id,
    }
    parent_config.update(branch_extra or {})
    parent_ops = [
        make_operation(1, "set_variable", {"variables": {"var": var_value}}),
        make_operation(2, "condition", parent_config, depends_on=[1],
                       name="Check var"),
    ]
    workflows = [make_workflow(7, parent_ops, name="Parent")]
    if true_id is not None:
        workflows.append(make_workflow(true_id, child_true_ops or [
            make_operation(50, "set_variable", {"variables": {"branch": "true"}}),
        ], name="Success workflow"))
    if false_id is not None:
        workflows.append(make_workflow(false_id, child_false_ops or [
            make_operation(60, "set_variable", {"variables": {"branch": "false"}}),
        ], name="Failure workflow"))
    return workflows


def test_condition_true_starts_true_workflow_child_run():
    executor, repo, _ = make_executor(_sub_workflow_condition_setup())
    run, result = run_workflow(executor, repo, 7, input_payload={"some": "input"})
    assert result["status"] == "success"
    condition_output = result["operation_outputs"][2]
    assert condition_output["branch_mode"] == "sub_workflow"
    assert condition_output["result"] is True
    assert condition_output["selected_branch"] == "true"
    assert condition_output["selected_workflow_id"] == 12
    assert condition_output["child_status"] == "success"

    child_runs = [r for r in repo.runs.values() if r.parent_run_id == run.id]
    assert len(child_runs) == 1
    child = child_runs[0]
    assert child.workflow_id == 12
    assert child.trigger_type == "condition_true"
    assert condition_output["child_workflow_run_id"] == child.id
    assert child.parent_operation_run_id is not None

    # deterministic child input payload
    assert child.input_payload["condition"]["result"] is True
    assert child.input_payload["condition"]["selected_branch"] == "true"
    assert child.input_payload["condition"]["matched_value"] == "ja"
    assert child.input_payload["condition"]["expected_value"] == "ja"
    assert child.input_payload["condition"]["parent_operation_id"] == 2
    assert child.input_payload["condition"]["parent_workflow_run_id"] == run.id
    assert child.input_payload["workflow_variables"]["var"] == "ja"
    assert child.input_payload["parent_input"] == {"some": "input"}
    assert child.input_payload["mapped_inputs"] == {}


def test_condition_false_starts_false_workflow_child_run():
    executor, repo, _ = make_executor(_sub_workflow_condition_setup(var_value="nee"))
    run, result = run_workflow(executor, repo, 7)
    condition_output = result["operation_outputs"][2]
    assert condition_output["result"] is False
    assert condition_output["selected_branch"] == "false"
    assert condition_output["selected_workflow_id"] == 13
    child = [r for r in repo.runs.values() if r.parent_run_id == run.id][0]
    assert child.workflow_id == 13
    assert child.trigger_type == "condition_false"


def test_condition_sub_workflow_trace_events():
    executor, repo, _ = make_executor(_sub_workflow_condition_setup())
    run, result = run_workflow(executor, repo, 7)
    trace_types = [e["type"] for e in result["operation_outputs"][2]["trace"]]
    assert "workflow_condition_evaluated" in trace_types
    assert "workflow_condition_branch_workflow_selected" in trace_types
    assert "workflow_condition_branch_child_run_created" in trace_types
    assert "workflow_condition_branch_child_run_completed" in trace_types


def test_condition_sub_workflow_child_failure_fails_condition(artifact_dir):
    failing_child_ops = [make_operation(50, "artifact", {"action": "save_text", "name": "x.txt"})]
    executor, repo, _ = make_executor(
        _sub_workflow_condition_setup(child_true_ops=failing_child_ops)
    )
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="condition branch child workflow run failed"):
        asyncio.run(executor.execute_run(run.id))
    child = [r for r in repo.runs.values() if r.parent_run_id == run.id][0]
    assert child.status == "failed"
    assert str(child.id) in run.error


def test_condition_sub_workflow_missing_selected_branch_fails_clearly():
    executor, repo, _ = make_executor(_sub_workflow_condition_setup(true_id=None, false_id=13,
                                                                    branch_extra={"true_workflow_id": None}))
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="condition branch workflow is not configured for branch: true"):
        asyncio.run(executor.execute_run(run.id))


def test_condition_sub_workflow_tmp_workflow_id_fails_clearly():
    executor, repo, _ = make_executor(
        _sub_workflow_condition_setup(branch_extra={"true_workflow_id": "tmp-1780307590701-4"})
    )
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="condition true_workflow_id must be an integer: tmp-1780307590701-4"):
        asyncio.run(executor.execute_run(run.id))


def test_condition_sub_workflow_unknown_workflow_fails_clearly():
    executor, repo, _ = make_executor(_sub_workflow_condition_setup(branch_extra={"true_workflow_id": 999}))
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="condition branch workflow not found: 999"):
        asyncio.run(executor.execute_run(run.id))


def test_condition_sub_workflow_disabled_workflow_fails_clearly():
    workflows = _sub_workflow_condition_setup()
    for wf in workflows:
        if wf.id == 12:
            wf.is_enabled = False
    executor, repo, _ = make_executor(workflows)
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises(RuntimeError, match="condition branch workflow is disabled: 12"):
        asyncio.run(executor.execute_run(run.id))


def test_condition_inline_remains_default():
    workflow = _condition_workflow([], [])
    executor, repo, _ = make_executor([workflow])
    run, result = run_workflow(executor, repo, 7)
    assert result["operation_outputs"][2]["branch_mode"] == "inline"


# --- Deel C: for_each items_source ------------------------------------------

ASSISTANT_WITH_ITEMS = {
    "mode": "final",
    "answer": "found entities",
    "downstream_handoff": {
        "summary": "two entities",
        "iterables": {"items": [{"name": "A"}, {"name": "B"}]},
    },
    "trace": [],
}


def _for_each_workflows(items_source, *, child_input_mapping=None, parent_extra_ops=None,
                        parent_input_op=True):
    child_config = {"variables": {"x": "done"}}
    if child_input_mapping:
        child_config = {"variables": {"x": "done"}, "input_mapping": child_input_mapping}
    child = make_workflow(20, [make_operation(70, "set_variable", child_config)], name="Child")
    parent_ops = list(parent_extra_ops or [])
    depends = [op.id for op in parent_ops]
    if parent_input_op:
        parent_ops.append(make_operation(1, "assistant", {"skill_names": ["demo"]}, ref_id=10, name="Get entities"))
        depends = [1]
    parent_ops.append(make_operation(2, "for_each", {
        "items_source": items_source,
        "max_concurrency": 1,
    }, depends_on=depends, ref_id=20, name="Loop entities"))
    parent = make_workflow(7, parent_ops, name="Parent")
    return [parent, child]


def test_for_each_items_source_operation_output():
    workflows = _for_each_workflows({
        "source": "operation_output", "operation_id": 1,
        "path": "downstream_handoff.iterables.items", "required": True,
    })
    executor, repo, _ = make_executor(workflows, assistant_result=ASSISTANT_WITH_ITEMS)
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "success"
    for_each_output = result["operation_outputs"][2]
    assert for_each_output["items_count"] == 2
    assert for_each_output["success_count"] == 2
    assert for_each_output["failed_count"] == 0
    assert len(for_each_output["results"]) == 2

    child_runs = sorted([r for r in repo.runs.values() if r.parent_run_id == run.id],
                        key=lambda r: r.parent_item_index)
    assert len(child_runs) == 2
    assert child_runs[0].input_payload["for_each_item"] == {"name": "A"}
    assert child_runs[1].input_payload["for_each_item"] == {"name": "B"}
    meta = child_runs[0].input_payload["for_each"]
    assert meta["index"] == 0
    assert meta["total"] == 2
    assert meta["parent_operation_id"] == 2
    assert meta["parent_operation_key"] == "Loop entities"


def test_for_each_items_source_workflow_input():
    workflows = _for_each_workflows({"source": "workflow_input", "path": "items", "required": True},
                                    parent_input_op=False)
    executor, repo, _ = make_executor(workflows)
    run, result = run_workflow(executor, repo, 7, input_payload={"items": [{"name": "A"}]})
    assert result["operation_outputs"][2]["items_count"] == 1


def test_for_each_items_source_workflow_variables():
    set_var = make_operation(5, "set_variable", {"variables": {"entities": [{"name": "A"}, {"name": "B"}]}})
    workflows = _for_each_workflows({"source": "workflow_variables", "path": "entities", "required": True},
                                    parent_extra_ops=[set_var], parent_input_op=False)
    executor, repo, _ = make_executor(workflows)
    run, result = run_workflow(executor, repo, 7)
    assert result["operation_outputs"][2]["items_count"] == 2


def test_for_each_items_source_previous_operation_output():
    workflows = _for_each_workflows({
        "source": "previous_operation_output",
        "path": "downstream_handoff.iterables.items", "required": True,
    })
    executor, repo, _ = make_executor(workflows, assistant_result=ASSISTANT_WITH_ITEMS)
    run, result = run_workflow(executor, repo, 7)
    assert result["operation_outputs"][2]["items_count"] == 2


def test_for_each_items_source_mapped_inputs():
    workflows = _for_each_workflows({"source": "mapped_inputs", "path": "items", "required": True},
                                    parent_input_op=False)
    for wf in workflows:
        if wf.id == 7:
            for op in wf.operations:
                if op.operation_type == "for_each":
                    op.config["input_mapping"] = {
                        "items": {"source": "workflow_input", "path": "raw_items", "required": True},
                    }
    executor, repo, _ = make_executor(workflows)
    run, result = run_workflow(executor, repo, 7, input_payload={"raw_items": [{"name": "A"}]})
    assert result["operation_outputs"][2]["items_count"] == 1


def test_for_each_items_source_static():
    workflows = _for_each_workflows({"source": "static", "value": [{"name": "A"}, {"name": "B"}]},
                                    parent_input_op=False)
    executor, repo, _ = make_executor(workflows)
    run, result = run_workflow(executor, repo, 7)
    assert result["operation_outputs"][2]["items_count"] == 2


def test_for_each_scalar_items_fail():
    workflows = _for_each_workflows({"source": "static", "value": ["a", "b"]}, parent_input_op=False)
    executor, repo, _ = make_executor(workflows)
    run = repo.create_run(workflow_id=7, trigger_type="manual", input_payload={})
    with pytest.raises((ValueError, RuntimeError), match="must be a JSON object"):
        asyncio.run(executor.execute_run(run.id))


def test_for_each_child_input_mapping_from_current_item():
    workflows = _for_each_workflows(
        {"source": "static", "value": [{"name": "dim_customer", "path": "silver/dim_customer.py"}]},
        child_input_mapping={
            "entity_name": {"source": "for_each_item", "path": "name", "required": True},
            "entity_path": {"source": "for_each_item", "path": "path", "required": True},
        },
        parent_input_op=False,
    )
    executor, repo, _ = make_executor(workflows)
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "success"
    child_run = [r for r in repo.runs.values() if r.parent_run_id == run.id][0]
    child_op_runs = child_run.operation_runs
    assert child_op_runs, "expected child operation runs"
    mapped = child_op_runs[0].input_payload["mapped_inputs"]
    assert mapped["entity_name"] == "dim_customer"
    assert mapped["entity_path"] == "silver/dim_customer.py"


def test_for_each_legacy_iterable_source_fallback():
    child = make_workflow(20, [make_operation(70, "set_variable", {"variables": {"x": "done"}})])
    parent_ops = [
        make_operation(1, "assistant", {"skill_names": ["demo"]}, ref_id=10, name="Get entities"),
        make_operation(2, "for_each", {
            "iterable_source": {"operation_id": 1, "name": "items"},
            "max_concurrency": 1,
        }, depends_on=[1], ref_id=20),
    ]
    executor, repo, _ = make_executor([make_workflow(7, parent_ops), child],
                                      assistant_result=ASSISTANT_WITH_ITEMS)
    run, result = run_workflow(executor, repo, 7)
    assert result["operation_outputs"][2]["items_count"] == 2


# --- Deel G: end-to-end testflow 1 ------------------------------------------

def test_testflow_1_set_variable_condition_artifact(artifact_dir):
    operations = [
        make_operation(1, "set_variable", {"variables": {"var": "ja"}}),
        make_operation(2, "condition", {
            "condition": {"source": "workflow_variables", "path": "var", "operator": "equals", "value": "ja"},
            "then_operation_ids": [3],
            "else_operation_ids": [],
        }, depends_on=[1]),
        make_operation(3, "artifact", {
            "action": "save_text", "name": "test.txt",
            "content_from": "workflow_variables.var", "content_type": "text/plain",
        }, depends_on=[2]),
    ]
    executor, repo, _ = make_executor([make_workflow(7, operations)])
    run, result = run_workflow(executor, repo, 7)
    assert result["status"] == "success"
    assert result["operation_statuses"] == {1: "success", 2: "success", 3: "success"}
    assert result["operation_outputs"][2]["result"] is True
    assert result["operation_outputs"][2]["selected_branch"] == "then"
    assert artifact_content(artifact_dir, run.id, "test.txt") == "ja"
    assert result["workflow_variables"]["var"] == "ja"


# ── Theme 4: per-activity skip/fail when a model is unavailable ──────────────
def test_assistant_op_skipped_when_model_unavailable(monkeypatch):
    op = make_operation(1, "assistant", config={"on_model_unavailable": "skip"}, ref_id=10)
    wf = make_workflow(1, [op])
    executor, repo, runner = make_executor([wf])
    monkeypatch.setattr(executor, "_operation_model_available", lambda operation: False)
    run, result = run_workflow(executor, repo, 1)
    assert repo.runs[run.id].status == "success"
    assert result["operation_statuses"][1] == "skipped"


def test_assistant_op_fails_run_when_model_unavailable_and_fail(monkeypatch):
    op = make_operation(1, "assistant", config={"on_model_unavailable": "fail"}, ref_id=10)
    wf = make_workflow(1, [op])
    executor, repo, runner = make_executor([wf])
    monkeypatch.setattr(executor, "_operation_model_available", lambda operation: False)
    run = repo.create_run(workflow_id=1, trigger_type="manual", input_payload={})
    with pytest.raises(Exception):
        asyncio.run(executor.execute_run(run.id))
    assert repo.runs[run.id].status == "failed"


# ── Theme 5b: in-flight operation cancellation (mid-op, not only between ops) ─
def test_in_flight_operation_cancelled_mid_flight(monkeypatch):
    op = make_operation(1, "notification", config={})
    wf = make_workflow(1, [op])
    executor, repo, runner = make_executor([wf])

    started = {"v": False}

    async def slow(operation, input_payload, context):
        started["v"] = True
        await asyncio.sleep(8)  # long op; should be interrupted well before this
        return {"status": "success"}

    executor._execute_notification_operation = slow

    cancelled = {"v": False}
    monkeypatch.setattr(repo, "is_cancel_requested", lambda run_id: cancelled["v"])

    run = repo.create_run(workflow_id=1, trigger_type="manual", input_payload={})

    async def request_cancel_when_running():
        while not started["v"]:
            await asyncio.sleep(0.02)
        cancelled["v"] = True  # request cancel while the op is in-flight

    async def go():
        return await asyncio.gather(
            executor.execute_run(run.id),
            request_cancel_when_running(),
            return_exceptions=True,
        )

    import time
    t0 = time.monotonic()
    asyncio.run(go())
    elapsed = time.monotonic() - t0

    assert repo.runs[run.id].status == "cancelled"
    assert elapsed < 6  # interrupted mid-flight by the watcher, not after the 8s sleep
