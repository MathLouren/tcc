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
from .lexico import IndiceLexical

logger = logging.getLogger(__name__)

ARQ_VETORES = "vetores.npy"
ARQ_BLOCOS = "blocos.json"
ARQ_INFO = "info.json"

ContarTokens = Callable[[str], int]

_RE_FRASE = re.compile(r"(?<=[.!?;])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ0-9\"“(])")


# "... legislação vigente. § 2º A frequência ..." ou "... recinto. Artigo 126 A avaliação ..."
_RE_INICIO_DE_NORMA = re.compile(r"(?<=[.;:\"”])\s+(?=(?:§\s*\d+\s*[ºo°]|Art(?:igo|\.)\s*\d+)\b)")
# Unidade que abre um parágrafo ou artigo, com ou sem marcador de lista na frente ("- § 2º", "4. § 3º").
_RE_ABRE_NORMA = re.compile(r"^\s*(?:[-*]|\d+\.)?\s*[\"“]?(?:§\s*\d+\s*[ºo°]|Art(?:igo|\.)\s*\d+)\b")
_PARCELA_MINIMA_ANTES_DE_NORMA = 0.3
# Um bloco pode passar um pouco do limite para terminar junto com o § ou artigo, em vez de deixar
# duas linhas da regra no bloco seguinte, misturadas com a regra de outro escopo.
_FOLGA_ATE_O_FIM_DA_NORMA = 1.3
_RE_SO_MARCADOR = re.compile(r"^[-*]?\s*\(?(?:[a-zA-Z]|\d{1,2}|[ivxIVX]{1,5})[.)]$")
_RE_ARTIGO = re.compile(r"\bArtigo\s+\d+\b")


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
    pendente = ""
    for paragrafo in texto.split("\n"):
        paragrafo = paragrafo.strip()
        if not paragrafo:
            continue
        # Parágrafos (§) e artigos escritos na mesma linha viram unidades próprias: são os limites naturais dos blocos.
        frases = [f for parte in _RE_INICIO_DE_NORMA.split(paragrafo) for f in _RE_FRASE.split(parte)]
        for frase in frases:
            frase = frase.strip()
            if not frase:
                continue
            if pendente:
                frase, pendente = f"{pendente} {frase}", ""
            if _RE_SO_MARCADOR.match(frase):
                pendente = frase  # "a." sozinho: junta ao texto do item, que vem na frase seguinte
                continue
            if contar(frase) <= max_tokens:
                unidades.append(frase)
            else:
                unidades.extend(_quebrar_frase(frase, max_tokens, contar))
    if pendente:
        unidades.append(pendente)
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


# Parágrafo de regimento que define a quem as regras seguintes se aplicam, por exemplo:
# "§ 2º Nos cursos de Graduação na modalidade de Educação a Distância serão observados os critérios:".
_RE_ESCOPO = re.compile(r"§\s*\d+\s*[ºo°]?\s+(?:Nos?|Nas?|Para)\s[^:;.§]{5,200}[:.]")
_RE_INICIO_DE_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)?\s*")


def contextos_dos_blocos(partes: list[str]) -> list[str]:
    """Para cada bloco, o parágrafo de escopo em vigor quando ele começa no meio desse parágrafo.

    Sem isso, um bloco com "Média Parcial igual ou superior a 6,0 aprova" chega ao modelo sem a
    informação de que a regra vale para o EaD, que ficou no bloco anterior. O contexto é guardado à
    parte: entra no prompt, mas não no embedding, para não alterar a busca nem os cortes dos blocos.
    """
    contextos: list[str] = []
    escopo = ""
    for parte in partes:
        comeca_com_escopo = bool(_RE_ESCOPO.match(_RE_INICIO_DE_ITEM.sub("", parte, count=1)))
        abre_norma = bool(_RE_ABRE_NORMA.match(parte))  # bloco que já começa em um § ou artigo não herda nada
        contextos.append("" if comeca_com_escopo or abre_norma or escopo in parte else escopo)
        # Vale o último marco do bloco: um § de escopo define o contexto; um novo artigo o encerra.
        marcos = [(m.start(), " ".join(m.group(0).split()).rstrip(":.")) for m in _RE_ESCOPO.finditer(parte)]
        marcos += [(m.start(), "") for m in _RE_ARTIGO.finditer(parte)]
        if marcos:
            escopo = max(marcos)[1]
    return contextos


def contextos_de_tabela(texto_da_secao: str, partes: list[str]) -> list[str]:
    """Para cada bloco que começa no meio de uma tabela, a frase que apresenta a tabela e a linha de colunas.

    Uma tabela longa é cortada em vários blocos. Sem isso, a linha "Direito | São Gonçalo | 800 | 5 anos"
    chega ao modelo sem dizer que 800 são vagas anuais nem que a tabela é a dos cursos presenciais.
    """
    contexto_da_linha: dict[str, str] = {}
    legenda = cabecalho = ""
    anterior_era_tabela = False
    for linha in texto_da_secao.split("\n"):
        linha = linha.strip()
        if not linha:
            continue
        if " | " not in linha:
            legenda, anterior_era_tabela = linha, False
            continue
        primeira_celula = linha.split(" | ", 1)[0]
        if not anterior_era_tabela or (cabecalho and primeira_celula == cabecalho.split(" | ", 1)[0]):
            if anterior_era_tabela and legenda:  # cabeçalho repetido no meio da tabela: o bloco pode começar aqui
                contexto_da_linha.setdefault(linha, legenda)
            cabecalho = linha
        else:
            contexto_da_linha.setdefault(linha, f"{legenda}\nColunas: {cabecalho}" if legenda else f"Colunas: {cabecalho}")
        anterior_era_tabela = True

    contextos: list[str] = []
    for parte in partes:
        primeira = parte.split("\n", 1)[0].strip()
        contexto = ""
        if " | " in primeira:
            contexto = contexto_da_linha.get(primeira, "")
            if not contexto:  # o bloco começa no meio de uma linha longa da tabela
                contexto = next((c for linha, c in contexto_da_linha.items() if primeira in linha), "")
        contextos.append(contexto)
    return contextos


def dividir_em_blocos(texto: str, max_tokens: int, sobreposicao: int, contar: ContarTokens) -> list[str]:
    """Agrupa frases em blocos de até max_tokens, com sobreposição entre blocos consecutivos."""
    max_tokens = max(1, int(max_tokens))
    sobreposicao = max(0, min(int(sobreposicao), max_tokens // 2))
    blocos: list[str] = []
    atual: list[str] = []
    tamanho = 0
    unidades = _unidades(texto, max_tokens, contar)
    tamanhos = [contar(u) for u in unidades]
    # Para cada unidade, quantos tokens faltam até o próximo § ou artigo (ou o fim da seção).
    ate_a_norma = [0] * len(unidades)
    acumulado = 0
    for i in range(len(unidades) - 1, -1, -1):
        acumulado += tamanhos[i]
        ate_a_norma[i] = acumulado
        if _RE_ABRE_NORMA.match(unidades[i]):
            acumulado = 0
    for i, unidade in enumerate(unidades):
        n = tamanhos[i]
        if atual and _RE_ABRE_NORMA.match(unidade) and tamanho >= _PARCELA_MINIMA_ANTES_DE_NORMA * max_tokens:
            # Novo § ou artigo: fecha o bloco aqui, sem sobreposição, para não misturar regras de escopos diferentes.
            blocos.append("\n".join(atual))
            atual, tamanho = [], 0
        cabe_ate_a_norma = tamanho + ate_a_norma[i] <= _FOLGA_ATE_O_FIM_DA_NORMA * max_tokens
        if atual and tamanho + n > max_tokens and not cabe_ate_a_norma:
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
    tabela = f"{bloco['tabela']}\n" if bloco.get("tabela") else ""
    return f"{bloco['documento']} – {bloco['secao']}\n{tabela}{bloco['texto']}"


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
            partes = dividir_em_blocos(secao.texto, orcamento, config.chunk_sobreposicao, contar)
            tabelas = contextos_de_tabela(secao.texto, partes)
            for parte, contexto, tabela in zip(partes, contextos_dos_blocos(partes), tabelas):
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
                    "contexto": contexto,
                    "tabela": tabela,
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
        # Busca lexical (BM25) sobre os mesmos textos: o motor RAG combina as duas (busca híbrida).
        self.lexico = IndiceLexical([texto_para_embedding(b) for b in blocos])
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
    exportar_blocos(pasta, indice.blocos)


def exportar_blocos(pasta_do_indice: Path, blocos: list[dict]) -> Path:
    """Grava os blocos de cada documento em indice/blocos/<documento>.jsonl (uma linha JSON por bloco).

    É o formato pronto para IA: texto do bloco com documento, seção, contexto e metadados. Serve para
    auditar o que o chatbot realmente consulta e para reaproveitar a base em outras ferramentas.
    """
    pasta = Path(pasta_do_indice) / "blocos"
    pasta.mkdir(parents=True, exist_ok=True)
    por_arquivo: dict[str, list[dict]] = {}
    for bloco in blocos:
        por_arquivo.setdefault(bloco["arquivo"], []).append(bloco)
    esperados = set()
    for arquivo, itens in por_arquivo.items():
        destino = pasta / f"{Path(arquivo).stem}.jsonl"
        esperados.add(destino.name)
        destino.write_text(
            "".join(json.dumps(b, ensure_ascii=False) + "\n" for b in itens), encoding="utf-8"
        )
    for antigo in pasta.glob("*.jsonl"):
        if antigo.name not in esperados:
            antigo.unlink(missing_ok=True)
    return pasta


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
