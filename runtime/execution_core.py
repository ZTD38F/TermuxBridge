"""Bounded streaming command execution for the ordinary Termux application UID.

Memory use is bounded by the tail buffer; subprocess output is never captured
wholesale. Limits are per child process (not device-wide Android cgroups).
"""
from __future__ import annotations

from collections import deque
import os
import resource
import selectors
import signal
import subprocess
import threading
import time

# Limits across synchronous MCP calls in a single backend generation.
SLOTS = threading.BoundedSemaphore(2)
DEFAULT_MEMORY_MB = 2048
MAX_MEMORY_MB = 4096
DEFAULT_OUTPUT = 256000
MAX_OUTPUT = 1000000


def _limits(cpu_seconds: int, memory_mb: int):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))
        memory = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    return apply


def _terminate_child(proc: subprocess.Popen, grace=1.5):
    if proc.poll() is not None:
        return
    # Only signal the dedicated process group of this newly created child.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)


def execute(argv, cwd, timeout=30, max_output_chars=DEFAULT_OUTPUT,
            memory_mb=DEFAULT_MEMORY_MB, cpu_seconds=None, cancel=None):
    if not isinstance(timeout, int) or not 1 <= timeout <= 1800:
        raise ValueError("invalid timeout")
    if not isinstance(memory_mb, int) or not 128 <= memory_mb <= MAX_MEMORY_MB:
        raise ValueError("memory_mb must be 128..4096")
    if not isinstance(max_output_chars, int) or not 1 <= max_output_chars <= MAX_OUTPUT:
        raise ValueError("max_output_chars outside limits")
    cpu_seconds = cpu_seconds if cpu_seconds is not None else min(timeout + 5, 1800)
    if not isinstance(cpu_seconds, int) or not 1 <= cpu_seconds <= 1800:
        raise ValueError("cpu_seconds outside limits")
    if not SLOTS.acquire(timeout=2):
        raise RuntimeError("Execution capacity reached (max 2 synchronous commands)")
    proc = None
    start = time.monotonic()
    tail = bytearray()
    total_bytes = 0
    timed_out = False
    cancelled = False
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=_limits(cpu_seconds, memory_mb),
            close_fds=True,
            env=os.environ.copy())
        assert proc.stdout is not None
        os.set_blocking(proc.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            eof = False
            while not eof or proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    _terminate_child(proc)
                if time.monotonic() - start >= timeout and proc.poll() is None:
                    timed_out = True
                    _terminate_child(proc)
                for key, _ in selector.select(timeout=0.1):
                    try:
                        data = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not data:
                        selector.unregister(key.fileobj)
                        eof = True
                        break
                    total_bytes += len(data)
                    tail.extend(data)
                    if len(tail) > max_output_chars:
                        del tail[:len(tail) - max_output_chars]
                if eof and proc.poll() is None:
                    proc.wait(timeout=max(0.1, timeout))
        code = proc.wait()
        return {
            "exit_code": code,
            "output": tail.decode("utf-8", errors="replace"),
            "truncated": total_bytes > max_output_chars,
            "total_output_bytes": total_bytes,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "duration_ms": round((time.monotonic() - start) * 1000),
        }
    finally:
        if proc is not None:
            _terminate_child(proc)
            if proc.stdout is not None:
                proc.stdout.close()
        SLOTS.release()
