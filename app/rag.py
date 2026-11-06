"""Motor RAG: recuperação, filtro de relevância, montagem do prompt e geração (seção 3.6 do TCC)."""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import date

import numpy as np

from .config import Parametros
from .llm import LLMIndisponivel

logger = logging.getLogger(__name__)

MENSAGEM_NAO_ENCONTRADA = (
    "Não localizei essa informação nos documentos oficiais da UNIVERSO disponíveis para consulta. "
    "Para dúvidas individuais ou não previstas nos documentos, acesse o Espaço do Aluno ou fale com a "
    "central de atendimento pelo 0800 721 0251 ou pelo e-mail contato@universo.com.br."
)
INTRO_EXTRATIVA = "Encontrei estas informações nos documentos oficiais da UNIVERSO:"
AVISO_EXTRATIVO = (
    "\n\n(Resposta em modo extrativo: o modelo de linguagem está indisponível no momento. "
    "Confira as fontes indicadas abaixo.)"
)

_RE_DIVISAO_FRASES = re.compile(r"(?<=[.!?])\s+|\n+")


def _pergunta_pede_lista_de_conteudos(texto: str) -> bool:
    normalizado = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    pede_lista = re.search(r"\b(quais|liste|lista)\b", normalizado)
    pede_conteudo = re.search(r"\b(materias?|disciplinas?|conteudos?|topicos?|programa|ementa)\b", normalizado)
    return bool(pede_lista and pede_conteudo)


@dataclass
class Resposta:
    resposta: str
    fontes: list[dict]
    modo: str  # "llm", "extrativo" ou "sem_resultado"
    trechos: list[dict]
    tempo_ms: int


def montar_fontes(trechos: list[dict]) -> list[dict]:
    """Lista de fontes sem repetição (documento + seção), ordenada pela similaridade."""
    fontes: dict[tuple[str, str], dict] = {}
    for t in trechos:
        chave = (t["documento"], t["secao"])
        if chave not in fontes or t["similaridade"] > fontes[chave]["similaridade"]:
            fontes[chave] = {
                "documento": t["documento"],
                "secao": t["secao"],
                "url": t.get("url", ""),
                "atualizado_em": t.get("atualizado_em", ""),
                "similaridade": t["similaridade"],
            }
    return sorted(fontes.values(), key=lambda f: -f["similaridade"])


class MotorRAG:
    MAX_SESSOES = 1000
    MAX_TROCAS_GUARDADAS = 10

    def __init__(self, indice, embedder, llm, parametros: Parametros):
        self.indice = indice
        self.embedder = embedder
        self.llm = llm
        self.parametros = parametros
        self._sessoes: OrderedDict[str, deque] = OrderedDict()
        self._lock = threading.Lock()

    # ------------------------------------------------------------ recuperação
    def recuperar(self, consulta: str) -> list[dict]:
        """Busca os top-k blocos e descarta os que ficam abaixo do limiar de similaridade."""
        indice, p = self.indice, self.parametros
        consultas = [consulta]
        busca_lista = _pergunta_pede_lista_de_conteudos(consulta)
        if busca_lista:
            consultas.append(f"{consulta} programa da disciplina conteúdos tópicos")

        resultados_por_id: dict[int, float] = {}
        limite_busca = max(p.top_k, p.top_k * 2) if busca_lista else p.top_k
        for texto_busca in consultas:
            vetor = self.embedder.embed_consulta(texto_busca)
            for posicao, similaridade in indice.buscar(vetor, limite_busca):
                resultados_por_id[posicao] = max(resultados_por_id.get(posicao, -1.0), similaridade)

        trechos = [
            dict(indice.blocos[i], similaridade=round(s, 4))
            for i, s in sorted(resultados_por_id.items(), key=lambda item: -item[1])
            if s >= p.limiar
        ]
        if not busca_lista:
            return trechos

        grupos: dict[tuple[str, str], list[dict]] = {}
        for bloco in indice.blocos:
            secao = str(bloco.get("secao", ""))
            secao_normalizada = unicodedata.normalize("NFKD", secao).encode("ascii", "ignore").decode().lower()
            if "programa" not in secao_normalizada and "ementa" not in secao_normalizada:
                continue
            chave = (str(bloco.get("arquivo", "")), secao)
            grupos.setdefault(chave, []).append(bloco)

        if not grupos:
            return trechos

        def pontuacao_grupo(blocos: list[dict]) -> tuple[float, int]:
            pontuacoes = [resultados_por_id.get(int(bloco["id"]), -1.0) for bloco in blocos]
            secao = unicodedata.normalize("NFKD", str(blocos[0].get("secao", ""))).encode("ascii", "ignore").decode().lower()
            return max(pontuacoes), int("programa" in secao)

        grupo = max(grupos.values(), key=pontuacao_grupo)
        score_grupo = max(pontuacao_grupo(grupo)[0], p.limiar)
        return [
            dict(bloco, similaridade=round(resultados_por_id.get(int(bloco["id"]), score_grupo), 4))
            for bloco in sorted(grupo, key=lambda item: int(item["id"]))
        ]

    # ------------------------------------------------------------ histórico da conversa (RF06)
    def historico(self, sessao_id: str | None) -> list[tuple[str, str]]:
        if not sessao_id:
            return []
        with self._lock:
            return list(self._sessoes.get(sessao_id, ()))

    def _guardar(self, sessao_id: str | None, pergunta: str, resposta: str) -> None:
        if not sessao_id:
            return
        with self._lock:
            fila = self._sessoes.get(sessao_id)
            if fila is None:
                fila = deque(maxlen=self.MAX_TROCAS_GUARDADAS)
                self._sessoes[sessao_id] = fila
            self._sessoes.move_to_end(sessao_id)
            fila.append((pergunta, resposta))
            while len(self._sessoes) > self.MAX_SESSOES:
                self._sessoes.popitem(last=False)

    # ------------------------------------------------------------ prompt (Quadro 2 do TCC)
    def montar_mensagens(self, pergunta: str, trechos: list[dict], historico: list[tuple[str, str]]) -> list[dict]:
        partes = []
        for n, t in enumerate(trechos, start=1):
            atualizado = f", atualizado em {t['atualizado_em']}" if t.get("atualizado_em") else ""
            partes.append(f"[Trecho {n}] Fonte: {t['documento']} – {t['secao']}{atualizado}\n{t['texto']}")
        sistema = (
            f"{self.parametros.prompt_sistema}\n\n"
            f"Data de hoje: {date.today():%d/%m/%Y}.\n\n"
            "Trechos dos documentos oficiais:\n\n" + "\n\n".join(partes)
        )
        mensagens = [{"role": "system", "content": sistema}]
        for pergunta_anterior, resposta_anterior in historico:
            mensagens.append({"role": "user", "content": pergunta_anterior})
            mensagens.append({"role": "assistant", "content": resposta_anterior})
        mensagens.append({"role": "user", "content": pergunta})
        return mensagens

    # ------------------------------------------------------------ modo extrativo (sem LLM)
    def resposta_extrativa(self, pergunta: str, trechos: list[dict]) -> str:
        """Seleciona as frases dos trechos mais próximas da pergunta quando o LLM está indisponível."""
        candidatas: dict[str, str] = {}
        for t in trechos[:2]:
            for frase in _RE_DIVISAO_FRASES.split(t["texto"]):
                frase = frase.strip()
                if len(frase) >= 15 and frase not in candidatas:
                    candidatas[frase] = t["secao"]
        if not candidatas:
            return f"{INTRO_EXTRATIVA}\n\n{trechos[0]['texto'][:600]}{AVISO_EXTRATIVO}"
        frases = list(candidatas)
        vetores = self.embedder.embed_documentos(frases)
        notas = vetores @ self.embedder.embed_consulta(pergunta)
        escolhidas = sorted(int(i) for i in np.argsort(-notas)[:3])
        linhas = [f"• {candidatas[frases[i]]}: {frases[i]}" for i in escolhidas]
        return f"{INTRO_EXTRATIVA}\n\n" + "\n".join(linhas) + AVISO_EXTRATIVO

    # ------------------------------------------------------------ fluxo completo (Figura 4 do TCC)
    def responder(self, pergunta: str, sessao_id: str | None = None) -> Resposta:
        inicio = time.perf_counter()
        p = self.parametros
        historico = self.historico(sessao_id)
        historico = historico[-p.historico_trocas:] if p.historico_trocas > 0 else []

        trechos = self.recuperar(pergunta)
        if not trechos and historico:
            # pergunta de acompanhamento ("e para o outro semestre?"): usa a pergunta anterior como contexto
            trechos = self.recuperar(f"{historico[-1][0]} {pergunta}")

        if not trechos:
            texto, modo = MENSAGEM_NAO_ENCONTRADA, "sem_resultado"
        else:
            try:
                texto = self.llm.gerar(self.montar_mensagens(pergunta, trechos, historico), p.temperatura)
                modo = "llm"
            except LLMIndisponivel as erro:
                logger.warning("LLM indisponível, usando modo extrativo: %s", erro)
                texto, modo = self.resposta_extrativa(pergunta, trechos), "extrativo"

        self._guardar(sessao_id, pergunta, texto)
        return Resposta(
            resposta=texto,
            fontes=montar_fontes(trechos),
            modo=modo,
            trechos=[
                {k: t.get(k) for k in ("id", "documento", "arquivo", "secao", "similaridade")}
                for t in trechos
            ],
            tempo_ms=int((time.perf_counter() - inicio) * 1000),
        )
