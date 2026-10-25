"""工具层基础设施：TTL 缓存、熔断器、故障注入（演示/测试用）。"""
from __future__ import annotations

import time
from collections import deque
from typing import Any, Optional


class TTLCache:
    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, ttl_s: float) -> Optional[Any]:
        item = self._store.get(key)
        if not item:
            return None
        stored_at, value = item
        if time.time() - stored_at <= ttl_s:
            return value
        return None

    def get_stale(self, key: str) -> Optional[tuple[float, Any]]:
        """忽略 TTL 取最近一次成功值，返回 (stored_at, value)。"""
        return self._store.get(key)

    def set(self, key: str, value: Any) -> None:
        self._store[key] = (time.time(), value)

    def clear(self) -> None:
        self._store.clear()


class CircuitBreaker:
    """按 key（通常为数据源）统计失败：窗口内失败 >= 阈值则断开一段时间。"""

    def __init__(self, threshold: int = 3, window_s: float = 60.0, open_s: float = 30.0):
        self.threshold = threshold
        self.window_s = window_s
        self.open_s = open_s
        self._failures: dict[str, deque] = {}
        self._opened_at: dict[str, float] = {}

    def record_failure(self, key: str) -> None:
        now = time.time()
        dq = self._failures.setdefault(key, deque())
        dq.append(now)
        while dq and now - dq[0] > self.window_s:
            dq.popleft()
        if len(dq) >= self.threshold:
            self._opened_at[key] = now

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)
        self._opened_at.pop(key, None)

    def is_open(self, key: str) -> bool:
        opened = self._opened_at.get(key)
        if opened is None:
            return False
        if time.time() - opened > self.open_s:
            # 半开：允许一次尝试
            self._opened_at.pop(key, None)
            self._failures.pop(key, None)
            return False
        return True

    def state(self) -> dict:
        now = time.time()
        return {
            k: {"open": self.is_open(k), "remaining_s": max(0.0, round(self.open_s - (now - v), 1))}
            for k, v in list(self._opened_at.items())
        }

    def reset(self) -> None:
        self._failures.clear()
        self._opened_at.clear()


class FaultInjector:
    """演示 / 测试用故障注入：target ∈ {fuyao, ifind, llm, local}，mode ∈ {error, timeout, ratelimit, off}。"""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._faults: dict[str, dict] = {}

    def set(self, target: str, mode: str, remaining: Optional[int] = None) -> dict:
        if mode == "off":
            self._faults.pop(target, None)
        else:
            self._faults[target] = {"mode": mode, "remaining": remaining}
        return self.state()

    def active(self, target: str) -> Optional[str]:
        if not self.enabled:
            return None
        f = self._faults.get(target)
        if not f:
            return None
        if f["remaining"] is not None:
            if f["remaining"] <= 0:
                self._faults.pop(target, None)
                return None
            f["remaining"] -= 1
            if f["remaining"] <= 0:
                self._faults.pop(target, None)
        return f["mode"]

    def state(self) -> dict:
        return {k: dict(v) for k, v in self._faults.items()}

    def clear(self) -> None:
        self._faults.clear()
