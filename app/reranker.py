"""Reordenação dos trechos candidatos por um cross-encoder (opcional, RERANKER_MODELO no .env).

O embedding compara a pergunta e o trecho separadamente; o cross-encoder lê os dois juntos e dá uma
nota de relevância mais fina. Fica depois da busca híbrida: ela traz os candidatos, ele escolhe os
top_k que vão ao modelo. Custa algumas centenas de milissegundos em CPU por pergunta.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class Reranker:
    def __init__(self, modelo: str, candidatos: int = 6):
        self.modelo = modelo
        self.candidatos = candidatos
        self._cross = None
        self._lock = threading.Lock()

    def _carregar(self):
        with self._lock:
            if self._cross is None:
                from sentence_transformers import CrossEncoder

                self._cross = CrossEncoder(self.modelo, max_length=512)
                logger.info("Reranker %s carregado.", self.modelo)
        return self._cross

    def pontuar(self, consulta: str, textos: list[str]) -> list[float]:
        if not textos:
            return []
        return [float(n) for n in self._carregar().predict([(consulta, t) for t in textos], batch_size=16)]


def criar_reranker(config) -> Reranker | None:
    modelo = getattr(config, "reranker_modelo", "")
    return Reranker(modelo, getattr(config, "reranker_candidatos", 6)) if modelo else None
