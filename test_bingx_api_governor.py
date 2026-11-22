import threading
import time

import pytest

from bingx_api_governor import APIGovernorError, BingXAPIGovernor, GovernedExchange


class FakeExchange:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def fetch_ticker(self, symbol):
        with self.lock:
            self.calls.append(("fetch_ticker", symbol))
        return {"last": 123.0}

    def fetch_positions(self, symbols=None):
        with self.lock:
            self.calls.append(("fetch_positions", symbols))
        return []

    def fetch_balance(self):
        with self.lock:
            self.calls.append(("fetch_balance", None))
        return {"total": {"USDT": 100.0}, "free": {"USDT": 80.0}}

    def create_order(self, *args, **kwargs):
        with self.lock:
            self.calls.append(("create_order", args))
        return {"id": "order-1"}

    def load_markets(self):
        with self.lock:
            self.calls.append(("load_markets", None))
        return {"BTC/USDT:USDT": {"id": "BTC-USDT"}}

    def market(self, symbol):
        return {"id": symbol}

    def amount_to_precision(self, symbol, amount):
        return str(amount)

    def swapV2PrivateGetTradeOpenOrders(self, params):
        with self.lock:
            self.calls.append(("swapV2PrivateGetTradeOpenOrders", params))
        return {"data": {"orders": []}}


def test_proxy_routes_ccxt_and_raw_bingx_calls_through_one_governor():
    raw = FakeExchange()
    governor = BingXAPIGovernor(min_intervals={"market_data": 0, "account_positions": 0,
                                                "account_balance": 0, "order_write": 0,
                                                "account_orders": 0, "account_config": 0})
    exchange = GovernedExchange(raw, governor)

    assert exchange.fetch_ticker("BTC/USDT")["last"] == 123.0
    assert exchange.fetch_positions(["BTC/USDT"]) == []
    assert exchange.fetch_balance()["free"]["USDT"] == 80.0
    assert exchange.load_markets()
    assert exchange.swapV2PrivateGetTradeOpenOrders({"symbol": "BTC-USDT"})
    # Local precision/market metadata must not consume a remote request slot.
    assert exchange.amount_to_precision("BTC/USDT", 1.0) == "1.0"
    assert exchange.market("BTC/USDT")["id"] == "BTC/USDT"

    snap = governor.snapshot()
    assert snap["totals"]["attempts"] == 5
    assert snap["categories"]["market_data"]["attempts"] == 1
    assert snap["categories"]["account_positions"]["attempts"] == 1
    assert snap["categories"]["account_balance"]["attempts"] == 1
    assert snap["categories"]["account_orders"]["attempts"] == 1


def test_order_write_is_never_retried_by_governor():
    raw = FakeExchange()
    calls = []
    def fail_once(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("socket timed out after request dispatch")
    raw.create_order = fail_once
    exchange = GovernedExchange(raw, BingXAPIGovernor(min_intervals={"order_write": 0}))

    with pytest.raises(TimeoutError):
        exchange.create_order("BTC/USDT", "market", "buy", 1)
    assert len(calls) == 1


def test_endpoint_429_cooldown_blocks_followup_without_sending_request():
    raw = FakeExchange()
    calls = []
    def limited(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return []
    raw.fetch_positions = limited
    governor = BingXAPIGovernor(
        min_intervals={"account_positions": 0}, cooldown_429=0.15, cooldown_418=0.2
    )
    exchange = GovernedExchange(raw, governor)

    with pytest.raises(RuntimeError, match="429"):
        exchange.fetch_positions(["BTC/USDT"])
    with pytest.raises(APIGovernorError) as caught:
        exchange.fetch_positions(["ETH/USDT"])
    assert caught.value.code == 429
    assert len(calls) == 1
    assert governor.snapshot()["totals"]["rate_limited"] == 1

    time.sleep(0.17)
    assert exchange.fetch_positions(["ETH/USDT"]) == []
    assert len(calls) == 2


def test_418_cooldown_is_global_across_endpoint_categories():
    raw = FakeExchange()
    calls = []
    def banned(symbol):
        calls.append(symbol)
        raise RuntimeError("HTTP 418 IP banned")
    raw.fetch_positions = banned
    governor = BingXAPIGovernor(
        min_intervals={"account_positions": 0, "market_data": 0},
        cooldown_429=0.05, cooldown_418=0.15
    )
    exchange = GovernedExchange(raw, governor)

    with pytest.raises(RuntimeError, match="418"):
        exchange.fetch_positions(["BTC/USDT"])
    with pytest.raises(APIGovernorError) as caught:
        exchange.fetch_ticker("BTC/USDT")
    assert caught.value.code == 418
    assert len(calls) == 1
    assert governor.snapshot()["global_cooldown_seconds"] > 0


def test_network_interruption_does_not_create_rate_limit_cooldown():
    raw = FakeExchange()
    calls = []
    def intermittent(symbol):
        calls.append(symbol)
        if len(calls) == 1:
            raise ConnectionError("network disconnected")
        return {"last": 125.0}
    raw.fetch_ticker = intermittent
    governor = BingXAPIGovernor(min_intervals={"market_data": 0})
    exchange = GovernedExchange(raw, governor)

    with pytest.raises(ConnectionError):
        exchange.fetch_ticker("BTC/USDT")
    assert governor.snapshot()["cooldowns"] == {}
    assert exchange.fetch_ticker("BTC/USDT")["last"] == 125.0
    assert len(calls) == 2


def test_global_concurrency_cap_holds_under_eight_parallel_workers():
    raw = FakeExchange()
    governor = BingXAPIGovernor(
        min_intervals={"market_data": 0}, max_concurrent=2
    )
    active = 0
    maximum = 0
    lock = threading.Lock()

    def slow_ticker(symbol):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.025)
        with lock:
            active -= 1
        return {"last": 1.0}

    raw.fetch_ticker = slow_ticker
    exchange = GovernedExchange(raw, governor)
    threads = [threading.Thread(target=exchange.fetch_ticker, args=(f"S{i}/USDT",))
               for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert maximum <= 2
    assert governor.snapshot()["totals"]["success"] == 8


def test_rate_limit_cooldown_survives_process_restart(tmp_path):
    state_path = str(tmp_path / "data" / "bingx_api_governor_state.json")
    raw = FakeExchange()
    calls = []
    def limited(symbol):
        calls.append(symbol)
        raise RuntimeError("HTTP 429 Too Many Requests")
    raw.fetch_positions = limited

    first_process_governor = BingXAPIGovernor(
        min_intervals={"account_positions": 0}, cooldown_429=5,
        state_path=state_path
    )
    with pytest.raises(RuntimeError, match="429"):
        GovernedExchange(raw, first_process_governor).fetch_positions(["BTC/USDT"])

    restarted_governor = BingXAPIGovernor(
        min_intervals={"account_positions": 0}, cooldown_429=5,
        state_path=state_path
    )
    with pytest.raises(APIGovernorError) as caught:
        GovernedExchange(raw, restarted_governor).fetch_positions(["ETH/USDT"])
    assert caught.value.code == 429
    assert len(calls) == 1


def test_priority_policy_keeps_execution_and_protection_ahead_of_scans():
    assert BingXAPIGovernor.classify("create_order") == ("order_write", 0)
    assert BingXAPIGovernor.classify("fetch_open_orders") == ("account_orders", 0)
    assert BingXAPIGovernor.classify("fetch_positions") == ("account_positions", 0)
    assert BingXAPIGovernor.classify("fetch_balance") == ("account_balance", 1)
    assert BingXAPIGovernor.classify("fetch_ohlcv") == ("market_kline", 2)
