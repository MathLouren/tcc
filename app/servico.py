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
from .indexacao import IndiceQdrant, carregar_indice, construir_indice
from .llm import criar_cliente_llm
from .rag import MotorRAG
from .registros import Registro

logger = logging.getLogger(__name__)


class Servico:
    def __init__(self, config: Config | None = None, embedder=None, llm=None):
        self.config = config or carregar_config()
        self.parametros = Parametros.carregar(self.config.parametros_arquivo)
        if self.parametros.vector_store:
            self.config = replace(self.config, vector_store=self.parametros.vector_store)
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


    def reindexar_documento(self, nome: str) -> dict:
        """Reindexa somente o documento indicado no Qdrant; local mantém rebuild."""
        if self.config.vector_store != "qdrant":
            return self.reindexar()
        from datetime import datetime
        from .indexacao import gerar_blocos, texto_para_embedding, salvar_indice_qdrant
        from .qdrant_incremental import contar_pontos
        caminho = self.config.docs_dir / nome
        if not caminho.is_file():
            raise ValueError("Arquivo não encontrado.")
        with self._lock:
            todos, documentos = gerar_blocos(self.config, self.embedder.contar_tokens)
            blocos = [b for b in todos if b["arquivo"] == nome]
            if not blocos:
                raise ValueError("Documento sem conteúdo indexável.")
            vetores = self.embedder.embed_documentos([texto_para_embedding(b) for b in blocos])
            cliente = salvar_indice_qdrant(self.config, vetores, blocos, remover_legados=True)
            self.info = dict(self.info)
            existentes = {d["arquivo"]: d for d in self.info.get("documentos", [])}
            for d in documentos:
                if d["arquivo"] == nome:
                    existentes[nome] = d
            self.info.update({"documentos": list(existentes.values()),
                              "n_documentos": len(existentes),
                              "n_blocos": contar_pontos(cliente, self.config.qdrant_collection),
                              "data_indexacao": datetime.now().isoformat(timespec="seconds")})
            self.indice = IndiceQdrant(cliente, self.config.qdrant_collection)
            self.motor.indice = self.indice
            self.config.indice_dir.mkdir(parents=True, exist_ok=True)
            (self.config.indice_dir / "info.json").write_text(
                __import__("json").dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8")
            return self.info

    def remover_documento_qdrant(self, nome: str) -> dict:
        """Remoção seletiva ANTES de excluir o arquivo físico."""
        if self.config.vector_store != "qdrant":
            return self.info
        from datetime import datetime
        from .qdrant_incremental import remover_arquivo, contar_pontos
        with self._lock:
            removidos = remover_arquivo(self.indice.cliente, self.config.qdrant_collection, nome)
            self.info = dict(self.info)
            docs = [d for d in self.info.get("documentos", []) if d["arquivo"] != nome]
            self.info.update({"documentos": docs, "n_documentos": len(docs),
                              "n_blocos": contar_pontos(self.indice.cliente, self.config.qdrant_collection),
                              "data_indexacao": datetime.now().isoformat(timespec="seconds")})
            logger.info("Documento %s: %s pontos removidos do Qdrant", nome, removidos)
            self.config.indice_dir.mkdir(parents=True, exist_ok=True)
            (self.config.indice_dir / "info.json").write_text(
                __import__("json").dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8")
            return self.info

    def sincronizar_crawler(self, *, aplicar: bool = False, max_paginas: int = 25) -> dict:
        """Executa coleta explicitamente via painel/API; serializa mudanças."""
        from .crawler import sincronizar
        from .qdrant_incremental import contar_pontos
        from .indexacao import carregar_manifesto
        with self._lock:
            resumo = sincronizar(self.config, self.embedder, aplicar=aplicar, max_paginas=max_paginas)
            if aplicar:
                manifesto = carregar_manifesto(self.config.manifesto)
                self.info = dict(self.info)
                self.info.update({"documentos": [
                    {"arquivo": nome, "titulo": m.get("titulo", nome), "url": m.get("url", ""),
                     "atualizado_em": m.get("atualizado_em", ""), "categoria": m.get("categoria", "")}
                    for nome, m in manifesto.items()],
                    "n_documentos": len(manifesto),
                    "n_blocos": contar_pontos(self.indice.cliente, self.config.qdrant_collection)})
                self.config.indice_dir.mkdir(parents=True, exist_ok=True)
                (self.config.indice_dir / "info.json").write_text(
                    __import__("json").dumps(self.info, ensure_ascii=False, indent=2), encoding="utf-8")
            return resumo

    def atualizar_parametros(self, alteracoes: dict) -> Parametros:
        """RF10: ajusta top_k, limiar, temperatura, histórico e prompt; persiste em parametros.json."""
        with self._lock:
            novos = replace(self.parametros, **alteracoes)
            novos.validar()
            novos.salvar(self.config.parametros_arquivo)
            self.parametros = novos
            if "vector_store" in alteracoes and novos.vector_store:
                self.config = replace(self.config, vector_store=novos.vector_store)
            self.motor.parametros = novos
        return novos

    def alterar_vector_store(self, vector_store: str) -> tuple[Parametros, dict]:
        """Altera o backend vetorial, persiste a escolha e reindexa a base."""
        if vector_store not in {"local", "qdrant"}:
            raise ValueError("vector_store deve ser 'local' ou 'qdrant'.")
        with self._lock:
            novos = replace(self.parametros, vector_store=vector_store)
            novos.validar()
            nova_config = replace(self.config, vector_store=vector_store)
            indice, info = construir_indice(nova_config, self.embedder)
            novos.salvar(nova_config.parametros_arquivo)
            self.config = nova_config
            self.parametros = novos
            self.indice, self.info = indice, info
            self.motor.indice = indice
        return novos, info
