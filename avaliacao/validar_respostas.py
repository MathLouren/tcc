"""Validação das respostas com o modelo real, contra um gabarito verificado nos documentos.

Uso, na pasta do projeto e com o ambiente virtual ativo:
    python avaliacao/validar_respostas.py                       # modelo e base do .env
    python avaliacao/validar_respostas.py --modelo gemma3:4b    # compara outro modelo
    python avaliacao/validar_respostas.py --base CAMINHO        # base de teste (documentos/, manifesto.json, indice/)
    python avaliacao/validar_respostas.py --gabarito avaliacao/gabarito_ampliado.json   # gabarito ampliado

Os registros de conversa vão para uma pasta temporária: os logs reais não são alterados.
Resultado: avaliacao/resultados/validacao_<rotulo>.json e um resumo no terminal.

O gabarito foi conferido no Manual do Aluno 2026.2 e na matriz 109AN. Ao trocar documentos,
revise as perguntas e os termos esperados abaixo.
Quando o chatbot pergunta "presencial ou EaD?", o script responde como aluno de graduação presencial.
O modelo varia um pouco entre execuções: compare rodadas repetidas antes de concluir que algo mudou.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
import unicodedata
from dataclasses import replace
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import carregar_config  # noqa: E402
from app.servico import Servico  # noqa: E402

# pergunta, grupos esperados (todos os grupos; qualquer termo do grupo), termos proibidos
RESPONDIVEIS = [
    ("qual a media pra passar direto?", [["7,0", "sete"]], ["inferior a 7"]),
    ("Com que média passo direto sem VS?", [["7,0", "sete"]], ["inferior a 7"]),
    ("e se eu ficar com 5 de media?", [["vs", "verificacao suplementar"]], ["reprovado"]),
    ("O que acontece se a minha média ficar entre 4 e 7?", [["vs", "verificacao suplementar"]], ["reprovado"]),
    ("tem segunda chamada da vs?", [["nao havera", "nao ha", "nao existe", "nao tem", "nao,", "nao."]], ["localizei"]),
    ("Há segunda chamada para R1?", [["nao havera", "nao ha", "nao existe", "nao tem", "nao,", "nao."]], ["localizei"]),
    ("quantas faltas posso ter?", [["75", "25%", "25 %"]], []),
    ("Qual a frequência mínima para não reprovar?", [["75"]], []),
    ("quanto preciso tirar na vs pra passar?", [["5,0", "cinco"]], []),
    ("Qual é a nota mínima da VS?", [["5,0", "cinco"]], []),
    ("com que media eu reprovo direto?", [["4,0", "quatro"]], []),
    ("qual o prazo pra pedir segunda chamada?", [["2 (dois)", "2 dias", "dois dias"]], []),
    ("como faço pra pedir segunda chamada?", [["espaco do aluno", "requerimento", "protocol", "recurso academico"]], []),
    ("o que é AT?", [["trabalho"]], []),
    ("o que é AF no ead?", [["formativa"]], []),
    ("como funciona a média no ead?", [["ap+af+at", "ap + af + at", "soma"]], []),
    ("com que media passo direto no ead?", [["6,0", "seis"]], []),
    ("Qual a média final mínima depois da AS no EaD?", [["6,0", "seis"]], []),
    ("como tranco a matricula?", [["requer", "espaco do aluno", "secretaria"]], []),
    ("posso trancar se ja estiver reprovado por falta?", [["nao"]], []),
    ("como peço revisao da minha prova?", [["5 (cinco)", "5 dias", "cinco dias", "secretaria"]], []),
    ("Como é calculada a média final de quem faz VS?", [["ms + vs", "ms+vs", "media aritmetica"]], []),
    ("qual o endereço da universo em são gonçalo?", [["lambari"]], []),
    ("tem whatsapp pra falar com a faculdade?", [["mensagem instantanea", "[tel]"]], ["localizei"]),
    ("até quando tenho que entregar as atividades complementares?", [["penultimo"]], []),
    ("o nivelamento conta como atividade complementar?", [["20"]], []),
    ("como faço pra ser monitor?", [["processo seletivo", "selecao", "seletivo"]], []),
    ("quais as formas de entrar na universo?", [["enem", "exame nacional"], ["vestibular"]], []),
    ("posso colar grau em outra data?", [["requerimento"]], []),
    ("O que é dilação de prazo?", [["50%", "deficiencia"]], []),
    ("O que é a CPA?", [["comissao propria"]], []),
    ("O que é a VT?", [["testes", "trabalhos"]], []),
    ("quais as materias do 5 periodo?", [["inteligencia artificial"], ["grafos"]], []),
    ("qual o pre requisito do tcc 2?", [["n535"]], []),
    ("banco de dados é de qual periodo?", [["3"]], []),
]
# Dados ausentes da base ou fora do escopo: o chatbot deve recusar sem inventar datas.
SEM_RESPOSTA = [
    "Qual o prazo para entregar as ats?",
    "quando começam as aulas?",
    "Quando começam as aulas dos veteranos em 2026.2?",
    "Quando termina o semestre 2026.2?",
    "Quando ocorre o vestibular de Medicina 2027.1?",
    "Qual é o período do recesso de fim de ano?",
    "Qual é o prazo final para trancamento de matrícula?",
    "Quantos dias letivos há em agosto?",
    "Qual é a senha do Wi-Fi do campus?",
    "Qual é o salário dos professores da UNIVERSO?",
    "Quem ganhou a Copa do Mundo de 2022?",
    "Me passa uma receita de bolo de chocolate.",
]
MARCAS_ABSTENCAO = ("nao localizei", "nao esta especificad", "nao e especificad", "nao especifica", "nao encontrei", "nao consta", "nao foi possivel", "nao ha informac", "nao tenho", "nao possuo")
RE_DATA = re.compile(
    r"\b\d{1,2}\s*(?:de\s+(?:janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)|/\d{1,2})\b"
)


def norm(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto).lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c)).replace("0800 721 0251", "[tel]")
    return " ".join(texto.split())


def avaliar_respondivel(motor, n: int, pergunta: str, grupos, proibidos) -> dict:
    inicio = time.time()
    sessao = f"validacao-r{n}"
    resposta = motor.responder(pergunta, sessao)
    perguntou = resposta.modo == "esclarecimento"
    if perguntou:
        resposta_aluno = "graduação" if "pos-graduacao" in norm(resposta.resposta) else "presencial"
        resposta = motor.responder(resposta_aluno, sessao)
    texto = norm(resposta.resposta)
    tem = all(any(norm(t) in texto for t in g) for g in grupos)
    absteve = any(m in texto for m in MARCAS_ABSTENCAO) or resposta.modo == "sem_resultado"
    proibido = any(norm(p) in texto for p in proibidos)
    if resposta.modo == "esclarecimento":
        tipo = "perguntou"
    elif tem and not absteve and not proibido:
        tipo = "certo"
    else:
        tipo = "recusou" if absteve else "errado"
    return {"grupo": "respondivel", "pergunta": pergunta, "tipo": tipo, "perguntou": perguntou,
            "modo": resposta.modo, "segundos": round(time.time() - inicio, 1), "resposta": resposta.resposta}


def avaliar_sem_resposta(motor, n: int, pergunta: str) -> dict:
    inicio = time.time()
    resposta = motor.responder(pergunta, f"validacao-s{n}")
    texto = norm(resposta.resposta)
    absteve = any(m in texto for m in MARCAS_ABSTENCAO) or resposta.modo in ("sem_resultado", "esclarecimento")
    tipo = "certo" if absteve and not RE_DATA.search(texto) else "inventou"
    return {"grupo": "sem_resposta", "pergunta": pergunta, "tipo": tipo, "modo": resposta.modo,
            "segundos": round(time.time() - inicio, 1), "resposta": resposta.resposta}


def avaliar_conversa(motor, n: int, conversa: dict) -> dict:
    """Vários turnos na mesma sessão; só a resposta ao último turno é avaliada."""
    inicio = time.time()
    sessao = f"validacao-c{n}"
    resposta = None
    for turno in conversa["turnos"]:
        resposta = motor.responder(turno, sessao)
    texto = norm(resposta.resposta)
    absteve = any(m in texto for m in MARCAS_ABSTENCAO) or resposta.modo in ("sem_resultado", "esclarecimento")
    if conversa.get("abstencao"):
        tipo = "certo" if absteve and not RE_DATA.search(texto) else "inventou"
    else:
        tem = all(any(norm(t) in texto for t in g) for g in conversa["grupos"])
        tipo = "certo" if tem and not absteve else ("recusou" if absteve else "errado")
    return {"grupo": "conversa", "pergunta": " -> ".join(conversa["turnos"]), "tipo": tipo, "modo": resposta.modo,
            "segundos": round(time.time() - inicio, 1), "resposta": resposta.resposta}


def carregar_gabarito(caminho: str) -> tuple[list, list, list]:
    dados = json.loads(Path(caminho).read_text(encoding="utf-8"))
    respondiveis = [(i["pergunta"], i["grupos"], i.get("proibidos", [])) for i in dados.get("respondiveis", [])]
    return respondiveis, dados.get("sem_resposta", []), dados.get("conversas", [])


def main() -> None:
    analisador = argparse.ArgumentParser(description="Validação das respostas com o modelo real.")
    analisador.add_argument("--gabarito", help="arquivo JSON com respondiveis, sem_resposta e conversas (padrão: o deste script)")
    analisador.add_argument("--modelo", help="modelo do Ollama (padrão: LLM_MODELO do .env)")
    analisador.add_argument("--base", help="pasta com documentos/, manifesto.json e indice/ (padrão: a do projeto)")
    analisador.add_argument("--top-k", type=int, help="trechos enviados ao modelo (padrão: parametros.json)")
    analisador.add_argument("--peso-lexical", type=float, help="peso da busca lexical; 0 = só vetorial (padrão: parametros.json)")
    analisador.add_argument("--rotulo", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    args = analisador.parse_args()

    config = replace(carregar_config(), registros_dir=Path(tempfile.mkdtemp(prefix="validacao_")))
    if args.modelo:
        config = replace(config, llm_modelo=args.modelo)
    if args.base:
        base = Path(args.base)
        config = replace(config, docs_dir=base / "documentos", manifesto=base / "manifesto.json", indice_dir=base / "indice")
    servico = Servico(config)
    if args.top_k:
        servico.motor.parametros = replace(servico.motor.parametros, top_k=args.top_k)
    if args.peso_lexical is not None:
        servico.motor.parametros = replace(servico.motor.parametros, peso_lexical=args.peso_lexical)
    if not servico.llm.disponivel():
        sys.exit(f"Modelo {config.llm_modelo} indisponível no Ollama ({config.ollama_url}).")

    respondiveis, sem_resposta, conversas = RESPONDIVEIS, SEM_RESPOSTA, []
    if args.gabarito:
        respondiveis, sem_resposta, conversas = carregar_gabarito(args.gabarito)

    inicio = time.time()
    linhas = [avaliar_respondivel(servico.motor, n, *caso) for n, caso in enumerate(respondiveis)]
    linhas += [avaliar_sem_resposta(servico.motor, n, p) for n, p in enumerate(sem_resposta)]
    linhas += [avaliar_conversa(servico.motor, n, c) for n, c in enumerate(conversas)]

    pasta = RAIZ / "avaliacao" / "resultados"
    pasta.mkdir(parents=True, exist_ok=True)
    saida = pasta / f"validacao_{args.rotulo}.json"
    saida.write_text(json.dumps({"modelo": config.llm_modelo, "linhas": linhas}, ensure_ascii=False, indent=1), encoding="utf-8")

    resp = [l for l in linhas if l["grupo"] == "respondivel"]
    sem = [l for l in linhas if l["grupo"] == "sem_resposta"]
    conv = [l for l in linhas if l["grupo"] == "conversa"]

    def contar(itens: list[dict], tipo: str) -> int:
        return sum(1 for l in itens if l["tipo"] == tipo)

    print(f"Modelo {config.llm_modelo}: certas {contar(resp, 'certo')}/{len(resp)} "
          f"(recusou {contar(resp, 'recusou')}, errou {contar(resp, 'errado')}) | "
          f"recusas corretas {contar(sem, 'certo')}/{len(sem)} | conversas {contar(conv, 'certo')}/{len(conv)} | perguntas ao aluno {sum(l['perguntou'] for l in resp)} | "
          f"tempo médio {sum(l['segundos'] for l in linhas) / len(linhas):.1f}s | "
          f"mais lenta {max(l['segundos'] for l in linhas):.1f}s | total {time.time() - inicio:.0f}s")
    for l in linhas:
        if l["tipo"] != "certo":
            print(f"  [{l['tipo']}] {l['pergunta']}\n      {l['resposta'][:300]!r}")
    print(f"Detalhes: {saida}")


if __name__ == "__main__":
    main()
