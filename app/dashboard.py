"""Indicadores do painel administrativo, calculados a partir dos registros.

Responde a três perguntas da equipe:
- o sistema está conseguindo responder? (desfecho de cada pergunta e disponibilidade do modelo);
- as respostas estão certas? (avaliação dos alunos e validação da secretaria ou coordenação);
- quando o chatbot é usado? (perguntas por dia e por hora).

Saudações e pedidos de esclarecimento são interações, mas não perguntas acadêmicas respondidas ou
não: ficam de fora das taxas de resposta e da fila de validação.
"""
from __future__ import annotations

import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from app.registros import agora, ler_data_hora

PERIODOS = {"hoje": 0, "7d": 7, "30d": 30, "90d": 90, "tudo": None}
RESPONDIDAS = {"llm", "validado", "citacao"}
SEM_RESULTADO = {"sem_resultado"}
MODELO_INDISPONIVEL = {"extrativo"}
OUTRAS = {"conversa", "esclarecimento"}
MAX_PARA_REVISAR = 15
MAX_SEM_RESPOSTA = 8


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto).lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c)).replace("-", "")  # "Wi-Fi" = "wifi"
    return " ".join(re.sub(r"[^\w\s]", " ", texto).split())


def _desfecho(modo: str) -> str:
    if modo in RESPONDIDAS:
        return "respondidas"
    if modo in SEM_RESULTADO:
        return "sem_resultado"
    if modo in MODELO_INDISPONIVEL:
        return "modelo_indisponivel"
    return "outras"


def _taxa(parte: int, todo: int) -> float | None:
    return round(parte / todo, 4) if todo else None


def _percentil(valores: list[int], p: float) -> int | None:
    if not valores:
        return None
    ordenados = sorted(valores)
    return ordenados[min(len(ordenados) - 1, int(round(p * (len(ordenados) - 1))))]


def inicio_do_periodo(periodo: str, referencia: datetime) -> datetime | None:
    dias = PERIODOS.get(periodo, 30)
    if dias is None:
        return None
    meia_noite = referencia.replace(hour=0, minute=0, second=0, microsecond=0)
    return meia_noite - timedelta(days=max(dias - 1, 0))


def montar_dashboard(interacoes: list[dict], avaliacoes: dict[str, dict], revisoes: dict[str, dict],
                     periodo: str = "30d", referencia: datetime | None = None) -> dict:
    referencia = referencia or agora()
    inicio = inicio_do_periodo(periodo, referencia)

    no_periodo = []
    for r in interacoes:
        quando = ler_data_hora(r.get("data_hora"))
        if quando is None or (inicio and quando < inicio) or quando > referencia + timedelta(minutes=5):
            continue
        no_periodo.append((quando, r))
    no_periodo.sort(key=lambda par: par[0])

    perguntas = [(q, r) for q, r in no_periodo if r.get("modo") not in OUTRAS]
    desfechos = Counter(_desfecho(r.get("modo")) for _, r in perguntas)

    # ---------------------------------------------------------------- avaliação dos alunos
    avaliadas = [avaliacoes[r["id"]] for _, r in no_periodo if r.get("id") in avaliacoes]
    uteis = sum(1 for a in avaliadas if a.get("util"))

    # ---------------------------------------------------------------- validação da equipe
    revisadas = [revisoes[r["id"]] for _, r in perguntas if r.get("id") in revisoes]
    vereditos = Counter(v.get("veredito") for v in revisadas)
    pontos = {"correta": 1.0, "parcial": 0.5, "incorreta": 0.0}
    escore = (sum(pontos.get(v.get("veredito"), 0) for v in revisadas) / len(revisadas)) if revisadas else None
    pendentes = [(q, r) for q, r in perguntas if r.get("id") not in revisoes]

    # ---------------------------------------------------------------- tempos
    tempos = [int(r.get("tempo_ms") or 0) for _, r in perguntas]
    tempos_modelo = [int(r.get("tempo_ms") or 0) for _, r in perguntas if r.get("modo") in {"llm", "citacao"}]
    metricas = [r.get("metricas") or {} for _, r in perguntas]
    primeiros_tokens = [int(m["primeiro_token_ms"]) for m in metricas if m.get("primeiro_token_ms") is not None]

    # ---------------------------------------------------------------- séries por dia e por hora
    por_dia: dict[str, dict] = {}
    if no_periodo:
        primeiro = (inicio or no_periodo[0][0]).date()
        ultimo = referencia.date()
        dia = primeiro
        while dia <= ultimo:
            por_dia[dia.isoformat()] = {"data": dia.isoformat(), "respondidas": 0, "sem_resultado": 0,
                                        "modelo_indisponivel": 0, "uteis": 0, "nao_uteis": 0}
            dia += timedelta(days=1)
    por_hora = [0] * 24
    for quando, r in no_periodo:
        chave = quando.date().isoformat()
        if chave not in por_dia:
            continue
        desfecho = _desfecho(r.get("modo"))
        if desfecho != "outras":
            por_dia[chave][desfecho] += 1
            por_hora[quando.hour] += 1
        avaliacao = avaliacoes.get(r.get("id"))
        if avaliacao:
            por_dia[chave]["uteis" if avaliacao.get("util") else "nao_uteis"] += 1

    # ---------------------------------------------------------------- respostas para validar
    def prioridade(par):
        _, r = par
        avaliacao = avaliacoes.get(r.get("id"))
        if avaliacao and not avaliacao.get("util"):
            return 0, "Marcada como não útil pelo aluno"
        if r.get("modo") in MODELO_INDISPONIVEL:
            return 1, "Modelo indisponível: resposta só com trechos"
        if r.get("modo") in SEM_RESULTADO:
            return 2, "Sem resposta nos documentos"
        return 3, "Aguardando validação"

    fila = sorted(pendentes, key=lambda par: (prioridade(par)[0], -par[0].timestamp()))
    para_revisar = []
    for quando, r in fila[:MAX_PARA_REVISAR]:
        para_revisar.append({"id": r.get("id"), "data_hora": quando.isoformat(timespec="seconds"),
                             "pergunta": r.get("pergunta"), "modo": r.get("modo"), "motivo": prioridade((quando, r))[1]})

    # ---------------------------------------------------------------- perguntas sem resposta repetidas
    agrupadas: dict[str, dict] = defaultdict(lambda: {"vezes": 0})
    for quando, r in perguntas:
        if r.get("modo") in SEM_RESULTADO:
            grupo = agrupadas[_normalizar(r.get("pergunta", ""))]
            grupo["vezes"] += 1
            grupo["pergunta"] = r.get("pergunta")
            grupo["ultima"] = quando.isoformat(timespec="seconds")
    sem_resposta = sorted(agrupadas.values(), key=lambda g: (-g["vezes"], -ler_data_hora(g["ultima"]).timestamp()))

    falhas_modelo = [q for q, r in no_periodo if r.get("modo") in MODELO_INDISPONIVEL]
    total_perguntas = len(perguntas)
    return {
        "gerado_em": referencia.isoformat(timespec="seconds"),
        "periodo": {"codigo": periodo, "inicio": inicio.isoformat(timespec="seconds") if inicio else None,
                    "fim": referencia.isoformat(timespec="seconds")},
        "totais": {
            "interacoes": len(no_periodo),
            "perguntas": total_perguntas,
            "alunos": len({(r.get("nome") or "").strip().lower() for _, r in no_periodo if r.get("nome")}),
            "sessoes": len({r.get("sessao_id") for _, r in no_periodo if r.get("sessao_id")}),
        },
        "desfechos": {
            "respondidas": desfechos["respondidas"],
            "sem_resultado": desfechos["sem_resultado"],
            "modelo_indisponivel": desfechos["modelo_indisponivel"],
            "taxa_resposta": _taxa(desfechos["respondidas"], total_perguntas),
            "taxa_modelo_indisponivel": _taxa(desfechos["modelo_indisponivel"], total_perguntas),
        },
        "por_modo": dict(Counter(r.get("modo") for _, r in no_periodo)),
        "alunos": {
            "uteis": uteis,
            "nao_uteis": len(avaliadas) - uteis,
            "sem_avaliacao": len(no_periodo) - len(avaliadas),
            "taxa_util": _taxa(uteis, len(avaliadas)),
        },
        "validacao": {
            "correta": vereditos["correta"],
            "parcial": vereditos["parcial"],
            "incorreta": vereditos["incorreta"],
            "pendentes": len(pendentes),
            "escore": round(escore, 4) if escore is not None else None,
            "taxa_correta": _taxa(vereditos["correta"], len(revisadas)),
        },
        "tempo": {
            "mediano_ms": int(statistics.median(tempos)) if tempos else None,
            "medio_ms": round(statistics.mean(tempos)) if tempos else None,
            "modelo_medio_ms": round(statistics.mean(tempos_modelo)) if tempos_modelo else None,
            "modelo_p95_ms": _percentil(tempos_modelo, 0.95),
            # Com streaming, é o tempo até o aluno começar a ver a resposta.
            "primeiro_token_mediano_ms": int(statistics.median(primeiros_tokens)) if primeiros_tokens else None,
            "respostas_pela_reserva": sum(1 for m in metricas if m.get("reserva")),
            "respostas_do_cache": sum(1 for m in metricas if m.get("cache")),
        },
        "por_dia": list(por_dia.values()),
        "por_hora": por_hora,
        "ultima_interacao": no_periodo[-1][0].isoformat(timespec="seconds") if no_periodo else None,
        "ultima_falha_modelo": falhas_modelo[-1].isoformat(timespec="seconds") if falhas_modelo else None,
        "para_revisar": para_revisar,
        "sem_resposta_frequentes": sem_resposta[:MAX_SEM_RESPOSTA],
    }
