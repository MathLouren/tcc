"""Roda um arquivo de perguntas em Markdown (formato "### ID — Tema" + Pergunta/Resposta esperada/Termos).

Uso: python avaliacao/rodar_perguntas_md.py ARQUIVO.md ROTULO
Salva as respostas em avaliacao/resultados/perguntas_ROTULO.json. Não altera nada no chatbot.
A conferência por termos é só uma pista: a avaliação de verdade é ler cada resposta.
"""
import json, re, sys, tempfile, time, unicodedata
from dataclasses import replace
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from app.config import carregar_config  # noqa: E402
from app.servico import Servico  # noqa: E402


def norm(t):
    t = unicodedata.normalize("NFKD", str(t).lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())


def ler_casos(texto):
    casos, nivel = [], ""
    for parte in re.split(r"\n(?=##+ )", texto):
        cab = parte.splitlines()[0]
        if cab.startswith("## ") and not cab.startswith("###"):
            nivel = cab[3:].strip()
            continue
        if not cab.startswith("### "):
            continue
        campo = lambda n: (re.search(rf"\*\*{n}:\*\*\s*(.+)", parte) or [None, ""])[1].strip()
        id_, _, tema = cab[4:].partition(" — ")
        tema_nivel = re.match(r"(Fácil|Médio|Difícil|Fora do escopo)\s*\|\s*(.+)", tema)
        casos.append({"id": id_.strip(), "nivel": tema_nivel[1] if tema_nivel else nivel,
                      "tema": tema_nivel[2] if tema_nivel else tema.strip(),
                      "pergunta": campo("Pergunta"), "esperada": campo("Resposta esperada"),
                      "termos": [t.strip(" .") for t in campo("Termos esperados").split(",") if t.strip(" .")]})
    return casos


def main():
    arquivo, rotulo = Path(sys.argv[1]), sys.argv[2]
    casos = ler_casos(arquivo.read_text(encoding="utf-8"))
    servico = Servico(replace(carregar_config(), registros_dir=Path(tempfile.mkdtemp())))
    for n, c in enumerate(casos):
        inicio, sessao, esclarecimento = time.time(), f"{rotulo}-{n}", None
        r = servico.motor.responder(c["pergunta"], sessao)
        if r.modo == "esclarecimento":
            esclarecimento = r.resposta
            pedido = norm(r.resposta)
            if "curso" in pedido:
                resposta = "Sistemas de Informação"
            elif "ead" in pedido or "presencia" in pedido:
                resposta = "EaD" if "ead" in norm(c["pergunta"]) else "presencial"
            else:
                resposta = "graduação"
            r = servico.motor.responder(resposta, sessao)
        texto = norm(r.resposta)
        c.update(resposta=r.resposta, modo=r.modo, esclarecimento=esclarecimento,
                 segundos=round(time.time() - inicio, 1),
                 termos_achados=sum(norm(t) in texto for t in c["termos"]), termos_total=len(c["termos"]))
        print(f"{c['id']} [{r.modo}] termos {c['termos_achados']}/{c['termos_total']} {c['segundos']}s", flush=True)
    destino = RAIZ / "avaliacao" / "resultados" / f"perguntas_{rotulo}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(casos, ensure_ascii=False, indent=1), encoding="utf-8")
    print("salvo em", destino)


if __name__ == "__main__":
    main()
