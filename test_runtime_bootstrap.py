from pathlib import Path
import threading
import time

from ggp_runtime_bootstrap import RuntimeBootstrap


def test_runtime_bootstrap_starts_one_supervisor_and_is_idempotent(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    state = {}
    bootstrap = RuntimeBootstrap(lock_factory=lambda path: object())

    def target():
        entered.set()
        release.wait(2)

    lock_path = str(tmp_path / "runtime.lock")
    assert bootstrap.start(target, state, lock_path) is True
    assert entered.wait(1)
    assert bootstrap.start(target, state, lock_path) is True
    assert state["status"] == "RUNNING"
    assert state["thread_alive"] is True
    release.set()
    deadline = time.time() + 1
    while bootstrap.is_alive() and time.time() < deadline:
        time.sleep(0.01)
    assert state["status"] == "STOPPED_UNEXPECTEDLY"
    assert state["thread_alive"] is False


def test_runtime_bootstrap_fails_closed_when_lock_is_owned(tmp_path):
    state = {}
    started = []
    bootstrap = RuntimeBootstrap(lock_factory=lambda path: None)

    assert bootstrap.start(lambda: started.append(True), state, str(tmp_path / "runtime.lock")) is False
    assert state["status"] == "LOCKED_BY_OTHER_PROCESS"
    assert state["thread_alive"] is False
    assert started == []


def test_runtime_bootstrap_records_lock_acquisition_error(tmp_path):
    state = {}
    bootstrap = RuntimeBootstrap(
        lock_factory=lambda path: (_ for _ in ()).throw(PermissionError("lock path unavailable"))
    )

    assert bootstrap.start(lambda: None, state, str(tmp_path / "runtime.lock")) is False
    assert state["status"] == "FAILED"
    assert state["thread_alive"] is False
    assert "RUNTIME_LOCK_ACQUISITION_FAILED:PermissionError" in state["last_error"]


def test_runtime_bootstrap_records_supervisor_exception(tmp_path):
    state = {}
    bootstrap = RuntimeBootstrap(lock_factory=lambda path: object())

    def fail():
        raise RuntimeError("startup failed")

    assert bootstrap.start(fail, state, str(tmp_path / "runtime.lock")) is True
    deadline = time.time() + 1
    while state.get("status") != "FAILED" and time.time() < deadline:
        time.sleep(0.01)
    assert state["status"] == "FAILED"
    assert "RuntimeError:startup failed" == state["last_error"]


def test_wsgi_entrypoint_starts_supervisor_and_procfile_uses_single_worker():
    root = Path(__file__).resolve().parent
    wsgi = (root / "wsgi.py").read_text(encoding="utf-8")
    procfile = (root / "Procfile").read_text(encoding="utf-8")
    assert "runtime_bootstrap.start(" in wsgi
    assert "ggpbot.safe_main_loop" in wsgi
    assert "gunicorn wsgi:app" in procfile
    assert "--workers 1" in procfile


def test_runtime_loop_publishes_heartbeat_and_readiness_endpoint():
    root = Path(__file__).resolve().parent
    source = (root / "ggpbot.py").read_text(encoding="utf-8")
    assert '"heartbeat_ts": now' in source
    assert '@app.route("/runtime-health")' in source
    assert 'runtime_status = str(runtime_state.get("status") or "NOT_STARTED")' in source
