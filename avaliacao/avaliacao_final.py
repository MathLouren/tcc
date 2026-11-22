"""Avaliação automática do chatbot sobre os conjuntos de perguntas em Markdown.

Uso, na pasta do projeto e com o ambiente virtual ativo:
    python avaliacao/avaliacao_final.py ROTULO ARQUIVO.md [ARQUIVO.md ...]

Para cada pergunta, grava a resposta, o modo, o tempo e os trechos usados (que o LLM-juiz lê para
julgar a fidelidade), e calcula as métricas automáticas:
- Hit@k e MRR: posição do primeiro trecho do documento esperado entre os k recuperados pela busca;
- recall de termos no contexto: parcela dos termos esperados presentes nos trechos recuperados;
- recall de termos na resposta: parcela dos termos esperados presentes na resposta;
- abstenção correta: perguntas fora do escopo recusadas;
- tempo de resposta.
Saída: avaliacao/resultados/avaliacao_ROTULO.json
"""
from __future__ import annotations

import json
import re
import statistics
import sys
import tempfile
import time
import unicodedata
from dataclasses import replace
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "avaliacao"))

from app.config import carregar_config  # noqa: E402
from app.servico import Servico  # noqa: E402
from rodar_perguntas_md import ler_casos  # noqa: E402

MARCAS_ABSTENCAO = ("nao localizei", "nao encontrei", "nao consta", "nao ha informac", "nao foi possivel localizar")
# Documento que sustenta cada conjunto: o conjunto MIA vem do Manual do Aluno; o GC, da grade curricular.
DOCUMENTO_ESPERADO = {"MIA": "universo_l_mia", "GC": "grade_curricular"}


def norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto).lower())
    return " ".join("".join(c for c in texto if not unicodedata.combining(c)).split())


def recall_de_termos(termos: list[str], texto: str) -> float | None:
    if not termos:
        return None
    alvo = norm(texto)
    return sum(norm(t) in alvo for t in termos) / len(termos)


def responder(motor, pergunta: str, sessao: str):
    """Responde como um aluno faria: se o chatbot pedir esclarecimento, responde ao pedido."""
    r = motor.responder(pergunta, sessao)
    esclarecimento = None
    if r.modo == "esclarecimento":
        esclarecimento = r.resposta
        pedido = norm(r.resposta)
        if "curso" in pedido:
            resposta = "Sistemas de Informação"
        elif "ead" in pedido or "presencia" in pedido:
            resposta = "EaD" if "ead" in norm(pergunta) else "presencial"
        else:
            resposta = "graduação"
        r = motor.responder(resposta, sessao)
    return r, esclarecimento


def main() -> None:
    rotulo, arquivos = sys.argv[1], [Path(a) for a in sys.argv[2:]]
    casos = []
    for arquivo in arquivos:
        casos += ler_casos(arquivo.read_text(encoding="utf-8"))

    servico = Servico(replace(carregar_config(), registros_dir=Path(tempfile.mkdtemp(prefix="avaliacao_"))))
    motor = servico.motor
    por_id = {b["id"]: b for b in motor.indice.blocos}
    k = motor.parametros.top_k

    for n, c in enumerate(casos):
        fora = c["nivel"].startswith("Fora")
        prefixo = c["id"].split("-")[0]
        esperado = DOCUMENTO_ESPERADO.get(prefixo, "")

        recuperados = motor.recuperar(c["pergunta"])
        posicao = next((i for i, b in enumerate(recuperados, 1) if esperado and esperado in b.get("arquivo", "")), None)
        contexto = "\n".join(b.get("texto", "") for b in recuperados)

        inicio = time.perf_counter()
        r, esclarecimento = responder(motor, c["pergunta"], f"{rotulo}-{n}")
        segundos = time.perf_counter() - inicio

        usados = [por_id[t["id"]] for t in r.trechos if t.get("id") in por_id]
        texto_resposta = norm(r.resposta)
        c.update(
            fora_do_escopo=fora,
            resposta=r.resposta,
            modo=r.modo,
            esclarecimento=esclarecimento,
            segundos=round(segundos, 2),
            trechos_usados=[{"documento": b.get("documento"), "secao": b.get("secao"), "texto": b.get("texto", "")}
                            for b in usados],
            posicao_documento_esperado=None if fora else posicao,
            recall_termos_contexto=None if fora else recall_de_termos(c["termos"], contexto),
            recall_termos_resposta=None if fora else recall_de_termos(c["termos"], r.resposta),
            absteve=r.modo == "sem_resultado" or any(m in texto_resposta for m in MARCAS_ABSTENCAO),
        )
        print(f"{c['id']} [{r.modo}] {c['segundos']}s", flush=True)

    dentro = [c for c in casos if not c["fora_do_escopo"]]
    fora = [c for c in casos if c["fora_do_escopo"]]
    tempos = [c["segundos"] for c in casos]

    def media(valores):
        valores = [v for v in valores if v is not None]
        return round(sum(valores) / len(valores), 4) if valores else None

    metricas = {
        "perguntas": len(casos), "dentro_do_escopo": len(dentro), "fora_do_escopo": len(fora), "top_k": k,
        "hit_at_k": media([c["posicao_documento_esperado"] is not None for c in dentro]),
        "mrr": media([1 / c["posicao_documento_esperado"] if c["posicao_documento_esperado"] else 0 for c in dentro]),
        "recall_termos_contexto": media([c["recall_termos_contexto"] for c in dentro]),
        "recall_termos_resposta": media([c["recall_termos_resposta"] for c in dentro]),
        "abstencao_correta": media([c["absteve"] for c in fora]),
        "abstencao_indevida": media([c["absteve"] for c in dentro]),
        "tempo_medio_s": round(statistics.mean(tempos), 2),
        "tempo_mediano_s": round(statistics.median(tempos), 2),
        "tempo_p95_s": round(sorted(tempos)[int(0.95 * (len(tempos) - 1))], 2),
        "tempo_maximo_s": round(max(tempos), 2),
        "modelo": servico.config.llm_modelo,
        "embeddings": servico.config.embedding_modelo,
        "parametros": {"top_k": k, "limiar": motor.parametros.limiar, "temperatura": motor.parametros.temperatura,
                       "peso_lexical": getattr(motor.parametros, "peso_lexical", None),
                       "historico_trocas": motor.parametros.historico_trocas,
                       "chunk_tokens": servico.config.chunk_tokens, "chunk_sobreposicao": servico.config.chunk_sobreposicao,
                       "llm_num_ctx": servico.config.llm_num_ctx},
        "blocos_indexados": len(motor.indice.blocos),
        "documentos_indexados": sorted({b.get("documento") for b in motor.indice.blocos}),
    }
    destino = RAIZ / "avaliacao" / "resultados" / f"avaliacao_{rotulo}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps({"metricas": metricas, "casos": casos}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(metricas, ensure_ascii=False, indent=1))
    print("salvo em", destino)


if __name__ == "__main__":
    main()
