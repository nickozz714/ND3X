"""Ask-run cancellation: an in-flight run can be interrupted and marked cancelled."""
from __future__ import annotations

import asyncio

from services.assistants.ask_job_service import AskJobService


def _make_service(tmp_path) -> AskJobService:
    return AskJobService(ask_root=tmp_path / "ask", voice_root=tmp_path / "voice")


def test_cancel_unknown_run_reports_not_found(tmp_path):
    svc = _make_service(tmp_path)
    out = svc.cancel_job(thread_id="nope", run_id="nope")
    assert out["cancelled"] is False
    assert out["reason"] == "not_found"


def test_cancel_in_flight_run_interrupts_and_marks_cancelled(tmp_path):
    svc = _make_service(tmp_path)

    async def scenario():
        started = asyncio.Event()

        async def run_ask_cb(**_kwargs):
            started.set()
            await asyncio.sleep(30)  # long-running; should be cancelled before this returns
            return {"answer": "should not happen"}

        async def store_user_message_cb(**_kwargs):
            return None

        async def store_assistant_message_cb(**_kwargs):
            return None

        def timeout_response_cb(_thread_id):
            return {"mode": "timeout"}

        def error_response_cb(_thread_id, _exc):
            return {"mode": "error"}

        created = await svc.create_job(
            question="hello",
            payload={},
            thread_id="t1",
            model="m",
            run_ask_cb=run_ask_cb,
            store_user_message_cb=store_user_message_cb,
            store_assistant_message_cb=store_assistant_message_cb,
            timeout_response_cb=timeout_response_cb,
            error_response_cb=error_response_cb,
        )
        run_id = created["run_id"]

        await asyncio.wait_for(started.wait(), timeout=5)
        out = svc.cancel_job(thread_id="t1", run_id=run_id)
        assert out["cancelled"] is True

        # Let the cancelled task unwind so the CancelledError handler runs.
        await asyncio.sleep(0.1)

        status = svc.get_status(thread_id="t1", run_id=run_id)
        assert status["state"] == "cancelled"
        # The in-flight task should no longer be tracked.
        assert svc._task_key("t1", run_id) not in svc._running_tasks

    asyncio.run(scenario())


def test_cross_worker_cancel_via_disk_flag(tmp_path):
    """Simulate a cancel written by another worker (no in-process task to cancel):
    the run's watcher should observe the disk flag and stop the run."""
    svc = _make_service(tmp_path)

    async def scenario():
        started = asyncio.Event()

        async def run_ask_cb(**_kwargs):
            started.set()
            await asyncio.sleep(30)
            return {"answer": "should not happen"}

        async def noop(**_kwargs):
            return None

        created = await svc.create_job(
            question="hello",
            payload={},
            thread_id="t2",
            model="m",
            run_ask_cb=run_ask_cb,
            store_user_message_cb=noop,
            store_assistant_message_cb=noop,
            timeout_response_cb=lambda _t: {"mode": "timeout"},
            error_response_cb=lambda _t, _e: {"mode": "error"},
        )
        run_id = created["run_id"]
        await asyncio.wait_for(started.wait(), timeout=5)

        # Another worker marks it cancelled on disk WITHOUT touching this process's task.
        svc.write_status(thread_id="t2", run_id=run_id, state="cancelled",
                         extra={"phase": "cancelled", "message": "by other worker"})

        # Watcher polls ~1s; give it time to observe + cancel + unwind.
        await asyncio.sleep(2.5)
        assert svc.get_status(thread_id="t2", run_id=run_id)["state"] == "cancelled"
        assert svc._task_key("t2", run_id) not in svc._running_tasks

    asyncio.run(scenario())
