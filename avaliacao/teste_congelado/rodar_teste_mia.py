"""Roda as perguntas de perguntas_respostas_mia_2026_2.md no motor atual, sem alterar nada."""
import json, re, sys, tempfile, time, unicodedata
from dataclasses import replace
from pathlib import Path
RAIZ = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(RAIZ))
from app.config import carregar_config
from app.servico import Servico

def norm(t):
    t = unicodedata.normalize("NFKD", str(t).lower())
    return " ".join("".join(c for c in t if not unicodedata.combining(c)).split())

texto = (RAIZ / "avaliacao" / "teste_congelado" / "perguntas_respostas_mia_2026_2.md").read_text(encoding="utf-8")
casos = []
for bloco in re.split(r"\n### ", texto)[1:]:
    cab = bloco.splitlines()[0]
    campo = lambda n: (re.search(rf"\*\*{n}:\*\*\s*(.+)", bloco) or [None, ""])[1].strip()
    casos.append({"id": cab.split(" — ")[0], "nivel": cab.split(" — ")[1].split(" |")[0],
                  "pergunta": campo("Pergunta"), "esperada": campo("Resposta esperada"),
                  "termos": [t.strip() for t in campo("Termos esperados").split(",") if t.strip()]})

s = Servico(replace(carregar_config(), registros_dir=Path(tempfile.mkdtemp())))
saida = []
for n, c in enumerate(casos):
    ini = time.time(); sess = f"mia-{n}"
    r = s.motor.responder(c["pergunta"], sess); perguntou = None
    if r.modo == "esclarecimento":
        perguntou = r.resposta
        resp = "graduação" if "pos-graduacao" in norm(r.resposta) or "graduacao" in norm(r.resposta) and "pos" in norm(r.resposta) else "presencial"
        if "ead" in norm(c["pergunta"]): resp = "EaD"
        r = s.motor.responder(resp, sess)
    t = norm(r.resposta)
    achados = [x for x in c["termos"] if norm(x) in t]
    c.update(resposta=r.resposta, modo=r.modo, esclarecimento=perguntou, segundos=round(time.time() - ini, 1),
             termos_achados=len(achados), termos_total=len(c["termos"]))
    saida.append(c)
    print(f"{c['id']} [{r.modo}] termos {len(achados)}/{len(c['termos'])} {c['segundos']}s", flush=True)
destino = RAIZ / "avaliacao" / "resultados" / "teste_mia_2026_2.json"
destino.write_text(json.dumps(saida, ensure_ascii=False, indent=1), encoding="utf-8")
print("salvo em", destino)
