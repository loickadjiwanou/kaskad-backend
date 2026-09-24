"""Limitation des tentatives de connexion (anti brute force), en mémoire.

Pour plusieurs instances de l'API derrière un répartiteur de charge, remplacer par un stockage partagé (Redis).
"""

import time
from collections import defaultdict, deque

from fastapi import Request

from app.core.i18n import ApiError


class LoginRateLimiter:
    def __init__(self, max_attempts: int, window_seconds: int, max_per_ip: int | None = None):
        self.max_attempts = max_attempts
        self.max_per_ip = max_per_ip or max_attempts * 5
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        return hits

    @staticmethod
    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def check(self, request: Request, identifier: str) -> None:
        """À appeler avant de vérifier les identifiants : compte une tentative et refuse au-delà du seuil."""
        now = time.monotonic()
        ip = self.client_ip(request)
        per_account = self._prune(f"{ip}|{identifier.lower()}", now)
        per_ip = self._prune(ip, now)
        if len(per_account) >= self.max_attempts or len(per_ip) >= self.max_per_ip:
            retry_after = int(self.window - (now - (per_account[0] if per_account else per_ip[0]))) + 1
            raise ApiError(429, "too_many_attempts", retry_after=retry_after)
        per_account.append(now)
        per_ip.append(now)

    def reset(self, request: Request, identifier: str) -> None:
        """Connexion réussie : les échecs de ce compte sont oubliés et cette tentative ne compte pas pour l'IP
        (une équipe qui se connecte souvent depuis le même réseau ne doit pas se bloquer elle-même)."""
        ip = self.client_ip(request)
        self._hits.pop(f"{ip}|{identifier.lower()}", None)
        if self._hits.get(ip):
            self._hits[ip].pop()
