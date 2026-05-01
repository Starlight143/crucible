"""
features/watch_mode.py
======================
File-change watcher that automatically re-triggers the analysis pipeline.

Primary backend: ``watchdog`` (install with ``pip install watchdog``).
Fallback backend: stat-based polling loop (no extra dependencies required).

Changes to paths matching ``_IGNORED_DIRS`` (git internals, caches, virtual
environments, previous run outputs) are silently ignored.

A debounce timer ensures a burst of file-system events (e.g. a git checkout)
triggers only one re-run after settling for *debounce_seconds*.

Usage::

    from crucible.features.watch_mode import WatchModeRunner

    def my_run():
        # call your pipeline here
        ...

    runner = WatchModeRunner(
        watch_dir="/path/to/project",
        run_fn=my_run,
        debounce_seconds=30.0,
    )
    runner.start(run_immediately=True)   # blocks until KeyboardInterrupt
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Set

# ── Optional watchdog import ──────────────────────────────────────────────────

_WATCHDOG_AVAILABLE = False
try:
    from watchdog.events import FileSystemEventHandler as _FileSystemEventHandler
    from watchdog.observers import Observer as _Observer
    _WATCHDOG_AVAILABLE = True
except ImportError:
    _Observer = None                     # type: ignore[assignment,misc]
    _FileSystemEventHandler = object     # type: ignore[assignment,misc]


# ── Paths to ignore ───────────────────────────────────────────────────────────

_IGNORED_DIRS: Set[str] = {
    ".git",
    "__pycache__",
    "saved_projects",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    ".env",
    "node_modules",
    ".tox",
    "dist",
    "build",
    ".eggs",
    "*.egg-info",
}


def _should_ignore(path: str) -> bool:
    """Return True if *path* (or any of its parents) matches ``_IGNORED_DIRS``."""
    parts = Path(path).parts
    return any(part in _IGNORED_DIRS or part.endswith(".egg-info") for part in parts)


# ── Debounce timer ────────────────────────────────────────────────────────────

class _DebounceTimer:
    """
    Calls *fn* after *delay_seconds* of inactivity.

    Re-arming the timer (via ``trigger()``) resets the countdown.
    Thread-safe.
    """

    def __init__(self, delay_seconds: float, fn: Callable[[], None]) -> None:
        if delay_seconds <= 0:
            raise ValueError("delay_seconds must be positive")
        self._delay = delay_seconds
        self._fn = fn
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def trigger(self) -> None:
        """Reset the debounce countdown."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        with self._lock:
            self._timer = None
        try:
            self._fn()
        except Exception:  # noqa: BLE001
            # Log but do not let an unhandled exception silently kill the
            # background timer thread, which would permanently break watch mode.
            import logging
            logging.getLogger(__name__).exception(
                "_DebounceTimer callback raised an exception; watch mode continues."
            )

    def cancel(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


# ── watchdog event handler ────────────────────────────────────────────────────

if _WATCHDOG_AVAILABLE:
    class _WatchHandler(_FileSystemEventHandler):  # type: ignore[misc]
        def __init__(self, debounce: _DebounceTimer) -> None:
            super().__init__()
            self._debounce = debounce

        def on_any_event(self, event: object) -> None:  # type: ignore[override]
            if getattr(event, "is_directory", False):
                return
            src = str(getattr(event, "src_path", "") or "")
            if src and not _should_ignore(src):
                self._debounce.trigger()


# ── Polling backend ───────────────────────────────────────────────────────────

class _PollingWatcher:
    """
    Simple mtime/size snapshot poller.
    Calls *debounce.trigger()* when any tracked file changes.
    """

    def __init__(
        self,
        watch_dir: str,
        debounce: _DebounceTimer,
        poll_interval: float,
        stop_event: threading.Event,
    ) -> None:
        self._watch_dir = watch_dir
        self._debounce = debounce
        self._interval = poll_interval
        self._stop = stop_event

    def _snapshot(self) -> dict:
        result: dict = {}
        for dirpath, dirnames, filenames in os.walk(self._watch_dir):
            if _should_ignore(dirpath):
                # Prune os.walk so we never recurse into ignored subtrees.
                # Without this, `continue` alone still causes os.walk to yield
                # every file inside node_modules/__pycache__/etc., making each
                # snapshot O(N) in the total file count rather than O(tracked).
                dirnames[:] = []
                continue
            # Prune ignored subdirectories in-place so os.walk skips them
            dirnames[:] = [d for d in dirnames if not _should_ignore(os.path.join(dirpath, d))]
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                try:
                    st = os.stat(fpath)
                    result[fpath] = (st.st_mtime, st.st_size)
                except OSError:
                    pass
        return result

    def run(self) -> None:
        snapshot = self._snapshot()
        while not self._stop.is_set():
            time.sleep(self._interval)
            new_snapshot = self._snapshot()
            if new_snapshot != snapshot:
                snapshot = new_snapshot
                self._debounce.trigger()


# ── Public runner ─────────────────────────────────────────────────────────────

class WatchModeRunner:
    """
    Watch *watch_dir* for filesystem changes and call *run_fn* on change.

    Blocks in ``start()`` until ``stop()`` is called or a ``KeyboardInterrupt``
    is raised.

    Args:
        watch_dir:          Directory to monitor (resolved to absolute path).
        run_fn:             Zero-argument callable invoked after debounce settles.
        debounce_seconds:   Quiet period after last event before triggering run.
        poll_interval_seconds: Polling interval when watchdog is unavailable.
    """

    def __init__(
        self,
        watch_dir: str,
        run_fn: Callable[[], None],
        *,
        debounce_seconds: float = 30.0,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.watch_dir = str(Path(watch_dir).resolve())
        self._run_fn = run_fn
        self._debounce_seconds = max(1.0, debounce_seconds)
        self._poll_interval = max(0.5, poll_interval_seconds)
        self._stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._debounce = _DebounceTimer(self._debounce_seconds, self._on_change)

    def _on_change(self) -> None:
        backend = "watchdog" if _WATCHDOG_AVAILABLE else "poll"
        # Non-blocking acquire: if a run is already in progress, skip this
        # trigger rather than queuing a second concurrent execution.
        if not self._run_lock.acquire(blocking=False):
            print(
                f"[WatchMode/{backend}] Change detected — previous run still "
                f"in progress, skipping.",
                flush=True,
            )
            return
        try:
            print(
                f"\n[WatchMode/{backend}] Change detected — triggering analysis…",
                flush=True,
            )
            try:
                self._run_fn()
            except KeyboardInterrupt:
                self._stop_event.set()
                return
            except Exception as exc:
                print(f"[WatchMode] Run raised an exception: {exc}", file=sys.stderr, flush=True)
            print(
                f"[WatchMode] Watching {self.watch_dir} "
                f"(debounce: {self._debounce_seconds}s)…",
                flush=True,
            )
        finally:
            self._run_lock.release()

    def start(self, *, run_immediately: bool = True) -> None:
        """
        Start the watch loop and block until stopped.

        Args:
            run_immediately: When True (default), trigger one run before
                             waiting for file-system changes.
        """
        backend = "watchdog" if _WATCHDOG_AVAILABLE else "polling"
        print(
            f"[WatchMode] Watching {self.watch_dir} "
            f"({backend}, debounce: {self._debounce_seconds}s)",
            flush=True,
        )

        if run_immediately:
            self._on_change()

        if _WATCHDOG_AVAILABLE and _Observer is not None:
            self._run_watchdog()
        else:
            self._run_polling()

        print("\n[WatchMode] Stopped.", flush=True)

    def _run_watchdog(self) -> None:
        observer = _Observer()
        handler = _WatchHandler(self._debounce)
        observer.schedule(handler, self.watch_dir, recursive=True)
        observer.start()
        try:
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._debounce.cancel()
            observer.stop()
            observer.join(timeout=5.0)

    def _run_polling(self) -> None:
        poller = _PollingWatcher(
            self.watch_dir,
            self._debounce,
            self._poll_interval,
            self._stop_event,
        )
        try:
            poller.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._debounce.cancel()

    def stop(self) -> None:
        """Signal the watch loop to exit."""
        self._stop_event.set()
        self._debounce.cancel()
