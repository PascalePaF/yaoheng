"""Tk-owned scheduling and worker-result delivery.

Workers only enqueue Python values.  Every Tcl call remains on the UI thread.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from queue import Empty, SimpleQueue


class TkResultBridge:
    def __init__(
        self, owner: tk.Misc, callback: Callable[..., None], poll_ms: int = 25
    ) -> None:
        self.owner = owner
        self.callback = callback
        self.poll_ms = max(1, poll_ms)
        self._poll_delay = self.poll_ms
        self._results: SimpleQueue[tuple[object, ...]] = SimpleQueue()
        self._pending = 0
        self._poll_job: str | None = None
        self._closed = False

    def expect(self) -> None:
        if not self._closed:
            self._pending += 1
            self._ensure_poll()

    def deliver(self, *payload: object) -> None:
        if not self._closed:
            self._results.put(payload)

    def _ensure_poll(self) -> None:
        if self._closed or self._poll_job is not None:
            return
        try:
            self._poll_job = self.owner.after(self._poll_delay, self._poll)
        except tk.TclError:
            self.close()

    def _poll(self) -> None:
        self._poll_job = None
        if self._closed:
            return
        delivered = False
        while self._pending:
            try:
                payload = self._results.get_nowait()
            except Empty:
                break
            self._pending -= 1
            delivered = True
            try:
                self.callback(*payload)
            except Exception:
                self._poll_delay = self.poll_ms
                if self._pending and not self._closed:
                    self._ensure_poll()
                raise
            if self._closed:
                return
        if self._pending:
            self._poll_delay = (
                self.poll_ms if delivered
                else min(250, max(self.poll_ms, self._poll_delay * 2))
            )
            self._ensure_poll()
        else:
            self._poll_delay = self.poll_ms

    def close(self) -> None:
        self._closed = True
        self._pending = 0
        if self._poll_job is not None:
            try:
                self.owner.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None


class TkAfterJobs:
    """Own short-lived callbacks so widget destruction can cancel them."""

    def __init__(self, owner: tk.Misc) -> None:
        self.owner = owner
        self.jobs: set[str] = set()
        self.closed = False

    def schedule(self, delay: int, callback: Callable[[], None], idle: bool = False) -> str | None:
        if self.closed:
            return None
        holder: list[str] = []

        def run() -> None:
            if holder:
                self.jobs.discard(holder[0])
            if not self.closed:
                callback()

        try:
            job = self.owner.after_idle(run) if idle else self.owner.after(delay, run)
        except tk.TclError:
            return None
        holder.append(job)
        self.jobs.add(job)
        return job

    def cancel_all(self) -> None:
        self.closed = True
        for job in tuple(self.jobs):
            try:
                self.owner.after_cancel(job)
            except tk.TclError:
                pass
        self.jobs.clear()
