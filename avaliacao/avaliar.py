"""Validação do chatbot com o conjunto de perguntas de avaliação (seção 4.3 do TCC).

Uso, na pasta do projeto e com o ambiente virtual ativo:
    python avaliacao/avaliar.py              # usa o LLM configurado no .env (Ollama)
    python avaliacao/avaliar.py --sem-llm    # avalia apenas recuperação e modo extrativo

Métricas geradas:
- Hit@k e MRR: o documento esperado aparece entre os k trechos recuperados?
- Precisão do contexto: fração dos trechos usados que vêm dos documentos esperados;
- Acurácia automática: a resposta contém os termos do gabarito?
- Abstenção: perguntas fora do escopo foram recusadas?
- Tempo de resposta e limiar de similaridade sugerido.

Saídas em avaliacao/resultados/: relatório em Markdown, resultados em JSON e uma planilha CSV
para a revisão humana (dois avaliadores), que classifica as respostas como corretas,
parcialmente corretas ou incorretas, como descrito no TCC.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import carregar_config  # noqa: E402
from app.llm import LLMIndisponivel  # noqa: E402
from app.servico import Servico  # noqa: E402

MARCAS_ABSTENCAO = (
    "nao localizei", "nao encontrei", "nao consta", "nao foi possivel localizar", "nao ha informac",
)


class LLMDesligado:
    def gerar(self, mensagens, temperatura):
        raise LLMIndisponivel("LLM desligado para esta avaliação (--sem-llm).")

    def disponivel(self):
        return False


def normalizar(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in decomposto if not unicodedata.combining(c)).lower()


def media(valores):
    valores = [v for v in valores if v is not None]
    return round(statistics.mean(valores), 3) if valores else None


def avaliar_perguntas(servico: Servico, perguntas: list[dict]) -> list[dict]:
    top_k = servico.parametros.top_k
    categorias_indexadas = {b.get("categoria") for b in servico.indice.blocos}
    resultados = []
    for n, q in enumerate(perguntas, start=1):
        print(f"[{n}/{len(perguntas)}] {q['pergunta']}")
        base = {
            "id": q["id"],
            "categoria": q["categoria"],
            "pergunta": q["pergunta"],
            "fora_do_escopo": bool(q.get("fora_do_escopo")),
        }
        requer = q.get("requer_categoria")
        if requer and requer not in categorias_indexadas:
            resultados.append({**base, "status": "pulada", "motivo": f"nenhum documento da categoria '{requer}' indexado"})
            continue

        vetor = servico.embedder.embed_consulta(q["pergunta"])
        ranking = servico.indice.buscar(vetor, top_k)
        arquivos = [servico.indice.blocos[i]["arquivo"] for i, _ in ranking]
        esperados = set(q.get("documentos_esperados") or [])
        posicao = next((p for p, a in enumerate(arquivos, start=1) if a in esperados), None)

        resposta = servico.motor.responder(q["pergunta"])
        texto = normalizar(resposta.resposta)
        absteve = resposta.modo == "sem_resultado" or any(m in texto for m in MARCAS_ABSTENCAO)

        if base["fora_do_escopo"]:
            classificacao = "abstencao_correta" if absteve else "respondeu_fora_do_escopo"
        else:
            grupos = q.get("termos_esperados") or []
            contem = all(any(normalizar(t) in texto for t in grupo) for grupo in grupos)
            classificacao = "correta" if contem else ("abstencao" if absteve else "incorreta_ou_parcial")

        usados = [t["arquivo"] for t in resposta.trechos]
        precisao = (sum(a in esperados for a in usados) / len(usados)) if usados and esperados else None

        resultados.append({
            **base,
            "status": "avaliada",
            "classificacao": classificacao,
            "resposta": resposta.resposta,
            "modo": resposta.modo,
            "tempo_ms": resposta.tempo_ms,
            "similaridade_max": round(ranking[0][1], 4) if ranking else 0.0,
            "posicao_documento_esperado": posicao,
            "precisao_contexto": precisao,
            "fontes": [f"{f['documento']} – {f['secao']}" for f in resposta.fontes],
        })
    return resultados


def sugerir_limiar(dentro: list[dict], fora: list[dict]) -> dict | None:
    """Limiar que melhor separa perguntas do escopo (devem passar) e fora do escopo (devem ser barradas)."""
    if not dentro or not fora:
        return None
    pares = []
    for i in range(30, 96):
        limiar = i / 100
        acertos = sum(r["similaridade_max"] >= limiar for r in dentro) + sum(r["similaridade_max"] < limiar for r in fora)
        pares.append((limiar, acertos / (len(dentro) + len(fora))))
    melhor = max(taxa for _, taxa in pares)
    empatados = [limiar for limiar, taxa in pares if taxa == melhor]
    return {"limiar": round((empatados[0] + empatados[-1]) / 2, 2), "taxa_separacao": round(melhor, 3)}


def calcular_metricas(resultados: list[dict]) -> dict:
    avaliadas = [r for r in resultados if r["status"] == "avaliada"]
    dentro = [r for r in avaliadas if not r["fora_do_escopo"]]
    fora = [r for r in avaliadas if r["fora_do_escopo"]]

    por_categoria: dict[str, Counter] = defaultdict(Counter)
    for r in avaliadas:
        por_categoria[r["categoria"]][r["classificacao"]] += 1

    tempos = [r["tempo_ms"] for r in avaliadas]
    return {
        "perguntas_total": len(resultados),
        "perguntas_avaliadas": len(avaliadas),
        "perguntas_puladas": len(resultados) - len(avaliadas),
        "hit_at_k": media([r["posicao_documento_esperado"] is not None for r in dentro]),
        "mrr": media([1 / r["posicao_documento_esperado"] if r["posicao_documento_esperado"] else 0 for r in dentro]),
        "precisao_contexto": media([r["precisao_contexto"] for r in dentro]),
        "acuracia_automatica": media([r["classificacao"] == "correta" for r in dentro]),
        "abstencao_fora_do_escopo": media([r["classificacao"] == "abstencao_correta" for r in fora]),
        "tempo_medio_ms": round(statistics.mean(tempos)) if tempos else None,
        "tempo_mediano_ms": round(statistics.median(tempos)) if tempos else None,
        "tempo_maximo_ms": max(tempos) if tempos else None,
        "modos": dict(Counter(r["modo"] for r in avaliadas)),
        "por_categoria": {c: dict(v) for c, v in por_categoria.items()},
        "limiar_sugerido": sugerir_limiar(dentro, fora),
    }


def pct(valor) -> str:
    return "-" if valor is None else f"{valor * 100:.1f}%".replace(".", ",")


def escrever_relatorio(pasta: Path, carimbo: str, servico: Servico, metricas: dict, resultados: list[dict]) -> Path:
    p = servico.parametros
    linhas = [
        "# Relatório de validação – Chatbot Institucional UNIVERSO",
        "",
        f"- Data: {datetime.now():%d/%m/%Y %H:%M}",
        f"- Modelo de linguagem: {servico.config.llm_modelo} (API {servico.config.llm_api})",
        f"- Embeddings: {servico.config.embedding_modelo} ({servico.config.embedding_provedor})",
        f"- Parâmetros: top_k = {p.top_k}, limiar = {p.limiar}, temperatura = {p.temperatura}",
        f"- Base: {servico.info.get('n_documentos')} documentos, {servico.info.get('n_blocos')} blocos",
        "",
        "## Métricas globais",
        "",
        "| Métrica | Valor |",
        "|---|---|",
        f"| Perguntas avaliadas (puladas) | {metricas['perguntas_avaliadas']} ({metricas['perguntas_puladas']}) |",
        f"| Hit@{p.top_k} (documento esperado recuperado) | {pct(metricas['hit_at_k'])} |",
        f"| MRR | {metricas['mrr'] if metricas['mrr'] is not None else '-'} |",
        f"| Precisão do contexto | {pct(metricas['precisao_contexto'])} |",
        f"| Acurácia automática (termos do gabarito) | {pct(metricas['acuracia_automatica'])} |",
        f"| Abstenção correta fora do escopo | {pct(metricas['abstencao_fora_do_escopo'])} |",
        f"| Tempo médio / mediano / máximo (ms) | {metricas['tempo_medio_ms']} / {metricas['tempo_mediano_ms']} / {metricas['tempo_maximo_ms']} |",
        f"| Modos de resposta | {metricas['modos']} |",
        "",
    ]
    limiar = metricas["limiar_sugerido"]
    if limiar:
        linhas += [
            f"Limiar de similaridade sugerido: **{limiar['limiar']}** "
            f"(separa {pct(limiar['taxa_separacao'])} das perguntas entre dentro e fora do escopo). "
            "Para aplicá-lo, use PUT /admin/config.",
            "",
        ]
    linhas += ["## Resultados por categoria", "", "| Categoria | Classificações |", "|---|---|"]
    for categoria, contagem in metricas["por_categoria"].items():
        linhas.append(f"| {categoria} | {', '.join(f'{k}: {v}' for k, v in sorted(contagem.items()))} |")
    linhas += ["", "## Respostas que precisam de revisão", ""]
    revisar = [r for r in resultados if r.get("classificacao") not in (None, "correta", "abstencao_correta")]
    if not revisar:
        linhas.append("Nenhuma.")
    for r in revisar:
        linhas += [
            f"### Pergunta {r['id']} – {r['classificacao']}",
            f"**Pergunta:** {r['pergunta']}",
            "",
            f"**Resposta ({r['modo']}):** {r['resposta']}",
            "",
            f"**Fontes:** {'; '.join(r['fontes']) or 'nenhuma'}",
            "",
        ]
    puladas = [r for r in resultados if r["status"] == "pulada"]
    if puladas:
        linhas += ["## Perguntas puladas", ""] + [f"- {r['id']}: {r['pergunta']} ({r['motivo']})" for r in puladas]

    caminho = pasta / f"relatorio_{carimbo}.md"
    caminho.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return caminho


def escrever_planilha_revisao(pasta: Path, carimbo: str, resultados: list[dict]) -> Path:
    caminho = pasta / f"revisao_humana_{carimbo}.csv"
    with caminho.open("w", encoding="utf-8-sig", newline="") as f:
        escritor = csv.writer(f, delimiter=";")
        escritor.writerow([
            "id", "categoria", "pergunta", "resposta", "fontes", "classificacao_automatica",
            "avaliador_1", "avaliador_2", "consenso",
        ])
        for r in resultados:
            if r["status"] != "avaliada":
                continue
            escritor.writerow([
                r["id"], r["categoria"], r["pergunta"], r["resposta"], "; ".join(r["fontes"]),
                r["classificacao"], "", "", "",
            ])
    return caminho


def main() -> None:
    analisador = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    analisador.add_argument("--perguntas", default=str(RAIZ / "avaliacao" / "perguntas.json"))
    analisador.add_argument("--sem-llm", action="store_true", help="não chama o LLM (respostas em modo extrativo)")
    args = analisador.parse_args()

    perguntas = json.loads(Path(args.perguntas).read_text(encoding="utf-8"))["perguntas"]
    servico = Servico(carregar_config(), llm=LLMDesligado() if args.sem_llm else None)
    if not args.sem_llm and not servico.llm.disponivel():
        print("ATENÇÃO: o modelo de linguagem não respondeu; as respostas sairão em modo extrativo.")

    resultados = avaliar_perguntas(servico, perguntas)
    metricas = calcular_metricas(resultados)

    pasta = RAIZ / "avaliacao" / "resultados"
    pasta.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    (pasta / f"resultados_{carimbo}.json").write_text(
        json.dumps({"metricas": metricas, "resultados": resultados}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    relatorio = escrever_relatorio(pasta, carimbo, servico, metricas, resultados)
    planilha = escrever_planilha_revisao(pasta, carimbo, resultados)

    print()
    print(f"Hit@{servico.parametros.top_k}: {pct(metricas['hit_at_k'])}")
    print(f"Acurácia automática: {pct(metricas['acuracia_automatica'])}")
    print(f"Abstenção fora do escopo: {pct(metricas['abstencao_fora_do_escopo'])}")
    print(f"Tempo médio: {metricas['tempo_medio_ms']} ms")
    if metricas["limiar_sugerido"]:
        print(f"Limiar sugerido: {metricas['limiar_sugerido']['limiar']}")
    print(f"Relatório: {relatorio}")
    print(f"Planilha para revisão humana: {planilha}")


if __name__ == "__main__":
    main()
