"""Avaliação das respostas por LLM-juiz, com dois modelos de famílias diferentes.

Uso, na pasta do projeto e com o ambiente virtual ativo (o Ollama precisa estar em execução):
    python avaliacao/llm_juiz.py avaliacao/resultados/avaliacao_ROTULO.json [MODELO ...]

Cada juiz recebe a pergunta, a resposta esperada, a resposta do chatbot e os trechos que o chatbot
usou, e devolve em JSON:
- correcao: correta, parcial ou incorreta, em relação à resposta esperada;
- fidelidade: sim ou nao, conforme as afirmações da resposta estejam sustentadas pelos trechos.
Usar dois juízes de famílias diferentes, e medir a concordância entre eles (kappa de Cohen), reduz
o viés de um modelo que avalia as próprias respostas.
Saída: avaliacao/resultados/juiz_ROTULO.json
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
from app.config import carregar_config  # noqa: E402

JUIZES_PADRAO = ["qwen2.5:7b", "gemma3:4b"]
MAX_CARACTERES_DE_TRECHOS = 6000

INSTRUCOES = """Você é um avaliador rigoroso de um chatbot acadêmico. Compare a RESPOSTA DO CHATBOT com a \
RESPOSTA ESPERADA para a PERGUNTA e com os TRECHOS que o chatbot recebeu dos documentos oficiais.

Critério "correcao":
- "correta": traz a informação essencial da resposta esperada, sem contradizê-la. Detalhes extras corretos não penalizam.
- "parcial": acerta parte da informação essencial, mas omite algo importante ou contém imprecisão menor.
- "incorreta": contradiz a resposta esperada, traz informação errada ou se recusa a responder quando a resposta esperada existe.
Quando a resposta esperada diz que a informação não consta dos documentos, a resposta é "correta" se o chatbot \
informar que não localizou a informação, sem inventar, e "incorreta" se inventar um dado.

Critério "fidelidade":
- "sim": todas as afirmações factuais da resposta estão sustentadas pelos TRECHOS, ou a resposta é uma recusa.
- "nao": há ao menos uma afirmação factual que os TRECHOS não sustentam.

Responda somente com JSON no formato: {"correcao": "correta|parcial|incorreta", "fidelidade": "sim|nao", \
"justificativa": "uma frase"}"""


def montar_mensagem(caso: dict) -> str:
    trechos = "\n\n".join(f"[{t['documento']} – {t['secao']}]\n{t['texto']}" for t in caso["trechos_usados"])
    if len(trechos) > MAX_CARACTERES_DE_TRECHOS:
        trechos = trechos[:MAX_CARACTERES_DE_TRECHOS] + "\n[...]"
    return (f"PERGUNTA: {caso['pergunta']}\n\nRESPOSTA ESPERADA: {caso['esperada']}\n\n"
            f"RESPOSTA DO CHATBOT: {caso['resposta']}\n\nTRECHOS:\n{trechos or '(nenhum trecho)'}")


def julgar(cliente: httpx.Client, url: str, modelo: str, caso: dict) -> dict:
    corpo = {"model": modelo, "stream": False, "format": "json",
             "options": {"temperature": 0, "num_ctx": 8192, "seed": 42},
             "messages": [{"role": "system", "content": INSTRUCOES}, {"role": "user", "content": montar_mensagem(caso)}]}
    for tentativa in range(3):
        try:
            texto = cliente.post(f"{url}/api/chat", json=corpo, timeout=300).json()["message"]["content"]
            veredito = json.loads(texto)
            correcao = str(veredito.get("correcao", "")).strip().lower()
            fidelidade = str(veredito.get("fidelidade", "")).strip().lower().replace("ã", "a")
            if correcao in {"correta", "parcial", "incorreta"} and fidelidade in {"sim", "nao"}:
                return {"correcao": correcao, "fidelidade": fidelidade, "justificativa": veredito.get("justificativa", "")}
        except (httpx.HTTPError, json.JSONDecodeError, KeyError, TypeError):
            time.sleep(2)
    return {"correcao": None, "fidelidade": None, "justificativa": "veredito inválido após 3 tentativas"}


def kappa(a: list[str], b: list[str]) -> float | None:
    """Kappa de Cohen entre dois avaliadores: concordância corrigida pela concordância ao acaso."""
    pares = [(x, y) for x, y in zip(a, b) if x and y]
    if not pares:
        return None
    n = len(pares)
    observada = sum(x == y for x, y in pares) / n
    ca, cb = Counter(x for x, _ in pares), Counter(y for _, y in pares)
    esperada = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    return round((observada - esperada) / (1 - esperada), 4) if esperada < 1 else 1.0


def resumo(casos: list[dict], modelo: str) -> dict:
    vereditos = [c["juizes"][modelo] for c in casos if c["juizes"][modelo]["correcao"]]
    pontos = {"correta": 1.0, "parcial": 0.5, "incorreta": 0.0}
    return {
        "avaliadas": len(vereditos),
        "correta": sum(v["correcao"] == "correta" for v in vereditos),
        "parcial": sum(v["correcao"] == "parcial" for v in vereditos),
        "incorreta": sum(v["correcao"] == "incorreta" for v in vereditos),
        "taxa_correta": round(sum(v["correcao"] == "correta" for v in vereditos) / len(vereditos), 4) if vereditos else None,
        "escore_correcao": round(sum(pontos[v["correcao"]] for v in vereditos) / len(vereditos), 4) if vereditos else None,
        "fidelidade": round(sum(v["fidelidade"] == "sim" for v in vereditos) / len(vereditos), 4) if vereditos else None,
    }


def main() -> None:
    entrada = Path(sys.argv[1])
    juizes = sys.argv[2:] or JUIZES_PADRAO
    dados = json.loads(entrada.read_text(encoding="utf-8"))
    casos = dados["casos"]
    url = carregar_config().ollama_url

    with httpx.Client() as cliente:
        for modelo in juizes:
            inicio = time.time()
            for c in casos:
                c.setdefault("juizes", {})[modelo] = julgar(cliente, url, modelo, c)
            print(f"{modelo}: {len(casos)} vereditos em {time.time() - inicio:.0f}s", flush=True)

    resultado = {"juizes": juizes, "geral": {}, "por_nivel": defaultdict(dict), "por_tema": defaultdict(dict)}
    for modelo in juizes:
        resultado["geral"][modelo] = resumo(casos, modelo)
        for nivel in sorted({c["nivel"] for c in casos}):
            resultado["por_nivel"][nivel][modelo] = resumo([c for c in casos if c["nivel"] == nivel], modelo)
        for tema in sorted({c["tema"] for c in casos}):
            resultado["por_tema"][tema][modelo] = resumo([c for c in casos if c["tema"] == tema], modelo)
    if len(juizes) >= 2:
        a, b = juizes[:2]
        resultado["concordancia"] = {
            "kappa_correcao": kappa([c["juizes"][a]["correcao"] for c in casos], [c["juizes"][b]["correcao"] for c in casos]),
            "kappa_fidelidade": kappa([c["juizes"][a]["fidelidade"] for c in casos], [c["juizes"][b]["fidelidade"] for c in casos]),
            "concordancia_simples_correcao": round(
                sum(c["juizes"][a]["correcao"] == c["juizes"][b]["correcao"] for c in casos) / len(casos), 4),
        }
    destino = entrada.with_name(entrada.name.replace("avaliacao_", "juiz_"))
    destino.write_text(json.dumps({"resultado": resultado, "casos": casos}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(resultado["geral"], ensure_ascii=False, indent=1))
    print(json.dumps(resultado.get("concordancia"), ensure_ascii=False))
    print("salvo em", destino)


if __name__ == "__main__":
    main()
