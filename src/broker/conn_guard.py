"""
Per-connection lock for the two IBKR connections (options / portfolio).

WHY THIS EXISTS (2026-10-06)
The process holds two independent IBKR connections — options (Maggy) and portfolio (Winston):
different gateways, accounts, companies. Until now ONE threading lock serialized every call on
BOTH, so a long portfolio job (the monthly screener: 30-75 min) froze the options side too —
no option scans, no queued orders, no trade sync, no health check, an empty options dashboard.

Each connection now has its OWN lock. A portfolio job can only ever make portfolio work wait.

WHAT MAKES TWO LOCKS SAFE (the 2026-06-09 attempt at this failed without it)
ib_insync runs every request of a connection on that connection's internal event loop, and only
one thread may drive a given loop at a time. Two locks are therefore only safe if
  (a) the two connections never share a loop, and
  (b) a request for connection X always drives X's loop, under X's lock —
      whatever lock the caller took and whatever loop its thread happened to be pointed at.
(a) is guaranteed by each _connect() creating the connection on its own fresh loop.
(b) is guaranteed HERE, by the connection object itself (BoundIB), not by the ~240 call sites:
    every loop-driving entry point takes the connection's own lock (re-entrant, so free for
    callers that already hold it) and points the thread at the connection's own loop for the
    duration of the call.
All coordination is plain `threading` locks. There is no async code in this module.

DEADLOCKS BETWEEN THE TWO LOCKS
With two locks, thread 1 holding A and waiting for B while thread 2 holds B and waits for A
would hang forever. A thread that already holds one connection's lock therefore never waits
unboundedly for the other: it waits at most CROSS_WAIT seconds and then raises CrossLockTimeout,
which unwinds and frees what it held. (No call path that nests the two locks is known; this is
the guard for one that is missed or added later.)
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional

from ib_insync import IB, util

from src.core.logger import get_logger

log = get_logger(__name__)

_tls = threading.local()


def _held() -> dict:
    """{ConnLock: depth} for the locks THIS thread currently holds."""
    h = getattr(_tls, "held", None)
    if h is None:
        h = _tls.held = {}
    return h


def _pins() -> dict:
    """{ConnLock: _Pin} — the loop pin taken with each lock this thread holds."""
    p = getattr(_tls, "pins", None)
    if p is None:
        p = _tls.pins = {}
    return p


class ConnectionBusy(RuntimeError):
    """The connection's lock could not be obtained in time (another thread is using it)."""


class CrossLockTimeout(ConnectionBusy):
    """A thread holding one connection's lock gave up waiting for the other's (deadlock guard)."""


def _current_loop():
    """The loop this thread is currently pointed at, or None. Never raises.
    (No warnings.catch_warnings here on purpose: it rewrites process-global state and is not
    thread-safe, and this runs on every broker call from many threads.)"""
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except Exception:
        return None


class _Pin:
    """Point this thread at `loop` for a block; afterwards put back what was there before —
    unless the code inside re-pointed the thread itself, in which case that choice stands."""
    __slots__ = ("loop", "prev", "active")

    def __init__(self, loop):
        self.loop = loop
        self.prev = None
        self.active = False

    def __enter__(self):
        loop = self.loop
        if loop is None or loop.is_closed():
            return self
        self.prev = _current_loop()
        if self.prev is not loop:
            asyncio.set_event_loop(loop)
        self.active = True
        return self

    def __exit__(self, *exc):
        if not self.active:
            return False
        self.active = False
        prev = self.prev
        if prev is None or prev is self.loop or prev.is_closed():
            return False                      # nothing better to go back to — stay pointed here
        if _current_loop() is not self.loop:
            return False                      # the block re-pointed the thread on purpose
        asyncio.set_event_loop(prev)
        return False


class ConnLock:
    """Re-entrant lock for ONE IBKR connection. Drop-in for the threading.RLock it replaces:
    `with lock:`, `lock.acquire(timeout=..)`, `lock.acquire(blocking=False)`, `lock.release()`.

    Taking it also points the thread at that connection's loop (see module note); releasing the
    outermost hold puts the thread back where it was."""

    CROSS_WAIT = 30.0     # max seconds to wait for this lock while holding the OTHER connection's

    def __init__(self, name: str, loop_getter: Optional[Callable[[], object]] = None):
        self.name = name
        self._lock = threading.RLock()
        self._loop_getter = loop_getter

    def __repr__(self) -> str:
        return f"<ConnLock {self.name}>"

    def held_by_me(self) -> bool:
        return _held().get(self, 0) > 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        held = _held()
        depth = held.get(self, 0)
        if depth:                             # re-entrant: this thread already owns it
            self._lock.acquire()
            held[self] = depth + 1
            return True

        if not blocking:
            ok = self._lock.acquire(False)
        elif timeout is None or timeout < 0:
            if held:                          # holding the other connection's lock → bounded wait
                ok = self._lock.acquire(timeout=self.CROSS_WAIT)
                if not ok:
                    others = ", ".join(l.name for l in held)
                    log.error("ibkr_cross_lock_wait_timed_out", wanted=self.name, holding=others,
                              waited_s=self.CROSS_WAIT)
                    raise CrossLockTimeout(
                        f"waited {self.CROSS_WAIT:.0f}s for the {self.name} IBKR lock while "
                        f"holding the {others} lock — giving up to avoid a deadlock")
            else:
                ok = self._lock.acquire()
        else:
            ok = self._lock.acquire(timeout=timeout)

        if ok:
            held[self] = 1
            pin = None
            try:
                pin = _Pin(self._loop_getter() if self._loop_getter else None)
                pin.__enter__()
            except Exception as e:            # pinning must never break locking
                log.warning("ibkr_lock_pin_failed", lock=self.name, error=str(e))
                pin = None
            _pins()[self] = pin
        return ok

    def release(self) -> None:
        held = _held()
        depth = held.get(self, 0)
        if depth > 1:
            held[self] = depth - 1
        elif depth == 1:
            pin = _pins().pop(self, None)
            if pin is not None:
                try:
                    pin.__exit__(None, None, None)
                except Exception:
                    pass
            del held[self]
        self._lock.release()                  # raises RuntimeError if not owned, like RLock

    def __enter__(self):
        self.acquire()
        return True

    def __exit__(self, *exc):
        self.release()
        return False


class BoundIB(IB):
    """An IB connection that owns its lock and its loop.

    Every entry point that drives the event loop — a synchronous request (`_run`), `sleep`,
    `waitOnUpdate` — runs on THIS connection's loop under THIS connection's lock, regardless of
    which lock the caller holds or where its thread was pointed. Callers that already hold the
    right lock pay nothing (re-entrant)."""

    REQUEST_LOCK_WAIT = 10.0   # an un-locked caller waits this long for the connection, then errors

    def __init__(self, conn_lock: ConnLock, loop, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._conn_lock = conn_lock
        self._conn_loop = loop
        self._connecting = False
        self._pin_library_entry_points()

    def _pin_library_entry_points(self) -> None:
        """Two places inside ib_insync look up "the current thread's loop" BEFORE any of the
        methods below gets to run, so they are pinned at the source:

          * wrapper.startReq — creates the result handle for a request. 34 request methods
            (positions, open orders, option-chain parameters, account updates, ...) call it
            while building their argument, i.e. before _run() is entered. From a thread pointed
            at the other connection the handle landed on the wrong loop and the request failed
            ("attached to a different loop") — found by the live probe on 2026-10-06.
          * client.sendMsg — stamps and, when throttled, defers outgoing messages on a loop.

        No lock is taken here (these never drive the loop); they only make sure the right loop
        is the one looked up."""
        loop = self._conn_loop

        def pinned(fn):
            def call(*a, **k):
                with _Pin(loop):
                    return fn(*a, **k)
            call.__name__ = getattr(fn, "__name__", "call")
            return call

        self.wrapper.startReq = pinned(self.wrapper.startReq)
        self.client.sendMsg = pinned(self.client.sendMsg)

    # A connection being established sits on a brand-new loop that no other thread can be
    # driving, so connecting needs no lock — and must not wait for one: a reconnect has to be
    # able to proceed while some job still holds the lock around the dead connection.
    def connect(self, *args, **kwargs):
        self._connecting = True
        try:
            return super().connect(*args, **kwargs)
        finally:
            self._connecting = False

    def _run(self, *awaitables):
        if self._connecting:
            with _Pin(self._conn_loop):
                return super()._run(*awaitables)
        lock = self._conn_lock
        if not lock.acquire(timeout=self.REQUEST_LOCK_WAIT):
            for a in awaitables:              # don't leave "coroutine was never awaited" behind
                close = getattr(a, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            log.warning("ibkr_connection_busy", connection=lock.name,
                        waited_s=self.REQUEST_LOCK_WAIT)
            raise ConnectionBusy(
                f"{lock.name} IBKR connection busy — another thread held its lock for "
                f"{self.REQUEST_LOCK_WAIT:.0f}s")
        try:
            with _Pin(self._conn_loop):
                return super()._run(*awaitables)
        finally:
            lock.release()

    def sleep(self, secs: float = 0.02) -> bool:   # type: ignore[override]
        """Wait `secs` while this connection keeps processing. If another thread holds the lock
        it is the one driving the loop, so a plain wait is equivalent — never collide with it."""
        lock = self._conn_lock
        t0 = time.monotonic()
        if not lock.acquire(timeout=max(0.0, secs)):
            return True                       # waited the whole time for the lock — that WAS the sleep
        try:
            remaining = secs - (time.monotonic() - t0)
            with _Pin(self._conn_loop):
                util.sleep(remaining if remaining > 0 else 0.02)
        finally:
            lock.release()
        return True

    def waitOnUpdate(self, timeout: float = 0) -> bool:
        lock = self._conn_lock
        if not lock.acquire(timeout=self.REQUEST_LOCK_WAIT):
            raise ConnectionBusy(f"{lock.name} IBKR connection busy")
        try:
            with _Pin(self._conn_loop):
                return super().waitOnUpdate(timeout)
        finally:
            lock.release()
