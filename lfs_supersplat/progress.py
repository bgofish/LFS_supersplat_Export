"""Thread-safe multipart upload progress accounting."""

from __future__ import annotations

import threading
import time

from .models import ProgressSnapshot


class ProgressTracker:
    def __init__(
        self,
        total_bytes: int,
        total_parts: int,
        committed: dict[int, int] | None = None,
    ) -> None:
        self.total_bytes = max(int(total_bytes), 0)
        self.total_parts = max(int(total_parts), 0)
        self._committed = dict(committed or {})
        self._in_flight: dict[int, int] = {}
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._started_bytes = sum(self._committed.values())

    def begin_attempt(self, part_number: int) -> None:
        with self._lock:
            self._in_flight[part_number] = 0

    def add_bytes(self, part_number: int, byte_count: int) -> ProgressSnapshot:
        with self._lock:
            self._in_flight[part_number] = self._in_flight.get(part_number, 0) + max(
                int(byte_count), 0
            )
            return self._snapshot_locked()

    def reset_attempt(self, part_number: int) -> ProgressSnapshot:
        with self._lock:
            self._in_flight.pop(part_number, None)
            return self._snapshot_locked()

    def commit(self, part_number: int, size: int) -> ProgressSnapshot:
        with self._lock:
            self._in_flight.pop(part_number, None)
            self._committed[part_number] = max(int(size), 0)
            return self._snapshot_locked()

    def snapshot(self) -> ProgressSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> ProgressSnapshot:
        uploaded = min(
            sum(self._committed.values()) + sum(self._in_flight.values()),
            self.total_bytes,
        )
        elapsed = max(time.monotonic() - self._started_at, 1e-6)
        rate = max(uploaded - self._started_bytes, 0) / elapsed
        remaining = max(self.total_bytes - uploaded, 0)
        eta = remaining / rate if rate > 0 else None
        fraction = uploaded / self.total_bytes if self.total_bytes > 0 else 0.0
        return ProgressSnapshot(
            uploaded_bytes=uploaded,
            total_bytes=self.total_bytes,
            fraction=max(0.0, min(fraction, 1.0)),
            bytes_per_second=rate,
            eta_seconds=eta,
            completed_parts=len(self._committed),
            total_parts=self.total_parts,
        )


def format_bytes(byte_count: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(max(byte_count, 0))
    unit = 0
    while value >= 1024 and unit < len(units) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{value:.0f} {units[unit]}"
    return f"{value:.1f} {units[unit]}"


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    total = max(int(seconds + 0.999), 0)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"
