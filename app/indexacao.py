"""Segmentação em blocos, geração de embeddings e índice vetorial (seção 3.5 do TCC).

Uso direto (reindexação manual):
    python -m app.indexacao
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from .config import Config
from .documentos import EXTENSOES_SUPORTADAS, extrair_secoes

logger = logging.getLogger(__name__)

ARQ_VETORES = "vetores.npy"
ARQ_BLOCOS = "blocos.json"
ARQ_INFO = "info.json"

ContarTokens = Callable[[str], int]

_RE_FRASE = re.compile(r"(?<=[.!?;])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9\"“(])")


# ---------------------------------------------------------------- segmentação
def _quebrar_frase(frase: str, max_tokens: int, contar: ContarTokens) -> list[str]:
    """Divide uma frase maior que o bloco em pedaços que caibam no limite."""
    palavras = frase.split()
    partes: list[str] = []
    inicio = 0
    while inicio < len(palavras):
        fim = min(len(palavras), inicio + max_tokens)
        while fim > inicio + 1 and contar(" ".join(palavras[inicio:fim])) > max_tokens:
            fim = max(inicio + 1, inicio + int((fim - inicio) * 0.9))
        partes.append(" ".join(palavras[inicio:fim]))
        inicio = fim
    return partes


def _unidades(texto: str, max_tokens: int, contar: ContarTokens) -> list[str]:
    """Frases (ou linhas) do texto, cada uma com no máximo max_tokens."""
    unidades: list[str] = []
    for paragrafo in texto.split("\n"):
        paragrafo = paragrafo.strip()
        if not paragrafo:
            continue
        for frase in _RE_FRASE.split(paragrafo):
            frase = frase.strip()
            if not frase:
                continue
            if contar(frase) <= max_tokens:
                unidades.append(frase)
            else:
                unidades.extend(_quebrar_frase(frase, max_tokens, contar))
    return unidades


def _cauda(unidades: list[str], sobreposicao: int, contar: ContarTokens) -> tuple[list[str], int]:
    """Últimas unidades do bloco anterior que cabem na sobreposição."""
    if sobreposicao <= 0 or not unidades:
        return [], 0
    cauda: list[str] = []
    total = 0
    for unidade in reversed(unidades):
        n = contar(unidade)
        if total + n > sobreposicao:
            break
        cauda.insert(0, unidade)
        total += n
    if not cauda:  # a última frase é maior que a sobreposição: usa só o final dela
        palavras = unidades[-1].split()
        k = min(len(palavras), sobreposicao)
        while k > 0 and contar(" ".join(palavras[-k:])) > sobreposicao:
            k -= 1
        if k:
            trecho = " ".join(palavras[-k:])
            cauda, total = [trecho], contar(trecho)
    return cauda, total


def dividir_em_blocos(texto: str, max_tokens: int, sobreposicao: int, contar: ContarTokens) -> list[str]:
    """Agrupa frases em blocos de até max_tokens, com sobreposição entre blocos consecutivos."""
    max_tokens = max(1, int(max_tokens))
    sobreposicao = max(0, min(int(sobreposicao), max_tokens // 2))
    blocos: list[str] = []
    atual: list[str] = []
    tamanho = 0
    for unidade in _unidades(texto, max_tokens, contar):
        n = contar(unidade)
        if atual and tamanho + n > max_tokens:
            blocos.append("\n".join(atual))
            atual, tamanho = _cauda(atual, sobreposicao, contar)
            if atual and tamanho + n > max_tokens:
                atual, tamanho = [], 0
        atual.append(unidade)
        tamanho += n
    if atual:
        blocos.append("\n".join(atual))
    return blocos


# ---------------------------------------------------------------- manifesto e blocos
def carregar_manifesto(caminho: Path) -> dict[str, dict]:
    """Lê data/manifesto.json e devolve os metadados indexados pelo nome do arquivo."""
    if not caminho.exists():
        return {}
    dados = json.loads(caminho.read_text(encoding="utf-8"))
    itens = dados.get("documentos", []) if isinstance(dados, dict) else dados
    return {item["arquivo"]: item for item in itens if isinstance(item, dict) and "arquivo" in item}


def texto_para_embedding(bloco: dict) -> str:
    """O cabeçalho (documento e seção) é incluído no embedding para dar contexto ao bloco."""
    return f"{bloco['documento']} – {bloco['secao']}\n{bloco['texto']}"


def gerar_blocos(config: Config, contar: ContarTokens) -> tuple[list[dict], list[dict]]:
    manifesto = carregar_manifesto(config.manifesto)
    config.docs_dir.mkdir(parents=True, exist_ok=True)
    arquivos = sorted(
        p for p in config.docs_dir.iterdir() if p.is_file() and p.suffix.lower() in EXTENSOES_SUPORTADAS
    )
    blocos: list[dict] = []
    documentos: list[dict] = []
    for caminho in arquivos:
        meta = manifesto.get(caminho.name, {})
        if meta.get("indexar") is False:
            continue
        titulo = meta.get("titulo") or caminho.stem.replace("_", " ")
        try:
            secoes = extrair_secoes(caminho)
        except Exception:
            logger.exception("Falha ao extrair %s; documento ignorado.", caminho.name)
            continue
        antes = len(blocos)
        for secao in secoes:
            cabecalho = f"{titulo} – {secao.titulo}"
            orcamento = max(50, config.chunk_tokens - contar(cabecalho))
            for parte in dividir_em_blocos(secao.texto, orcamento, config.chunk_sobreposicao, contar):
                blocos.append({
                    "id": len(blocos),
                    "documento": titulo,
                    "arquivo": caminho.name,
                    "secao": secao.titulo,
                    "pagina": secao.pagina,
                    "url": meta.get("url", ""),
                    "atualizado_em": meta.get("atualizado_em", ""),
                    "categoria": meta.get("categoria", ""),
                    "texto": parte,
                })
        documentos.append({
            "arquivo": caminho.name,
            "titulo": titulo,
            "url": meta.get("url", ""),
            "atualizado_em": meta.get("atualizado_em", ""),
            "categoria": meta.get("categoria", ""),
            "blocos": len(blocos) - antes,
        })
        logger.info("%s: %d blocos", caminho.name, len(blocos) - antes)
    return blocos, documentos


# ---------------------------------------------------------------- índice vetorial
class IndiceVetorial:
    """Busca exata por produto interno (FAISS IndexFlatIP), com alternativa equivalente em NumPy."""

    def __init__(self, vetores: np.ndarray, blocos: list[dict]):
        self.vetores = np.ascontiguousarray(vetores, dtype="float32")
        self.blocos = blocos
        self._faiss = None
        if blocos:
            try:
                import faiss

                self._faiss = faiss.IndexFlatIP(self.vetores.shape[1])
                self._faiss.add(self.vetores)
            except ImportError:
                logger.warning("faiss-cpu não instalado: usando busca equivalente em NumPy.")
                self._faiss = None

    @property
    def usa_faiss(self) -> bool:
        return self._faiss is not None

    def buscar(self, vetor: np.ndarray, k: int) -> list[tuple[int, float]]:
        """Retorna até k pares (posição do bloco, similaridade de cosseno), do mais ao menos similar."""
        if not self.blocos or k <= 0:
            return []
        k = min(int(k), len(self.blocos))
        consulta = np.ascontiguousarray(np.asarray(vetor, dtype="float32").reshape(1, -1))
        if self._faiss is not None:
            similaridades, posicoes = self._faiss.search(consulta, k)
            return [(int(i), float(s)) for i, s in zip(posicoes[0], similaridades[0]) if i >= 0]
        similaridades = self.vetores @ consulta[0]
        ordem = np.argsort(-similaridades, kind="stable")[:k]
        return [(int(i), float(similaridades[i])) for i in ordem]


def construir_indice(config: Config, embedder) -> tuple[IndiceVetorial, dict]:
    inicio = time.perf_counter()
    blocos, documentos = gerar_blocos(config, embedder.contar_tokens)
    if blocos:
        vetores = embedder.embed_documentos([texto_para_embedding(b) for b in blocos])
    else:
        logger.warning("Nenhum documento encontrado em %s.", config.docs_dir)
        vetores = np.zeros((0, 0), dtype="float32")
    info = {
        "data_indexacao": datetime.now().isoformat(timespec="seconds"),
        "embedding_provedor": config.embedding_provedor,
        "embedding_modelo": config.embedding_modelo,
        "chunk_tokens": config.chunk_tokens,
        "chunk_sobreposicao": config.chunk_sobreposicao,
        "n_documentos": len(documentos),
        "n_blocos": len(blocos),
        "dimensao": int(vetores.shape[1]) if vetores.ndim == 2 else 0,
        "tempo_indexacao_s": round(time.perf_counter() - inicio, 2),
        "documentos": documentos,
    }
    indice = IndiceVetorial(vetores, blocos)
    salvar_indice(config.indice_dir, indice, info)
    logger.info("Índice criado: %d documentos, %d blocos.", len(documentos), len(blocos))
    return indice, info


def salvar_indice(pasta: Path, indice: IndiceVetorial, info: dict) -> None:
    pasta.mkdir(parents=True, exist_ok=True)
    np.save(pasta / ARQ_VETORES, indice.vetores)
    (pasta / ARQ_BLOCOS).write_text(json.dumps(indice.blocos, ensure_ascii=False), encoding="utf-8")
    (pasta / ARQ_INFO).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


def carregar_indice(config: Config) -> tuple[IndiceVetorial, dict] | None:
    """Carrega o índice salvo; retorna None se não existir ou se foi gerado com outro modelo."""
    pasta = config.indice_dir
    if not all((pasta / nome).exists() for nome in (ARQ_VETORES, ARQ_BLOCOS, ARQ_INFO)):
        return None
    info = json.loads((pasta / ARQ_INFO).read_text(encoding="utf-8"))
    mesmo_modelo = (
        info.get("embedding_provedor") == config.embedding_provedor
        and info.get("embedding_modelo") == config.embedding_modelo
    )
    if not mesmo_modelo:
        logger.warning("Índice gerado com outro modelo de embeddings; será reconstruído.")
        return None
    vetores = np.load(pasta / ARQ_VETORES)
    blocos = json.loads((pasta / ARQ_BLOCOS).read_text(encoding="utf-8"))
    return IndiceVetorial(vetores, blocos), info


if __name__ == "__main__":
    from .config import carregar_config
    from .embeddings import criar_embedder

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    configuracao = carregar_config()
    _, dados = construir_indice(configuracao, criar_embedder(configuracao))
    print(f"Indexação concluída: {dados['n_documentos']} documentos, {dados['n_blocos']} blocos.")
