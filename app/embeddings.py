"""Geração de embeddings (representações vetoriais) de blocos e perguntas.

Provedores:
- sentence-transformers (padrão do TCC): modelo intfloat/multilingual-e5-base, 768 dimensões,
  executado localmente na CPU. Os modelos E5 exigem os prefixos "query: " e "passage: ".
- ollama: usa o endpoint /api/embed do Ollama (ex.: bge-m3), sem instalar o PyTorch.

Todos os vetores são normalizados, de modo que o produto interno equivale à similaridade de cosseno.
"""
from __future__ import annotations

import logging
import math
import re

import numpy as np

from .config import Config

logger = logging.getLogger(__name__)


def normalizar_vetores(vetores) -> np.ndarray:
    vetores = np.asarray(vetores, dtype="float32")
    if vetores.ndim == 1:
        vetores = vetores.reshape(1, -1)
    normas = np.linalg.norm(vetores, axis=1, keepdims=True)
    normas[normas == 0] = 1.0
    return vetores / normas


def contar_tokens_aproximado(texto: str) -> int:
    """Estimativa de tokens (palavras e pontuação x 1,3) quando não há tokenizador disponível."""
    return math.ceil(len(re.findall(r"\w+|[^\w\s]", texto)) * 1.3)


class EmbedderSentenceTransformers:
    def __init__(self, modelo: str):
        from sentence_transformers import SentenceTransformer

        logger.info("Carregando modelo de embeddings %s (o primeiro uso faz o download).", modelo)
        self.nome = modelo
        self._modelo = SentenceTransformer(modelo)
        self._usa_prefixos = "e5" in modelo.lower()

    def embed_documentos(self, textos: list[str]) -> np.ndarray:
        if self._usa_prefixos:
            textos = [f"passage: {t}" for t in textos]
        vetores = self._modelo.encode(
            textos, batch_size=16, convert_to_numpy=True, show_progress_bar=len(textos) > 64
        )
        return normalizar_vetores(vetores)

    def embed_consulta(self, texto: str) -> np.ndarray:
        if self._usa_prefixos:
            texto = f"query: {texto}"
        return normalizar_vetores(self._modelo.encode([texto], convert_to_numpy=True))[0]

    def contar_tokens(self, texto: str) -> int:
        try:
            return len(self._modelo.tokenizer(texto, add_special_tokens=False)["input_ids"])
        except Exception:  # pragma: no cover - tokenizador indisponível
            return contar_tokens_aproximado(texto)


class EmbedderOllama:
    def __init__(self, url: str, modelo: str, timeout: float = 120.0, lote: int = 32):
        self.nome = modelo
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.lote = lote

    def _embed(self, textos: list[str]) -> np.ndarray:
        import httpx

        vetores: list[list[float]] = []
        for inicio in range(0, len(textos), self.lote):
            resposta = httpx.post(
                f"{self.url}/api/embed",
                json={"model": self.nome, "input": textos[inicio: inicio + self.lote]},
                timeout=self.timeout,
            )
            resposta.raise_for_status()
            vetores.extend(resposta.json()["embeddings"])
        return normalizar_vetores(vetores)

    def embed_documentos(self, textos: list[str]) -> np.ndarray:
        return self._embed(textos)

    def embed_consulta(self, texto: str) -> np.ndarray:
        return self._embed([texto])[0]

    def contar_tokens(self, texto: str) -> int:
        return contar_tokens_aproximado(texto)


def criar_embedder(config: Config):
    if config.embedding_provedor == "sentence-transformers":
        return EmbedderSentenceTransformers(config.embedding_modelo)
    if config.embedding_provedor == "ollama":
        return EmbedderOllama(config.ollama_url, config.embedding_modelo, timeout=config.llm_timeout)
    raise ValueError(
        f"EMBEDDING_PROVEDOR inválido: {config.embedding_provedor!r}. Use 'sentence-transformers' ou 'ollama'."
    )
