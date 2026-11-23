import ast
from pathlib import Path


def _project_root():
    here = Path(__file__).resolve()
    for candidate in (here.parent, here.parent.parent):
        if (candidate / "ggpbot.py").is_file():
            return candidate
    return here.parent


def _function(source, name):
    tree = ast.parse(source)
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    return matches[-1]


def _load_gate(state, trade_state, memory, recovery_block):
    source = (_project_root() / "ggpbot.py").read_text(encoding="utf-8")
    node = _function(source, "_discovery_paused")
    isolated = ast.Module(body=[node], type_ignores=[])
    namespace = {
        "STATE": state,
        "TRADE_STATE": trade_state,
        "MEMORY": memory,
        "RECOVERY_BLOCK_ENTRIES": recovery_block,
    }
    exec(compile(isolated, "ggpbot.py", "exec"), namespace)
    return namespace["_discovery_paused"]


def test_exchange_position_unknown_blocks_entries_but_not_market_discovery():
    gate = _load_gate(
        {"open": False}, {"in_position": False},
        {"position_status": "UNKNOWN"}, True,
    )
    assert gate() is False


def test_recovery_block_does_not_stop_scanner_observation():
    gate = _load_gate(
        {"open": False}, {"in_position": False},
        {"position_status": "FLAT"}, True,
    )
    assert gate() is False


def test_known_open_exposure_still_pauses_discovery():
    gate = _load_gate(
        {"open": True}, {"in_position": True},
        {"position_status": "OPEN"}, False,
    )
    assert gate() is True


def test_multiple_exchange_positions_still_pause_discovery():
    gate = _load_gate(
        {"open": False}, {"in_position": False},
        {"position_status": "MULTIPLE_OPEN"}, True,
    )
    assert gate() is True


def test_entry_authorization_keeps_explicit_recovery_fail_closed_guard():
    source = (_project_root() / "ggpbot.py").read_text(encoding="utf-8")
    node = _function(source, "smart_opportunity_selection")
    constants = {item.value for item in ast.walk(node) if isinstance(item, ast.Constant)}
    assert "RECOVERY_BLOCK_ENTRIES" in constants
    assert "position_status" in constants
