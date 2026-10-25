"""Ограничение частоты: защита от подбора пароля и массовых регистраций.

Счётчики — в памяти процесса. Для нескольких копий приложения нужен общий счётчик (например, Redis) —
это в «Потенциале развития». Для MVP с одним процессом приложения этого достаточно.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class Limiter:
    def __init__(self, max_hits: int, window_s: int):
        self.max_hits, self.window_s = max_hits, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def retry_after(self, *keys: str) -> int:
        """Сколько секунд ждать, если по любому из ключей превышен лимит; 0 — можно."""
        now = time.monotonic()
        with self._lock:
            waits = [int(self.window_s - (now - q[0])) + 1 for k in keys
                     if len(q := self._prune(k, now)) >= self.max_hits]
        return max(waits, default=0)

    def hit(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for k in keys:
                self._prune(k, now).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._hits.pop(k, None)

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


login_failures = Limiter(max_hits=5, window_s=600)  # неудачные входы по паре email + IP за 10 минут
ip_login_failures = Limiter(max_hits=30, window_s=600)  # и всего с одного IP — перебор разных email
registrations = Limiter(max_hits=10, window_s=3600)  # новые аккаунты с одного IP за час


def _pair(ip: str | None, email: str) -> str:
    return f"{ip}|{email.strip().lower()}"


def login_blocked(ip: str | None, email: str) -> int:
    return max(login_failures.retry_after(_pair(ip, email)), ip_login_failures.retry_after(str(ip)))


def login_failed(ip: str | None, email: str) -> None:
    login_failures.hit(_pair(ip, email))
    ip_login_failures.hit(str(ip))


def login_succeeded(ip: str | None, email: str) -> None:
    login_failures.reset(_pair(ip, email))


def wait_text(seconds: int) -> str:
    minutes = max(1, (seconds + 59) // 60)
    return f"Слишком много попыток. Попробуйте через {minutes} мин."


def clear_all() -> None:
    for lim in (login_failures, ip_login_failures, registrations):
        lim.clear()
