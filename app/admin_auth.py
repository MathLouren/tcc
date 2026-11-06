"""Sessão administrativa assinada, sem banco de dados ou segredo no navegador."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from threading import Lock

COOKIE_NAME = "universo_admin_session"
SESSION_SECONDS = 30 * 24 * 60 * 60


def _codificar(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode("ascii")


class AdminAuth:
    def __init__(self, email: str, password: str):
        self.email = email.strip().lower()
        self.password = password
        self.habilitado = bool(self.email and self.password)
        self._falhas: deque[tuple[str, float]] = deque(maxlen=1000)
        self._falhas_lock = Lock()
        # A troca do e-mail ou da senha invalida todas as sessões anteriores.
        self._chave = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            ("universo-admin-session:" + self.email).encode("utf-8"), 200_000,
        ) if self.habilitado else b""

    def autenticar(self, email: str, password: str) -> bool:
        if not self.habilitado:
            return False
        email_valido = secrets.compare_digest(email.strip().lower().encode(), self.email.encode())
        senha_valida = secrets.compare_digest(password.encode(), self.password.encode())
        return email_valido and senha_valida

    def pode_tentar(self, endereco: str) -> bool:
        agora = time.monotonic()
        with self._falhas_lock:
            while self._falhas and self._falhas[0][1] < agora - 300:
                self._falhas.popleft()
            return sum(1 for ip, _ in self._falhas if ip == endereco) < 10

    def registrar_falha(self, endereco: str) -> None:
        with self._falhas_lock:
            self._falhas.append((endereco, time.monotonic()))

    def criar_sessao(self) -> tuple[str, str]:
        csrf = secrets.token_urlsafe(32)
        corpo = _codificar(json.dumps({
            "exp": int(time.time()) + SESSION_SECONDS,
            "csrf": csrf,
            "nonce": secrets.token_urlsafe(16),
        }, separators=(",", ":")).encode("utf-8"))
        assinatura = _codificar(hmac.new(self._chave, corpo.encode("ascii"), hashlib.sha256).digest())
        return f"{corpo}.{assinatura}", csrf

    def verificar_sessao(self, token: str | None) -> dict | None:
        if not self.habilitado or not token or len(token) > 1024:
            return None
        try:
            corpo, assinatura = token.split(".")
            esperado = _codificar(hmac.new(self._chave, corpo.encode("ascii"), hashlib.sha256).digest())
            if not secrets.compare_digest(assinatura, esperado):
                return None
            payload = json.loads(base64.urlsafe_b64decode(corpo + "=" * (-len(corpo) % 4)))
            if not isinstance(payload, dict) or type(payload.get("exp")) is not int:
                return None
            if payload["exp"] <= time.time() or not isinstance(payload.get("csrf"), str):
                return None
            return payload
        except (ValueError, UnicodeError, TypeError, binascii.Error):
            return None
