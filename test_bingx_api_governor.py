import threading
import time

import pytest

from bingx_api_governor import APIGovernorError, BingXAPIGovernor, GovernedExchange


class FakeExchange:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def request(self, path, api="public", method="GET", params=None,
                headers=None, body=None, config=None):
        with self.lock:
            self.calls.append((path, api, method, params))
        return {"ok": True}

    def fetch_ticker(self, symbol):
        self.request("quote/ticker", "public", "GET", {"symbol": symbol})
        return {"last": 123.0}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=150):
        self.request("quote/klines", "public", "GET",
                     {"symbol": symbol, "interval": timeframe, "limit": limit})
        return []

    def fetch_positions(self, symbols=None):
        self.request("user/positions", "private", "GET", {"symbols": symbols})
        return []

    def fetch_balance(self):
        self.request("user/balance", "private", "GET", {})
        return {"total": {"USDT": 100.0}, "free": {"USDT": 80.0}}

    def create_order(self, *args, **kwargs):
        self.request("trade/order", "private", "POST", {"args": args, "kwargs": kwargs})
        return {"id": "order-1"}

    def load_markets(self):
        # One CCXT call can issue multiple HTTP requests; each consumes budget.
        self.request("exchangeInfo", "public", "GET", {})
        self.request("swap/contracts", "public", "GET", {})
        return {"BTC/USDT:USDT": {"id": "BTC-USDT"}}

    def market(self, symbol):
        return {"id": symbol}

    def amount_to_precision(self, symbol, amount):
        return str(amount)

    def swapV2PrivateGetTradeOpenOrders(self, params):
        self.request("trade/openOrders", "private", "GET", params)
        return {"data": {"orders": []}}


def make_governor(**kwargs):
    intervals = {
        "market_kline": 0, "market_data": 0, "account_balance": 0,
        "account_positions": 0, "account_orders": 0, "account_trades": 0,
        "account_config": 0, "order_write": 0, "leverage_write": 0,
        "generic": 0,
    }
    intervals.update(kwargs.pop("min_intervals", {}))
    return BingXAPIGovernor(min_intervals=intervals, **kwargs)


def test_proxy_governs_each_actual_ccxt_http_request_including_raw_endpoints():
    raw = FakeExchange()
    governor = make_governor()
    exchange = GovernedExchange(raw, governor)

    assert exchange.fetch_ticker("BTC/USDT")["last"] == 123.0
    assert exchange.fetch_positions(["BTC/USDT"]) == []
    assert exchange.fetch_balance()["free"]["USDT"] == 80.0
    assert exchange.load_markets()
    assert exchange.swapV2PrivateGetTradeOpenOrders({"symbol": "BTC-USDT"})
    # Local precision/market metadata does not make an HTTP request.
    assert exchange.amount_to_precision("BTC/USDT", 1.0) == "1.0"
    assert exchange.market("BTC/USDT")["id"] == "BTC/USDT"

    snap = governor.snapshot()
    assert snap["totals"]["attempts"] == 6
    assert snap["categories"]["market_data"]["attempts"] == 3
    assert snap["categories"]["account_positions"]["attempts"] == 1
    assert snap["categories"]["account_balance"]["attempts"] == 1
    assert snap["categories"]["account_orders"]["attempts"] == 1


def test_order_write_is_never_retried_by_governor():
    raw = FakeExchange()
    calls = []
    def fail_once(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("socket timed out after request dispatch")
    raw.request = fail_once
    exchange = GovernedExchange(raw, make_governor())

    with pytest.raises(TimeoutError):
        exchange.create_order("BTC/USDT", "market", "buy", 1)
    assert len(calls) == 1


def test_endpoint_429_cooldown_blocks_followup_without_sending_request():
    raw = FakeExchange()
    calls = []
    def limited_then_ok(path, api="public", method="GET", params=None,
                        headers=None, body=None, config=None):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return []
    raw.request = limited_then_ok
    governor = make_governor(cooldown_429=0.15, cooldown_418=0.2)
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
    def banned(path, api="public", method="GET", params=None,
               headers=None, body=None, config=None):
        calls.append(path)
        raise RuntimeError("HTTP 418 IP banned")
    raw.request = banned
    governor = make_governor(cooldown_429=0.05, cooldown_418=0.15)
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
    def intermittent(path, api="public", method="GET", params=None,
                      headers=None, body=None, config=None):
        calls.append(path)
        if len(calls) == 1:
            raise ConnectionError("network disconnected")
        return {"ok": True}
    raw.request = intermittent
    governor = make_governor()
    exchange = GovernedExchange(raw, governor)

    with pytest.raises(ConnectionError):
        exchange.fetch_ticker("BTC/USDT")
    assert governor.snapshot()["cooldowns"] == {}
    assert exchange.fetch_ticker("BTC/USDT")["last"] == 123.0
    assert len(calls) == 2


def test_global_concurrency_cap_holds_under_eight_parallel_workers():
    raw = FakeExchange()
    active = 0
    maximum = 0
    lock = threading.Lock()

    def slow_request(path, api="public", method="GET", params=None,
                     headers=None, body=None, config=None):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.025)
        with lock:
            active -= 1
        return {"ok": True}

    raw.request = slow_request
    governor = make_governor(max_concurrent=2)
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
    def limited(path, api="public", method="GET", params=None,
                headers=None, body=None, config=None):
        calls.append(path)
        raise RuntimeError("HTTP 429 Too Many Requests")
    raw.request = limited

    first_process_governor = make_governor(
        cooldown_429=5, state_path=state_path
    )
    with pytest.raises(RuntimeError, match="429"):
        GovernedExchange(raw, first_process_governor).fetch_positions(["BTC/USDT"])

    # A real process restart creates a fresh CCXT client and a fresh request method.
    restarted_raw = FakeExchange()
    restarted_raw.request = limited
    restarted_governor = make_governor(
        cooldown_429=5, state_path=state_path
    )
    with pytest.raises(APIGovernorError) as caught:
        GovernedExchange(restarted_raw, restarted_governor).fetch_positions(["ETH/USDT"])
    assert caught.value.code == 429
    assert len(calls) == 1


def test_endpoint_priority_policy_keeps_execution_and_protection_ahead_of_scans():
    classify = BingXAPIGovernor.classify_request
    assert classify("trade/order", "private", "POST") == ("order_write", 0)
    assert classify("trade/openOrders", "private", "GET") == ("account_orders", 0)
    assert classify("user/positions", "private", "GET") == ("account_positions", 0)
    assert classify("user/balance", "private", "GET") == ("account_balance", 1)
    assert classify("quote/klines", "public", "GET") == ("market_kline", 2)


def test_scanner_watchlist_dashboard_and_trade_management_share_one_budget():
    from concurrent.futures import ThreadPoolExecutor

    raw = FakeExchange()
    governor = make_governor(max_concurrent=4)
    exchange = GovernedExchange(raw, governor)

    def scanner(symbol):
        return exchange.fetch_ohlcv(symbol, "15m", 150)

    def watchlist(symbol):
        return exchange.fetch_ohlcv(symbol, "15m", 150)

    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(scanner, f"SCAN{i}/USDT") for i in range(10)]
        jobs += [pool.submit(watchlist, f"WATCH{i}/USDT") for i in range(12)]
        jobs += [
            pool.submit(exchange.fetch_balance),       # Dashboard shared account read
            pool.submit(exchange.fetch_positions, ["BTC/USDT"]),  # Reconciliation
            pool.submit(exchange.swapV2PrivateGetTradeOpenOrders, {"symbol": "BTC-USDT"}),
        ]
        for job in jobs:
            job.result(timeout=5)

    snapshot = governor.snapshot()
    assert snapshot["totals"]["attempts"] == 25
    assert snapshot["categories"]["market_kline"]["attempts"] == 22
    assert snapshot["categories"]["account_balance"]["attempts"] == 1
    assert snapshot["categories"]["account_positions"]["attempts"] == 1
    assert snapshot["categories"]["account_orders"]["attempts"] == 1
    assert snapshot["active"] == 0
    assert snapshot["queued"] == 0


def test_total_and_free_balance_readers_share_one_bingx_payload(monkeypatch):
    import ggpbot

    calls = []
    def fake_fetch_balance():
        calls.append(1)
        return {"total": {"USDT": 101.0}, "free": {"USDT": 77.0}}

    monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
    monkeypatch.setattr(ggpbot.ex.raw_exchange, "fetch_balance", fake_fetch_balance)
    with ggpbot.CACHE_LOCK:
        ggpbot.CACHE.pop("bingx_balance_payload", None)
        ggpbot.CACHE.pop("balance", None)
        ggpbot.CACHE.pop("free_balance", None)

    assert ggpbot.get_balance() == 101.0
    assert ggpbot.get_free_balance() == 77.0
    assert len(calls) == 1

    with ggpbot.CACHE_LOCK:
        ggpbot.CACHE.pop("bingx_balance_payload", None)
        ggpbot.CACHE.pop("balance", None)
        ggpbot.CACHE.pop("free_balance", None)


def test_market_data_429_pauses_both_kline_and_ticker_requests():
    raw = FakeExchange()
    calls = []
    def limited(path, api="public", method="GET", params=None,
                headers=None, body=None, config=None):
        calls.append(path)
        raise RuntimeError("HTTP 429 Too Many Requests")
    raw.request = limited
    exchange = GovernedExchange(raw, make_governor(cooldown_429=0.2))

    with pytest.raises(RuntimeError, match="429"):
        exchange.fetch_ohlcv("BTC/USDT", "15m", 150)
    with pytest.raises(APIGovernorError) as caught:
        exchange.fetch_ticker("BTC/USDT")
    assert caught.value.code == 429
    assert len(calls) == 1


def test_identical_concurrent_gets_are_single_flight_not_cached():
    raw = FakeExchange()
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def slow_request(path, api="public", method="GET", params=None,
                     headers=None, body=None, config=None):
        calls.append((path, method))
        entered.set()
        release.wait(timeout=2)
        return []

    raw.request = slow_request
    exchange = GovernedExchange(raw, make_governor(max_concurrent=4))
    results = []
    threads = [threading.Thread(
        target=lambda: results.append(exchange.fetch_positions(["BTC/USDT"]))
    ) for _ in range(8)]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=1)
    time.sleep(0.03)
    assert len(calls) == 1
    release.set()
    for thread in threads:
        thread.join(timeout=2)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 8
    assert len(calls) == 1


def test_order_write_invalidates_inflight_get_deduplication():
    raw = FakeExchange()
    position_calls = []
    entered = threading.Event()
    release = threading.Event()

    def controlled_request(path, api="public", method="GET", params=None,
                           headers=None, body=None, config=None):
        if path == "user/positions":
            position_calls.append(path)
            entered.set()
            release.wait(timeout=2)
            return []
        return {"ok": True}

    raw.request = controlled_request
    exchange = GovernedExchange(raw, make_governor(max_concurrent=4))
    first = threading.Thread(target=exchange.fetch_positions, args=(["BTC/USDT"],))
    first.start()
    assert entered.wait(timeout=1)

    # A write changes the request epoch. A later GET must fetch fresh data,
    # not join a pre-write in-flight snapshot.
    exchange.create_order("BTC/USDT", "market", "buy", 1)
    second = threading.Thread(target=exchange.fetch_positions, args=(["BTC/USDT"],))
    second.start()
    time.sleep(0.03)
    assert len(position_calls) == 2

    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
