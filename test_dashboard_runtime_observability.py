import ast
from pathlib import Path

from ggp_dashboard_snapshot import DashboardAccountSnapshot


def _root():
    here = Path(__file__).resolve()
    for candidate in (here.parent, here.parent.parent):
        if (candidate / "ggpbot.py").is_file():
            return candidate
    return here.parent


def _source():
    return (_root() / "ggpbot.py").read_text(encoding="utf-8")


def _function(source, name):
    tree = ast.parse(source)
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert matches, f"missing function: {name}"
    return matches[-1]


def test_dashboard_snapshot_is_thread_safe_read_model_and_marks_stale_values():
    snapshot = DashboardAccountSnapshot()
    assert snapshot.snapshot(now=100.0)["status"] == "NOT_STARTED"
    snapshot.update(125.0, 100.0, now=100.0)
    current = snapshot.snapshot(now=120.0, stale_after=45.0)
    assert current["status"] == "VALID" and current["fresh"] is True
    failed = snapshot.mark_error("temporary API failure", now=130.0)
    assert failed["balance"] == 125.0 and failed["free_balance"] == 100.0
    stale = snapshot.snapshot(now=160.0, stale_after=45.0)
    assert stale["status"] == "STALE"
    assert stale["last_error"] == "temporary API failure"


def test_dashboard_data_route_does_not_call_exchange_balance_functions():
    source = _source()
    node = _function(source, "data")
    calls = {
        item.func.id for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    }
    assert "get_dashboard_account_snapshot" in calls
    assert "get_balance_safe" not in calls
    assert "get_free_balance_safe" not in calls


def test_dashboard_scanned_count_uses_actual_scanner_count():
    source = _source()
    node = _function(source, "data")
    segment = ast.get_source_segment(source, node)
    assert 'MEMORY.get("scanned_count"' in segment


def test_live_idle_reconciliation_and_realized_pnl_have_separate_cadences():
    source = _source()
    node = _function(source, "sync_all_states")
    names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
    assert "IDLE_POSITION_SYNC_INTERVAL_SECONDS" in names
    assert "REALIZED_PNL_SYNC_INTERVAL_SECONDS" in names
    assert "_LAST_IDLE_POSITION_SYNC_MONOTONIC" in names
    assert "_LAST_REALIZED_PNL_SYNC_MONOTONIC" in names


def test_runtime_diagnostics_endpoint_exposes_worker_and_pipeline_states():
    source = _source()
    assert '@app.route("/runtime-diagnostics")' in source
    node = _function(source, "runtime_diagnostics")
    names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}
    assert "MEMORY" in names and "DASHBOARD_ACCOUNT_SNAPSHOT" in names
    assert "safe_api_call" not in names


def test_global_discovery_publishes_progress_stage_transitions():
    source = _source()
    node = _function(source, "run_global_discovery_scanner")
    constants = {item.value for item in ast.walk(node) if isinstance(item, ast.Constant)}
    assert {"LOAD_UNIVERSE", "SCANNER_V2", "PROFESSIONAL_SCAN", "RF_SCAN", "RADAR", "COMPLETE"}.issubset(constants)



def test_partial_account_refresh_preserves_last_known_other_balance_field():
    snapshot = DashboardAccountSnapshot()
    snapshot.update(125.0, 100.0, now=100.0)
    partial = snapshot.update(None, 90.0, now=110.0)
    assert partial["balance"] == 125.0
    assert partial["free_balance"] == 90.0
    assert partial["status"] == "VALID"


def test_dashboard_frontend_tolerates_missing_account_numbers_without_aborting_refresh():
    source = _source()
    assert "function dashboardNumber(value" in source
    assert 'dashboardNumber(d.balance)' in source
    assert 'dashboardNumber(d.free_balance)' in source
    assert 'dashboardNumber(d.avail_margin)' in source
    assert 'd.balance.toFixed(2)' not in source
    assert 'd.free_balance.toFixed(2)' not in source
    assert "fetch('/data', {{cache: 'no-store'}})" in source
