import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE = (ROOT / "ggpbot.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _functions(name):
    return [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name]


def _first_function(name):
    found = _functions(name)
    assert found, f"missing function: {name}"
    return found[0]


def _last_function(name):
    found = _functions(name)
    assert found, f"missing function: {name}"
    return found[-1]


def _call_names(node):
    return [call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)]


def test_main_supervisor_dispatches_candidate_work_instead_of_running_it_inline():
    # The first definition is the actual long-running supervisor; the later
    # definition is the startup-recovery wrapper.
    node = _first_function("main_loop_sniper")
    calls = _call_names(node)
    assert "dispatch_scanner_task" in calls
    assert "smart_opportunity_selection" not in calls
    assert "_run_sniper_scan_if_due" not in calls
    assert "update_institutional_flow_scanner" not in calls


def test_candidate_worker_serializes_watchlist_entry_then_sniper():
    node = _last_function("run_candidate_pipeline")
    calls = _call_names(node)
    assert calls.index("smart_opportunity_selection") < calls.index("_run_sniper_scan_if_due")
    assert "_discovery_paused" in calls
    assert "candidate_pipeline" in SOURCE


def test_institutional_flow_has_one_configurable_slow_polling_worker():
    node = _last_function("live_institutional_updater")
    constants = {item.value for item in ast.walk(node) if isinstance(item, ast.Constant)}
    names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
    assert "INSTITUTIONAL_FLOW_REFRESH_SECONDS" in names
    assert 5 in constants  # short sleep only while a position is open
    assert 'INSTITUTIONAL_FLOW_REFRESH_SECONDS = _env_int_range("INSTITUTIONAL_FLOW_REFRESH_SECONDS", 60, 30, 300)' in SOURCE

def test_runtime_diagnostics_exposes_candidate_worker_stage():
    node = _last_function("runtime_diagnostics")
    segment = ast.get_source_segment(SOURCE, node)
    assert '"candidate_pipeline": dict(MEMORY.get("candidate_pipeline") or {})' in segment
