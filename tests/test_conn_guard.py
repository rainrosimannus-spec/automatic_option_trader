"""The options and portfolio IBKR connections have SEPARATE locks (2026-10-06).

Before: one lock for both, so the portfolio monthly screener (30-75 min) froze the options side —
no scans, no queued orders, no health check, an empty options dashboard.

What must hold for two locks to be safe, and is checked here:
  * a long hold on one connection's lock does not delay the other connection at all;
  * a request for connection X always runs on X's own loop under X's own lock, whatever lock
    the caller took and wherever its thread was pointed (BoundIB enforces it, not call sites);
  * two threads can hammer both connections at once without "event loop is already running";
  * the two locks cannot deadlock each other (bounded cross wait).
No gateway needed: IB._run executes any awaitable on the bound loop.
"""
import asyncio
import threading
import time

import pytest

from src.broker import conn_guard as g
from src.broker.conn_guard import BoundIB, ConnLock, ConnectionBusy, CrossLockTimeout


def _conn(name):
    loop = asyncio.new_event_loop()
    lock = ConnLock(name, lambda: loop)
    return BoundIB(lock, loop), lock, loop


async def _which_loop(delay=0.0):
    if delay:
        await asyncio.sleep(delay)
    return asyncio.get_running_loop()


def _in_thread(fn):
    out = {}
    def run():
        try:
            out["v"] = fn()
        except BaseException as e:      # noqa: BLE001 — surface anything to the test
            out["e"] = e
    t = threading.Thread(target=run)
    t.start()
    return t, out


# ── the lock ────────────────────────────────────────────────────────────────────────────────

def test_lock_is_reentrant_and_keeps_the_rlock_call_shapes():
    lock = ConnLock("options")
    assert lock.acquire(timeout=1) is True
    assert lock.acquire(blocking=False) is True       # same thread → re-entrant
    with lock:
        assert lock.held_by_me()
    lock.release(); lock.release()
    assert not lock.held_by_me()
    with pytest.raises(RuntimeError):                 # like RLock: releasing what you don't hold
        lock.release()


def test_one_sides_long_hold_does_not_make_the_other_side_wait():
    opt, pf = ConnLock("options"), ConnLock("portfolio")
    release = threading.Event()
    def screener():                                   # portfolio job holding its lock a long time
        with pf:
            release.wait(5)
    t, _ = _in_thread(screener)
    time.sleep(0.1)
    t0 = time.monotonic()
    assert opt.acquire(timeout=1) is True             # options is free immediately
    opt.release()
    assert time.monotonic() - t0 < 0.2
    assert pf.acquire(blocking=False) is False        # and portfolio really is held
    release.set(); t.join()


def test_the_two_locks_cannot_deadlock_each_other(monkeypatch):
    monkeypatch.setattr(ConnLock, "CROSS_WAIT", 0.4)
    opt, pf = ConnLock("options"), ConnLock("portfolio")
    barrier = threading.Barrier(2)
    def a():
        with opt:
            barrier.wait(2)
            with pf:
                return "a"
    def b():
        with pf:
            barrier.wait(2)
            with opt:
                return "b"
    (ta, ra), (tb, rb) = _in_thread(a), _in_thread(b)
    ta.join(5); tb.join(5)
    assert not ta.is_alive() and not tb.is_alive()    # nobody hangs
    errs = [r["e"] for r in (ra, rb) if "e" in r]
    assert errs and all(isinstance(e, CrossLockTimeout) for e in errs)
    assert opt.acquire(blocking=False) and pf.acquire(blocking=False)   # both locks were freed
    opt.release(); pf.release()


def test_taking_a_lock_points_the_thread_at_that_connection_and_puts_it_back():
    ib_o, lock_o, loop_o = _conn("options")
    ib_p, lock_p, loop_p = _conn("portfolio")
    def body():
        asyncio.set_event_loop(loop_o)                # thread starts pointed at options
        with lock_p:
            inside = asyncio.get_event_loop()
        return inside, asyncio.get_event_loop()
    t, r = _in_thread(body); t.join()
    assert r["v"] == (loop_p, loop_o)


def test_a_block_that_repoints_the_thread_itself_is_respected():
    _, lock_o, loop_o = _conn("options")
    other = asyncio.new_event_loop()
    third = asyncio.new_event_loop()
    def body():
        asyncio.set_event_loop(other)
        with lock_o:
            asyncio.set_event_loop(third)             # deliberate re-point inside the block
        return asyncio.get_event_loop()
    t, r = _in_thread(body); t.join()
    assert r["v"] is third


# ── the bound connection ────────────────────────────────────────────────────────────────────

def test_request_runs_on_its_own_loop_even_from_a_thread_pointed_elsewhere():
    ib_o, _, loop_o = _conn("options")
    ib_p, _, loop_p = _conn("portfolio")
    def body():
        asyncio.set_event_loop(loop_o)                # WRONG loop for a portfolio request
        ran_on = ib_p._run(_which_loop())
        return ran_on, asyncio.get_event_loop()
    t, r = _in_thread(body); t.join()
    assert r["v"] == (loop_p, loop_o)                 # ran on portfolio's loop; thread restored


def test_request_works_from_a_thread_with_no_loop_at_all():
    ib_p, _, loop_p = _conn("portfolio")
    t, r = _in_thread(lambda: ib_p._run(_which_loop()))
    t.join()
    assert r["v"] is loop_p


def test_request_under_the_WRONG_lock_is_still_serialized_on_the_right_one():
    ib_p, lock_p, _ = _conn("portfolio")
    _, lock_o, _ = _conn("options")
    seen = {}
    async def probe():
        seen["held"] = lock_p.held_by_me()
        return 1
    def body():
        with lock_o:                                  # caller took the options lock by mistake
            return ib_p._run(probe())
    t, r = _in_thread(body); t.join()
    assert r["v"] == 1 and seen["held"] is True


def test_request_handle_is_created_on_the_right_loop_from_a_thread_pointed_elsewhere():
    # 34 ib_insync request methods create their result handle BEFORE _run() is entered
    # (reqPositions, reqAllOpenOrders, option-chain params, ...). From a thread pointed at the
    # other connection that handle used to land on the wrong loop — the request then failed with
    # "attached to a different loop". Found by the live gateway probe, 2026-10-06.
    ib_o, _, loop_o = _conn("options")
    ib_p, _, loop_p = _conn("portfolio")
    def body():
        asyncio.set_event_loop(loop_o)                        # pointed at OPTIONS
        fut = ib_p.wrapper.startReq("probe-key")              # handle for a PORTFOLIO request
        async def answer():
            fut.set_result(42)
            return await fut
        return fut.get_loop(), ib_p._run(answer()), asyncio.get_event_loop()
    t, r = _in_thread(body); t.join()
    assert "e" not in r, repr(r.get("e"))
    assert r["v"] == (loop_p, 42, loop_o)


def test_request_handle_from_a_thread_with_no_loop_at_all():
    ib_p, _, loop_p = _conn("portfolio")
    t, r = _in_thread(lambda: ib_p.wrapper.startReq("k2").get_loop())
    t.join()
    assert r.get("v") is loop_p, repr(r)


def test_outgoing_messages_look_up_the_connections_own_loop(monkeypatch):
    from ib_insync.client import Client
    seen = {}
    monkeypatch.setattr(Client, "sendMsg", lambda self, msg: seen.setdefault("loop", asyncio.get_event_loop()))
    ib_p, _, loop_p = _conn("portfolio")
    other = asyncio.new_event_loop()
    def body():
        asyncio.set_event_loop(other)
        ib_p.client.sendMsg("x")
        return asyncio.get_event_loop()
    t, r = _in_thread(body); t.join()
    assert seen["loop"] is loop_p and r["v"] is other


def test_both_connections_hammered_from_four_threads_never_collide():
    ib_o, lock_o, loop_o = _conn("options")
    ib_p, lock_p, loop_p = _conn("portfolio")
    stop = time.monotonic() + 1.5
    def hammer(ib, lock, loop, locked):
        n = 0
        while time.monotonic() < stop:
            if locked:
                with lock:
                    assert ib._run(_which_loop(0.001)) is loop
                    ib.sleep(0.001)
            else:
                assert ib._run(_which_loop(0.001)) is loop
            n += 1
            time.sleep(0.002)     # real jobs do work between broker calls; a tight
        return n                  # release-and-reacquire loop starves others on ANY Python lock
    jobs = [_in_thread(lambda: hammer(ib_o, lock_o, loop_o, True)),
            _in_thread(lambda: hammer(ib_o, lock_o, loop_o, False)),
            _in_thread(lambda: hammer(ib_p, lock_p, loop_p, True)),
            _in_thread(lambda: hammer(ib_p, lock_p, loop_p, False))]
    for t, _ in jobs:
        t.join(10)
    for t, r in jobs:
        assert not t.is_alive()
        assert "e" not in r, repr(r.get("e"))         # no "event loop is already running"
        assert r["v"] > 20


def test_a_slow_portfolio_request_does_not_delay_options_requests():
    ib_o, _, _ = _conn("options")
    ib_p, lock_p, _ = _conn("portfolio")
    def slow():
        with lock_p:
            return ib_p._run(_which_loop(1.0))        # portfolio busy for a second
    t, _ = _in_thread(slow)
    time.sleep(0.1)
    t0 = time.monotonic()
    for _ in range(20):
        ib_o._run(_which_loop())
    assert time.monotonic() - t0 < 0.5
    t.join()


def test_unlocked_request_waits_for_the_connection_then_fails_cleanly(monkeypatch):
    monkeypatch.setattr(BoundIB, "REQUEST_LOCK_WAIT", 0.3)
    ib_p, lock_p, _ = _conn("portfolio")
    release = threading.Event()
    def holder():
        with lock_p:
            release.wait(5)
    t, _ = _in_thread(holder)
    time.sleep(0.1)
    with pytest.raises(ConnectionBusy):
        ib_p._run(_which_loop())
    release.set(); t.join()
    assert ib_p._run(_which_loop()) is ib_p._conn_loop     # and works once it is free


def test_sleep_never_collides_with_the_thread_that_holds_the_lock():
    ib_p, lock_p, _ = _conn("portfolio")
    release = threading.Event()
    def holder():
        with lock_p:
            while not release.is_set():
                ib_p._run(_which_loop(0.01))          # actively driving the loop
    t, r = _in_thread(holder)
    time.sleep(0.1)
    t0 = time.monotonic()
    assert ib_p.sleep(0.3) is True                    # un-locked sleep from another thread
    assert 0.25 < time.monotonic() - t0 < 1.0
    release.set(); t.join()
    assert "e" not in r, repr(r.get("e"))             # the holder was not disturbed


def test_connecting_does_not_need_the_lock():
    # A reconnect must be able to proceed while a job still holds the lock around the dead
    # connection: a connection being established is on a brand-new loop nobody else can drive.
    ib_p, lock_p, loop_p = _conn("portfolio")
    release = threading.Event()
    def holder():
        with lock_p:
            release.wait(5)
    t, _ = _in_thread(holder)
    time.sleep(0.1)
    ib_p._connecting = True
    try:
        assert ib_p._run(_which_loop()) is loop_p
    finally:
        ib_p._connecting = False
    release.set(); t.join()


# ── the wiring ──────────────────────────────────────────────────────────────────────────────

def test_the_two_sides_really_have_two_different_locks():
    from src.broker.connection import get_ib_lock
    from src.portfolio.connection import get_portfolio_lock
    assert isinstance(get_ib_lock(), ConnLock) and isinstance(get_portfolio_lock(), ConnLock)
    assert get_ib_lock() is not get_portfolio_lock()
    assert (get_ib_lock().name, get_portfolio_lock().name) == ("options", "portfolio")


def test_options_health_check_no_longer_waits_on_the_portfolio_side():
    import inspect
    from src.scheduler import jobs
    src = inspect.getsource(jobs.job_health_check)
    assert "refresh_portfolio_open_orders_cache()" not in src
    assert "refresh_portfolio_pending_orders_cache()" not in src
    from src.portfolio import scheduler as ps
    psrc = inspect.getsource(ps.job_portfolio_health_check)
    assert "refresh_portfolio_open_orders_cache()" in psrc
    assert "refresh_portfolio_pending_orders_cache()" in psrc
