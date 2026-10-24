"""Serviço que reúne índice, embeddings, LLM, motor RAG e registros.

Na inicialização, carrega o índice salvo em indice/; se ele não existir ou tiver sido
gerado com outro modelo de embeddings, indexa os documentos automaticamente.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import replace

from .config import Config, Parametros, carregar_config
from .embeddings import criar_embedder
from .indexacao import carregar_indice, construir_indice
from .llm import criar_cliente_llm
from .rag import MotorRAG
from .registros import Registro

logger = logging.getLogger(__name__)


class Servico:
    def __init__(self, config: Config | None = None, embedder=None, llm=None):
        self.config = config or carregar_config()
        self.parametros = Parametros.carregar(self.config.parametros_arquivo)
        self.embedder = embedder if embedder is not None else criar_embedder(self.config)
        self.llm = llm if llm is not None else criar_cliente_llm(self.config)
        self.registro = Registro(self.config.registros_dir)
        self._lock = threading.Lock()

        carregado = carregar_indice(self.config)
        if carregado is None:
            logger.info("Índice ausente ou desatualizado: indexando os documentos de %s.", self.config.docs_dir)
            self.indice, self.info = construir_indice(self.config, self.embedder)
        else:
            self.indice, self.info = carregado
        self.motor = MotorRAG(self.indice, self.embedder, self.llm, self.parametros)

    def reindexar(self) -> dict:
        """RF09: reprocessa a base de conhecimento sem reiniciar a aplicação."""
        with self._lock:
            indice, info = construir_indice(self.config, self.embedder)
            self.indice, self.info = indice, info
            self.motor.indice = indice
        return info

    def atualizar_parametros(self, alteracoes: dict) -> Parametros:
        """RF10: ajusta top_k, limiar, temperatura, histórico e prompt; persiste em parametros.json."""
        with self._lock:
            novos = replace(self.parametros, **alteracoes)
            novos.validar()
            novos.salvar(self.config.parametros_arquivo)
            self.parametros = novos
            self.motor.parametros = novos
        return novos
