"""Local-model queue indicator (observability, no execution gating): a run is
'queued' when an earlier-started active run uses the same LOCAL model."""
from __future__ import annotations

import services.assistants.ask_job_service as svc


def _clear():
    svc._ACTIVE_RUNS.clear()


def test_no_queue_for_single_run():
    _clear()
    svc._register_active_run("r1", "qwen2.5:14b", is_local=True)
    q = svc.queue_info("r1")
    assert q["queued"] is False and q["ahead"] == 0


def test_second_local_run_same_model_is_queued():
    _clear()
    svc._register_active_run("r1", "qwen2.5:14b", is_local=True)
    import time; time.sleep(0.01)
    svc._register_active_run("r2", "qwen2.5:14b", is_local=True)
    assert svc.queue_info("r1")["queued"] is False       # the one in front
    q = svc.queue_info("r2")
    assert q["queued"] is True and q["ahead"] == 1


def test_different_models_do_not_queue():
    _clear()
    svc._register_active_run("r1", "qwen2.5:14b", is_local=True)
    svc._register_active_run("r2", "qwen2.5:7b", is_local=True)
    assert svc.queue_info("r2")["queued"] is False


def test_cloud_models_never_queue():
    _clear()
    svc._register_active_run("r1", "gpt-5.5", is_local=False)
    svc._register_active_run("r2", "gpt-5.5", is_local=False)
    assert svc.queue_info("r2")["queued"] is False


def test_unregister_clears_queue():
    _clear()
    svc._register_active_run("r1", "qwen2.5:14b", is_local=True)
    import time; time.sleep(0.01)
    svc._register_active_run("r2", "qwen2.5:14b", is_local=True)
    assert svc.queue_info("r2")["ahead"] == 1
    svc._unregister_active_run("r1")  # the run ahead finishes
    assert svc.queue_info("r2")["queued"] is False
    _clear()


def test_three_deep_queue_position():
    _clear()
    import time
    for i in range(3):
        svc._register_active_run(f"r{i}", "qwen2.5:14b", is_local=True)
        time.sleep(0.01)
    assert svc.queue_info("r0")["ahead"] == 0
    assert svc.queue_info("r1")["ahead"] == 1
    assert svc.queue_info("r2")["ahead"] == 2
    _clear()


def test_parallelism_raises_queue_threshold():
    _clear()
    import time
    # model allows 2 concurrent → first two never queue, third waits (1 ahead).
    for i in range(3):
        svc._register_active_run(f"r{i}", "qwen2.5:14b", is_local=True, parallel=2)
        time.sleep(0.01)
    assert svc.queue_info("r0")["queued"] is False
    assert svc.queue_info("r1")["queued"] is False   # within the 2 parallel slots
    q = svc.queue_info("r2")
    assert q["queued"] is True and q["ahead"] == 1
    _clear()
