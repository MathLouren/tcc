"""Sincronização não destrutiva de documentos individuais na coleção Qdrant.

Compatível com pontos legados (IDs inteiros); nenhuma função elimina a coleção.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from .identidade import gerar_chunk_id, gerar_document_id, hash_conteudo

logger = logging.getLogger(__name__)


def validar_colecao(cliente, colecao: str, dimensao: int) -> None:
    """Valida ANTES de escrever, impedindo misturar espaços vetoriais distintos por dimensão."""
    if not cliente.collection_exists(colecao):
        return
    info = cliente.get_collection(colecao)
    params = info.config.params.vectors
    if isinstance(params, dict):
        if len(params) != 1:
            raise ValueError("Coleção com vetores nomeados não suportada pelo índice atual.")
        params = next(iter(params.values()))
    tamanho = int(params.size)
    distancia = str(getattr(params.distance, "value", params.distance)).lower()
    if tamanho != dimensao or distancia != "cosine":
        raise ValueError(f"Qdrant incompatível: {tamanho} dimensões, distância {distancia}; esperado {dimensao}/Cosine.")


def listar_pontos_arquivo(cliente, colecao: str, arquivo: str) -> dict:
    from qdrant_client import models

    filtro = models.Filter(must=[models.FieldCondition(key="arquivo", match=models.MatchValue(value=arquivo))])
    encontrados = {}
    offset = None
    while True:
        pontos, offset = cliente.scroll(
            collection_name=colecao, scroll_filter=filtro, limit=128,
            with_payload=True, with_vectors=False, offset=offset,
        )
        for ponto in pontos:
            encontrados[ponto.id] = ponto.payload or {}
        if offset is None:
            break
    return encontrados


def preparar_pontos(blocos: list[dict], vetores: np.ndarray):
    """Vincula cada bloco ao arquivo de origem e gera IDs UUID5 estáveis."""
    from qdrant_client import models

    if len(blocos) != len(vetores):
        raise ValueError("Número de blocos não corresponde ao de embeddings.")
    ordens = defaultdict(int)
    pontos_por_arquivo = defaultdict(list)
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for bloco, vetor in zip(blocos, vetores):
        arquivo = str(bloco.get("arquivo", ""))
        if not arquivo:
            raise ValueError("Todo bloco incremental deve ter o campo arquivo.")
        secao = str(bloco.get("secao", "Geral"))
        chave = (arquivo, secao)
        ordem = int(bloco["chunk_ordem"]) if str(bloco.get("chunk_ordem", "")).isdigit() else ordens[chave]
        ordens[chave] = max(ordens[chave], ordem + 1)
        url_identidade = str(bloco.get("url_canonica") or bloco.get("url", ""))
        doc_id = gerar_document_id(arquivo, url_identidade)
        chunk_id = str(bloco.get("chunk_id") or gerar_chunk_id(doc_id, secao, ordem, str(bloco["texto"])))
        payload = dict(bloco)
        payload.update({
            "id": chunk_id, "chunk_id": chunk_id, "document_id": doc_id,
            "chunk_ordem": ordem, "chunk_hash": hash_conteudo(str(bloco["texto"])),
            "embedding_modelo": bloco.get("embedding_modelo", ""),
            "indexado_em": agora,
        })
        payload.pop("_bloco_id", None)
        pontos_por_arquivo[arquivo].append(models.PointStruct(id=chunk_id, vector=np.asarray(vetor, dtype="float32").tolist(), payload=payload))
    return pontos_por_arquivo


def upsert_incremental(cliente, colecao: str, pontos_por_arquivo: dict, *, validar=True, remover_legados=False) -> dict:
    """Upsert, confirmação de leitura e SOMENTE ENTÃO limpeza de IDs antigos do mesmo arquivo.

    Uma falha no meio deixa dados anteriores intactos, podendo deixar novos pontos extras;
    a repetição converge para os IDs estáveis. ``remover_legados`` só deve ser
    usado após reindexação explícita ou alteração confirmada do documento.
    """
    from qdrant_client import models

    resumo = {"atualizados": 0, "inalterados": 0, "removidos": 0, "pontos": 0}
    for arquivo, pontos in pontos_por_arquivo.items():
        anteriores = listar_pontos_arquivo(cliente, colecao, arquivo)
        novos = {p.id: p for p in pontos}
        estaveis_anteriores = {
            pid: payload for pid, payload in anteriores.items()
            if payload.get("chunk_id") or payload.get("document_id")
        }
        # O conteúdo de documentos legados não dispõe de hash; a primeira sincronização os migra.
        if len(estaveis_anteriores) == len(novos) and all(
            pid in estaveis_anteriores and estaveis_anteriores[pid].get("chunk_hash") == p.payload["chunk_hash"]
            for pid, p in novos.items()
        ):
            resumo["inalterados"] += 1
            continue
        for inicio in range(0, len(pontos), 64):
            cliente.upsert(collection_name=colecao, points=pontos[inicio:inicio + 64], wait=True)
        if validar and pontos:
            confirmados = cliente.retrieve(collection_name=colecao, ids=list(novos), with_payload=True, with_vectors=False)
            conf = {p.id: (p.payload or {}) for p in confirmados}
            if set(conf) != set(novos) or any(
                conf[pid].get("chunk_hash") != p.payload["chunk_hash"] for pid, p in novos.items()
            ):
                raise RuntimeError(f"Falha de verificação após upsert: {arquivo}. Pontos antigos preservados.")
        # IDs inteiros sem chunk_id permanecem intocados durante uma migração
        # genérica. Em uma reindexação explícita do documento, eles podem ser
        # removidos depois da confirmação do novo conjunto.
        obsoletos = [pid for pid in anteriores if pid not in novos and (
            remover_legados or pid in estaveis_anteriores
        )]
        if obsoletos:
            cliente.delete(collection_name=colecao, points_selector=models.PointIdsList(points=obsoletos), wait=True)
        resumo["atualizados"] += 1
        resumo["removidos"] += len(obsoletos)
        resumo["pontos"] += len(pontos)
        logger.info("Qdrant: %s => %s blocos (%s IDs antigos removidos)", arquivo, len(pontos), len(obsoletos))
    return resumo


def remover_arquivo(cliente, colecao: str, arquivo: str) -> int:
    """Remove EXCLUSIVAMENTE os pontos com payload arquivo correspondente."""
    from qdrant_client import models

    existentes = listar_pontos_arquivo(cliente, colecao, arquivo)
    if existentes:
        cliente.delete(collection_name=colecao, points_selector=models.PointIdsList(points=list(existentes)), wait=True)
    return len(existentes)


def contar_pontos(cliente, colecao: str) -> int:
    return int(cliente.count(collection_name=colecao, exact=True).count)
