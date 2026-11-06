"""Resume tarefas respondidas por participantes reais, sem criar dados de exemplo."""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from avaliacao.avaliar_sao_goncalo import carregar_perguntas


def carregar_tarefas(caminho: Path) -> list[dict]:
    perguntas = {q["id"]: q for q in carregar_perguntas()}
    with caminho.open(encoding="utf-8-sig", newline="") as arquivo:
        linhas = list(csv.DictReader(arquivo, delimiter=";"))
    tarefas = []
    vistos = set()
    for numero, linha in enumerate(linhas, start=2):
        if not any((v or "").strip() for v in linha.values()):
            continue
        participante = (linha.get("participante") or "").strip()
        grupo = (linha.get("grupo") or "").strip().lower()
        ident = (linha.get("pergunta_id") or "").strip()
        if not participante or grupo not in {"aluno", "candidato"} or ident not in perguntas:
            raise ValueError(f"linha {numero}: participante, grupo ou pergunta invalida")
        if (participante, ident) in vistos:
            raise ValueError(f"linha {numero}: tarefa duplicada")
        vistos.add((participante, ident))
        concluiu = (linha.get("concluiu") or "").strip().lower()
        if concluiu not in {"sim", "nao"}:
            raise ValueError(f"linha {numero}: concluiu deve ser sim ou nao")
        notas = {}
        for campo in ("clareza_1_a_5", "confianca_1_a_5", "utilidade_fontes_1_a_5"):
            valor = int(linha[campo])
            if not 1 <= valor <= 5:
                raise ValueError(f"linha {numero}: {campo} fora da escala")
            notas[campo] = valor
        tarefas.append({"participante": participante, "grupo": grupo, "pergunta_id": ident,
                        "dificuldade": perguntas[ident]["dificuldade"], "concluiu": int(concluiu == "sim"),
                        **notas, "comentario": (linha.get("comentario") or "").strip()})
    return tarefas


def resumir(tarefas: list[dict]) -> dict:
    if not tarefas:
        raise ValueError("nenhuma tarefa real preenchida")
    def estatisticas(linhas: list[dict]) -> dict:
        return {"tarefas": len(linhas), "participantes": len({r["participante"] for r in linhas}),
                "conclusao": round(statistics.mean(r["concluiu"] for r in linhas), 3),
                **{campo: round(statistics.mean(r[campo] for r in linhas), 2)
                   for campo in ("clareza_1_a_5", "confianca_1_a_5", "utilidade_fontes_1_a_5")}}
    grupos = defaultdict(list)
    niveis = defaultdict(list)
    for linha in tarefas:
        grupos[linha["grupo"]].append(linha)
        niveis[linha["dificuldade"]].append(linha)
    return {"global": estatisticas(tarefas),
            "por_grupo": {k: estatisticas(v) for k, v in sorted(grupos.items())},
            "por_dificuldade": {k: estatisticas(v) for k, v in sorted(niveis.items())}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path, help="CSV com respostas reais no formato usuarios_tarefas_modelo.csv")
    args = parser.parse_args()
    try:
        resultado = resumir(carregar_tarefas(args.csv))
    except (OSError, ValueError, KeyError) as erro:
        parser.error(str(erro))
    print(json.dumps(resultado, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
