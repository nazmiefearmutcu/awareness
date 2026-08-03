"""M-04: the stdin reader must never block process exit (no Enter needed).

The old ``loop.run_in_executor(sys.stdin.readline)`` parked a non-daemon
executor thread on a blocking read — with an open TTY the process hung at
exit until the user pressed Enter. ``_StdinLineReader`` polls with select
and is fully daemon/stop-able.
"""

from __future__ import annotations

import asyncio
import io
import os
import threading
import time

import pytest

from awareness.cli.main import _StdinLineReader


def _make_stdin() -> tuple[io.TextIOWrapper, int]:
    r, w = os.pipe()
    # fdopen in binary mode so we wrap exactly once in TextIOWrapper.
    return io.TextIOWrapper(os.fdopen(r, "rb", closefd=False), encoding="utf-8"), w


@pytest.mark.asyncio
async def test_reader_dispatches_lines_and_stops() -> None:
    loop = asyncio.get_running_loop()
    received: list[str] = []

    def handler(line: str) -> None:
        received.append(line)

    stdin, w = _make_stdin()
    import sys

    old_stdin = sys.stdin
    sys.stdin = stdin
    try:
        reader = _StdinLineReader(loop, handler)
        reader.start()
        # No input available → the reader must not block anything.
        await asyncio.sleep(0.5)
        assert received == []
        os.write(w, b"/status\n")
        deadline = time.time() + 3
        while not received and time.time() < deadline:
            await asyncio.sleep(0.05)
        assert received == ["/status"]
        reader.stop()
        reader._thread.join(timeout=2.0)
        assert not reader._thread.is_alive()
    finally:
        sys.stdin = old_stdin
        os.close(w)
        stdin.close()


@pytest.mark.asyncio
async def test_reader_stop_unblocks_without_input() -> None:
    """stop() must terminate the polling thread promptly even with zero input."""
    loop = asyncio.get_running_loop()
    stdin, w = _make_stdin()
    import sys

    old_stdin = sys.stdin
    sys.stdin = stdin
    try:
        reader = _StdinLineReader(loop, lambda line: None)
        reader.start()
        await asyncio.sleep(0.2)
        t0 = time.time()
        reader.stop()
        reader._thread.join(timeout=2.0)
        assert not reader._thread.is_alive()
        # One poll interval (0.2s) is enough — far below any "hang" threshold.
        assert time.time() - t0 < 1.5
    finally:
        sys.stdin = old_stdin
        os.close(w)
        stdin.close()


def test_reader_thread_is_daemon() -> None:
    """Even a leaked reader must never block interpreter exit."""
    loop = asyncio.new_event_loop()
    reader = _StdinLineReader(loop, lambda line: None)
    assert reader._thread is None or reader._thread.daemon
