"""Bounded periodic work queue with stable staggering and no catch-up storms."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

class TransientJobError(Exception):
    """Expected server unavailability; back off without flooding stack traces."""

def phase(key: str, interval: float) -> float:
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") / 2**64 * interval

@dataclass
class Job:
    key: str
    interval: float
    callback: Callable[[], Awaitable[None]]
    due: float
    timeout: float = 90
    pending: bool = False
    failures: int = 0

class StaggeredQueue:
    def __init__(self, workers: int = 3, capacity: int = 256):
        self.jobs: dict[str, Job] = {}
        self.queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=capacity)
        self.workers = max(1, min(8, workers))
        self.completed = 0

    def add(self, key: str, interval: float, callback, *, timeout: float = 90) -> None:
        if key in self.jobs:
            return
        self.jobs[key] = Job(key, interval, callback, time.monotonic() + phase(key, interval), timeout)

    def remove_prefix(self, prefix: str) -> None:
        self.jobs = {key: job for key, job in self.jobs.items() if not key.startswith(prefix)}

    def trigger(self, key: str) -> None:
        if key in self.jobs:
            self.jobs[key].due = time.monotonic()

    def enqueue_due(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for job in sorted(self.jobs.values(), key=lambda item: item.due):
            if job.pending or job.due > now:
                continue
            if self.queue.full():
                break
            job.pending = True
            self.queue.put_nowait(job)

    async def _worker(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                if self.jobs.get(job.key) is not job:
                    continue
                await asyncio.wait_for(job.callback(), timeout=job.timeout)
                job.failures = 0
                self.completed += 1
            except asyncio.CancelledError:
                raise
            except TransientJobError as exc:
                job.failures += 1
                logger.warning("Trabalho %s reagendado: %s", job.key, exc)
            except Exception:
                job.failures += 1
                logger.exception("Falha no trabalho %s", job.key)
            finally:
                job.pending = False
                job.due = time.monotonic() + min(1800, job.interval * 2**min(job.failures, 4))
                self.queue.task_done()

    async def run(self) -> None:
        workers = [asyncio.create_task(self._worker(), name=f"pz-worker-{i}") for i in range(self.workers)]
        try:
            while True:
                self.enqueue_due()
                await asyncio.sleep(0.5)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
