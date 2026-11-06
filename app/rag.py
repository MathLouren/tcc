"""Motor RAG: recuperação, filtro de relevância, montagem do prompt e geração (seção 3.6 do TCC)."""
from __future__ import annotations

import logging
import hashlib
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
from .validacao import (
    codigo_avaliacao_e_prazo,
    eventos_de_prazo,
    resposta_factual_de_eventos,
    validar_resposta_datas,
)

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
_MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}


def _normalizar(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


def _pergunta_pede_lista_de_conteudos(texto: str) -> bool:
    normalizado = _normalizar(texto)
    pede_lista = re.search(r"\b(quais|liste|lista)\b", normalizado)
    pede_conteudo = re.search(r"\b(materias?|disciplinas?|conteudos?|topicos?|programa|ementa)\b", normalizado)
    return bool(pede_lista and pede_conteudo)


def interpretar_consulta(texto: str) -> dict:
    """Extrai sinais determinísticos da pergunta sem exigir uma única palavra-chave."""
    normalizado = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    codigo_prazo = codigo_avaliacao_e_prazo(texto)
    normativa = bool(re.search(r"segunda chamada|regimento|art\.\s*\d+|norma", normalizado))
    calendario = bool(codigo_prazo) or bool(re.search(
        r"calendario|periodo letivo|semestre|inicio das aulas|termino do semestre|"
        r"encerramento do semestre|ferias|quando.*(prova|avaliacao|aula)|datas?.*(prova|avaliacao|matricula)",
        normalizado,
    ))
    filtros = {}
    if calendario and not normativa:
        filtros["tipo_documento"] = "calendario"
    if re.search(r"\bead\b|a distancia|semipresencial", normalizado):
        filtros["modalidade"] = "ead" if re.search(r"\bead\b|a distancia", normalizado) else "semipresencial"
    periodo = re.search(r"\b(20\d{2})\s*[./-]\s*([12])\b", normalizado)
    if not periodo:
        periodo = re.search(r"\b([12])\s*semestre\s*(?:de\s*)?(20\d{2})\b", normalizado)
        if periodo:
            filtros["periodo"] = f"{periodo.group(2)}.{periodo.group(1)}"
    else:
        filtros["periodo"] = f"{periodo.group(1)}.{periodo.group(2)}"
    if re.search(r"sao goncalo", normalizado):
        filtros["campus"] = "sao_goncalo"
    exige_multiplas = calendario or _pergunta_pede_lista_de_conteudos(texto) or bool(re.search(
        r"inicio.*termino|comeco.*fim|principais datas|todas as datas|ao longo|meses?", normalizado
    ))
    return {
        "filtros": filtros,
        "calendario": calendario,
        "codigo_prazo": codigo_prazo,
        "mistura_norma": calendario and normativa,
        "multiplas_secoes": exige_multiplas,
        "lista": _pergunta_pede_lista_de_conteudos(texto),
    }


def _chave_bloco(bloco: dict) -> str:
    bruto = "|".join(str(bloco.get(campo, "")) for campo in ("arquivo", "secao", "texto"))
    if bloco.get("texto"):
        return "conteudo:" + hashlib.sha256(bruto.encode("utf-8")).hexdigest()
    if bloco.get("chunk_id"):
        return f"chunk:{bloco['chunk_id']}"
    return "legado:" + hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def _lexical_score(consulta: str, bloco: dict) -> float:
    """Reforça termos exatos, datas, artigos e códigos sem substituir o vetor."""
    tokens_consulta = set(re.findall(r"[\wÀ-ÿ]+", consulta.lower()))
    tokens_texto = set(re.findall(r"[\wÀ-ÿ]+", str(bloco.get("texto", "")).lower()))
    if not tokens_consulta or not tokens_texto:
        return 0.0
    return len(tokens_consulta & tokens_texto) / len(tokens_consulta)


def _ordem_bloco(bloco: dict) -> int:
    for campo in ("chunk_ordem", "_bloco_id", "id"):
        valor = bloco.get(campo)
        if isinstance(valor, int) or (isinstance(valor, str) and valor.isdigit()):
            return int(valor)
    return 0


def _enriquecer_metadados_legados(bloco: dict) -> dict:
    """Recupera sinais óbvios de payloads antigos sem inventar campus ou curso."""
    bloco = dict(bloco)
    evidencia = " ".join(str(bloco.get(campo, "")) for campo in ("arquivo", "documento", "url"))
    normalizado = unicodedata.normalize("NFKD", evidencia).encode("ascii", "ignore").decode().lower()
    if not bloco.get("tipo_documento") and re.search(r"calendario", normalizado):
        bloco["tipo_documento"] = "calendario"
    if not bloco.get("modalidade") and re.search(r"\bead\b|a distancia", normalizado):
        bloco["modalidade"] = "ead"
    if not bloco.get("periodo"):
        periodo = re.search(r"\b(20\d{2})[_./-]([12])\b", normalizado)
        if periodo:
            bloco["periodo"] = f"{periodo.group(1)}.{periodo.group(2)}"
    if not bloco.get("ano"):
        periodo = re.search(r"\b(20\d{2})\b", str(bloco.get("periodo", "")))
        if periodo:
            bloco["ano"] = int(periodo.group(1))
    if not bloco.get("mes"):
        secao = _normalizar(str(bloco.get("secao", "")))
        for nome, numero in _MESES.items():
            if re.search(rf"\b{nome}\b", secao):
                bloco["mes"] = numero
                break
    return bloco


@dataclass
class Resposta:
    resposta: str
    fontes: list[dict]
    modo: str  # "llm", "validado", "extrativo" ou "sem_resultado"
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
                "pagina": t.get("pagina"),
                "url": t.get("url", ""),
                "atualizado_em": t.get("atualizado_em", ""),
                "campus": t.get("campus", ""),
                "tipo_documento": t.get("tipo_documento", ""),
                "modalidade": t.get("modalidade", ""),
                "periodo": t.get("periodo", ""),
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
        """Recupera candidatos, aplica filtros/lexical e expande o documento quando necessário."""
        indice, p = self.indice, self.parametros
        intencao = interpretar_consulta(consulta)
        busca_lista = intencao["lista"]
        consultas = [consulta]
        if intencao["codigo_prazo"]:
            consultas.append(f"avaliação {intencao['codigo_prazo']} fechamento do período de realização prazo final")
        elif intencao["calendario"]:
            consultas.append(f"{consulta} calendário acadêmico início das aulas término do semestre avaliações provas")
            if intencao["mistura_norma"]:
                consultas.append(f"{consulta} regra regimental artigo segunda chamada procedimento")
        elif busca_lista:
            consultas.append(f"{consulta} programa da disciplina conteúdos tópicos ementa")

        # Consultas estruturais recebem candidatos extras; o contexto final continua limitado.
        multiplicador = p.multiplicador_calendario if intencao["calendario"] else (2 if busca_lista else 1)
        limite = min(80, max(p.top_k, p.top_k * multiplicador))
        encontrados: dict[str, dict] = {}

        def executar_busca(filtros: dict | None):
            for pergunta in consultas:
                vetor = self.embedder.embed_consulta(pergunta)
                for resultado in indice.buscar(vetor, limite, filtros=filtros):
                    if isinstance(resultado, dict):
                        bloco = _enriquecer_metadados_legados(resultado)
                        score = float(bloco.pop("similaridade", 0.0))
                    else:
                        posicao, score = resultado
                        bloco = _enriquecer_metadados_legados(indice.blocos[posicao])
                    chave = _chave_bloco(bloco)
                    lexical = _lexical_score(consulta, bloco)
                    rank = float(score) * (0.78 if intencao["calendario"] else 0.9) + lexical * (0.22 if intencao["calendario"] else 0.1)
                    anterior = encontrados.get(chave)
                    candidato = dict(bloco, similaridade=float(score), _rank=rank)
                    if anterior is None or rank > anterior["_rank"]:
                        encontrados[chave] = candidato

        executar_busca(intencao["filtros"] or None)
        # Pontos legados podem não ter metadados novos. O fallback é usado apenas
        # quando o filtro não encontrou candidatos, nunca para misturar bases.
        if not encontrados and intencao["filtros"]:
            executar_busca(None)

        trechos = [
            dict(t, similaridade=round(t["similaridade"], 4))
            for t in sorted(encontrados.values(), key=lambda x: -x["_rank"])
            if t["similaridade"] >= p.limiar
        ]
        if not intencao["calendario"] and not busca_lista:
            trechos = trechos[:p.top_k]
        todos = self._listar_blocos(indice, intencao["filtros"] or None)
        if not todos and intencao["filtros"]:
            todos = self._listar_blocos(indice, None)
        if intencao["codigo_prazo"]:
            # Siglas curtas perdem força no embedding. Localiza o evento exato
            # no calendário e calcula a similaridade real dos blocos encontrados.
            calendario = [b for b in todos if b.get("tipo_documento") == "calendario"]
            eventos = eventos_de_prazo(calendario, intencao["codigo_prazo"])
            candidatos = list({_chave_bloco(e["bloco"]): e["bloco"] for e in eventos}.values())
            if candidatos:
                from .indexacao import texto_para_embedding

                vetores = self.embedder.embed_documentos([texto_para_embedding(b) for b in candidatos])
                consulta_vetor = self.embedder.embed_consulta(consulta)
                return [
                    dict(b, similaridade=round(float(nota), 4))
                    for b, nota in sorted(zip(candidatos, vetores @ consulta_vetor), key=lambda par: -par[1])
                ]
            return []
        if not trechos:
            return []
        if intencao["calendario"]:
            expandidos = self._expandir_documento(
                trechos, todos, max_blocos=p.contexto_max_blocos,
                multiplos=intencao["mistura_norma"],
            )
            if expandidos:
                return expandidos

        if not busca_lista:
            return trechos

        # Para listas oficiais, seleciona a seção de programa/ementa e devolve a
        # seção inteira, preservando a ordem, inclusive quando vier do Qdrant.
        grupos: dict[tuple[str, str], list[dict]] = {}
        for bloco in todos:
            secao = str(bloco.get("secao", ""))
            secao_normalizada = unicodedata.normalize("NFKD", secao).encode("ascii", "ignore").decode().lower()
            if "programa" not in secao_normalizada and "ementa" not in secao_normalizada:
                continue
            grupos.setdefault((str(bloco.get("arquivo", "")), secao), []).append(bloco)
        if not grupos:
            return trechos

        def nota(grupo: list[dict]) -> tuple[float, int]:
            scores = [encontrados.get(_chave_bloco(b), {}).get("_rank", -1.0) for b in grupo]
            secao = unicodedata.normalize("NFKD", str(grupo[0].get("secao", ""))).encode("ascii", "ignore").decode().lower()
            return max(scores), int("programa" in secao)

        grupo = max(grupos.values(), key=nota)
        if nota(grupo)[0] < p.limiar:
            return trechos
        score_padrao = max(nota(grupo)[0], p.limiar)
        return [dict(b, similaridade=round(encontrados.get(_chave_bloco(b), {}).get("similaridade", score_padrao), 4))
                for b in sorted(grupo, key=_ordem_bloco)]

    @staticmethod
    def _listar_blocos(indice, filtros: dict | None = None) -> list[dict]:
        if hasattr(indice, "listar_blocos"):
            blocos = indice.listar_blocos(filtros=filtros)
        else:
            blocos = list(getattr(indice, "blocos", []))
        blocos = [_enriquecer_metadados_legados(bloco) for bloco in blocos]
        if filtros:
            blocos = [b for b in blocos if all(str(b.get(k, "")).lower() == str(v).lower() for k, v in filtros.items())]
        unicos = {}
        for bloco in blocos:
            chave = _chave_bloco(bloco)
            # Ponto estável vence o legado quando os dois representam o mesmo texto.
            anterior = unicos.get(chave)
            if anterior is None or (bloco.get("chunk_id") and not anterior.get("chunk_id")):
                unicos[chave] = bloco
        return list(unicos.values())

    def _expandir_documento(self, trechos: list[dict], todos: list[dict], max_blocos: int, multiplos: bool = False) -> list[dict]:
        """Depois do primeiro hit, recupera o documento relacionado sem misturá-lo a outros."""
        if not todos:
            return []
        grupos: dict[str, list[dict]] = {}
        notas: dict[str, float] = {}
        for bloco in todos:
            chave = str(bloco.get("document_id") or bloco.get("arquivo") or bloco.get("documento"))
            grupos.setdefault(chave, []).append(bloco)
        for trecho in trechos:
            chave = str(trecho.get("document_id") or trecho.get("arquivo") or trecho.get("documento"))
            notas[chave] = max(notas.get(chave, -1.0), float(trecho.get("_rank", trecho.get("similaridade", 0.0))))
        if not notas:
            return []
        chaves = sorted(notas, key=notas.get, reverse=True)[:2 if multiplos else 1]
        resultado = []
        for chave in chaves:
            grupo = sorted(grupos.get(chave, []), key=_ordem_bloco)
            if not grupo:
                continue
            eh_calendario = any(b.get("tipo_documento") == "calendario" for b in grupo)
            if multiplos and not eh_calendario:
                ids_relevantes = {_chave_bloco(t) for t in trechos if str(t.get("document_id") or t.get("arquivo") or t.get("documento")) == chave}
                grupo = [b for b in grupo if _chave_bloco(b) in ids_relevantes][:max(4, max_blocos // 4)]
            else:
                # Documentos pequenos, como calendários, são uma unidade semântica.
                grupo = grupo[:max_blocos]
            score = max(notas[chave], self.parametros.limiar)
            resultado.extend(dict(bloco, similaridade=round(
                next((t["similaridade"] for t in trechos if _chave_bloco(t) == _chave_bloco(bloco)), score), 4))
                for bloco in grupo)
        return resultado[:max_blocos if not multiplos else max_blocos + max(4, max_blocos // 4)]


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
            contexto = []
            if t.get("pagina"):
                contexto.append(f"página {t['pagina']}")
            if t.get("mes") and t.get("ano"):
                contexto.append(f"mês de referência {int(t['mes']):02d}/{t['ano']}")
            if t.get("modalidade"):
                contexto.append(f"modalidade {t['modalidade']}")
            if t.get("tipo_documento"):
                contexto.append(f"tipo {t['tipo_documento']}")
            complemento = f" ({'; '.join(contexto)})" if contexto else ""
            partes.append(f"[Trecho {n}] Fonte: {t['documento']} – {t['secao']}{complemento}{atualizado}\n{t['texto']}")
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
        elif codigo := codigo_avaliacao_e_prazo(pergunta):
            prazos = eventos_de_prazo(trechos, codigo)
            documentos = {str(e["bloco"].get("document_id") or e["bloco"].get("arquivo")) for e in prazos}
            if len(documentos) > 1:
                texto = f"Encontrei mais de um calendário com prazo para a avaliação {codigo}. Informe o período e a modalidade para identificar a data correta."
                modo = "sem_resultado"
            else:
                factual = resposta_factual_de_eventos(pergunta, trechos)
                texto, modo = (factual, "validado") if factual else (MENSAGEM_NAO_ENCONTRADA, "sem_resultado")
        else:
            try:
                texto = self.llm.gerar(self.montar_mensagens(pergunta, trechos, historico), p.temperatura)
                modo = "llm"
                valido, motivos = validar_resposta_datas(pergunta, texto, trechos)
                if not valido:
                    logger.warning("Resposta rejeitada pela validação factual: %s", "; ".join(motivos))
                    factual = resposta_factual_de_eventos(pergunta, trechos)
                    if factual:
                        texto, modo = factual, "validado"
                    else:
                        texto, modo = MENSAGEM_NAO_ENCONTRADA, "sem_resultado"
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
