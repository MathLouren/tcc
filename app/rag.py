"""Motor RAG: recuperação, filtro de relevância, montagem do prompt e geração (seção 3.6 do TCC)."""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import numpy as np

from .config import MENSAGEM_NAO_LOCALIZADA, REGRAS_RESPOSTA, Parametros
from .llm import LLMIndisponivel
from .matriz import blocos_da_matriz, responder_consulta_matriz

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
_RE_ASSUNTO_AVALIACAO = re.compile(
    r"\b(prova|provas|avaliacao|avaliacoes|v1|v2|vs|r1|r2|segunda chamada)\b"
)
_RE_CONSULTA_TEMPORAL = re.compile(
    r"\b(quando|qual data|quais datas|data|datas|dia|periodo|comeca|comecam|inicio|termina|terminam)\b"
)
_RE_TIPO_AVALIACAO = re.compile(r"\b(v1|v2|vs|r1|r2|segunda chamada|todas as (?:provas|avaliacoes))\b")
_RE_MODALIDADE = re.compile(r"\b(presencial|presenciais|ead|a distancia|online)\b")
_RE_DEPENDE_CURSO = re.compile(
    r"\b(grade|matriz curricular|disciplina|disciplinas|carga horaria|duracao do curso|"
    r"coordenador|coordenacao|estagio|tcc|atividade complementar)\b"
)
_RE_SEMESTRE = re.compile(r"\b20\d{2}\s*[./-]?\s*[12]\b|\b[12][ºo]?\s*semestre\b")

# Pedidos de dado feitos pelo modelo: só o sistema pede esclarecimentos ao aluno.
_RE_PEDIDO_DO_MODELO = re.compile(
    r"por favor,? informe|poderia,?(?: por favor,?)? (?:informar|especificar|me dizer)|pode (?:informar|me dizer)|"
    r"informe (?:o |a |sua |seu )|qual (?:e |eh )?(?:o |a )?(?:seu|sua)\b|quais sao (?:o |a )?(?:seu|sua)\b|"
    r"me (?:diga|informe)\b|preciso (?:saber|de mais informac)|especifique\b|para qual .* voce se refere|"
    r"a qual .* voce se refere"
)
_LIMITE_PERGUNTA_CURTA = 220
_INSTRUCAO_SEM_PERGUNTAS = (
    "Responda diretamente com base nos trechos, sem fazer perguntas e sem pedir nenhum dado. "
    f"Se a informação não estiver nos trechos, escreva apenas: {MENSAGEM_NAO_LOCALIZADA}"
)

_SIGLAS_ACADEMICAS = {"AF", "AT", "AP", "AS", "V1", "V2", "VS", "R1", "R2"}
# Frases que restringem ou excluem um caso da regra geral (texto já normalizado, sem acentos).
_RE_RESTRICAO = re.compile(
    r"\b(nao havera|nao sera|nao serao|nao podera|nao poderao|nao e permitid\w*|nao sao permitid\w*|"
    r"nao e obrigatori\w*|nao se aplica\w*|nao isenta|nao substitui|nao e concedid\w*|exceto|salvo|"
    r"vedad\w*|proibid\w*|em nenhuma hipotese)\b"
)
_COBERTURA_MINIMA_DA_RESTRICAO = 0.5  # parcela dos termos da pergunta que a frase de restrição precisa ter
_MAX_RESTRICOES = 2
# Avaliações exclusivas de cada modalidade: a sigla já define a modalidade.
_SIGLAS_EAD = {"AF", "AT", "AP", "AS"}
_SIGLAS_PRESENCIAL = {"V1", "V2", "VS", "R1", "R2"}
_PLURAIS_DE_SIGLAS = {"AFS": "AF", "ATS": "AT", "APS": "AP"}
_MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)

PERGUNTAS_ESCLARECIMENTO = {
    "modalidade": "Você está se referindo às disciplinas presenciais ou à modalidade EaD?",
    "avaliacao": "Qual avaliação você quer consultar: V1, V2, VS, segunda chamada ou todas?",
    "curso": "Qual é o seu curso?",
    "semestre": "A qual semestre e ano você se refere? Por exemplo, 2026.2.",
    "nivel": "Sua dúvida é sobre a graduação ou a pós-graduação?",
}

# O modelo pode devolver uma pergunta ao aluno apenas neste formato; o código valida antes de aceitar.
MARCADOR_ESCLARECIMENTO = "ESCLARECIMENTO:"
_RE_PEDIDO_FORMAL = re.compile(rf"^\s*\**\s*{MARCADOR_ESCLARECIMENTO}\s*\**\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_RE_EAD = re.compile(r"\b(ead|a distancia|educacao a distancia|ava)\b")
_RE_PRESENCIAL = re.compile(r"\bpresencia(?:l|is)\b")
_RE_POS = re.compile(r"\b(pos-graduacao|pos graduacao|lato sensu|stricto sensu|especializacao|mestrado|doutorado)\b")
_RE_NIVEL_NA_PERGUNTA = re.compile(r"\b(graduacao|pos|lato|stricto|especializacao|mestrado|doutorado|bacharelado|licenciatura)\b")

# Saudações e agradecimentos isolados recebem resposta direta, sem busca nem modelo.
_RE_SAUDACAO = re.compile(
    r"^(oi+|ola|ola tudo bem|oi tudo bem|oi tudo bom|bom dia|boa tarde|boa noite|e ai|eai|eae|hey|"
    r"(?:oi|ola),? (?:bom dia|boa tarde|boa noite)|(?:oi|ola),? tudo (?:bem|bom)|tudo bem|tudo bom)$"
)
_RE_AGRADECIMENTO = re.compile(
    r"^((?:muito )?obrigad[oa]s?(?: pela ajuda| mesmo)?|valeu|vlw|brigad[oa]|obg|agradeco|ok,? obrigad[oa])$"
)
_RE_DESPEDIDA = re.compile(r"^(tchau|ate mais|ate logo|ate breve|falou|flw|adeus)$")
_RE_IDENTIDADE = re.compile(
    r"^(quem e voce|o que voce faz|o que voce e|o que e voce|voce e um robo|com quem (?:eu )?(?:falo|estou falando)|"
    r"qual (?:e )?(?:o )?seu nome|como voce (?:pode me ajudar|funciona)|em que voce pode (?:me )?ajudar|"
    r"o que voce sabe|ajuda|menu)$"
)

# Pergunta que só faz sentido com a anterior: "e no ead?", "qual a nota mínima nela?".
_RE_MARCA_DE_CONTINUACAO = re.compile(
    r"^(?:e|mas|entao|ok e|certo e|e se|e no|e na|e para|e pra)\b|"
    r"\b(nela|nele|nelas|neles|dela|dele|delas|deles|disso|nisso|isso|nesse caso|neste caso|esse prazo|essa prova)\b"
)
_LIMITE_PALAVRAS_CONTINUACAO = 8
_RE_NUMERO_SOLTO = re.compile(r"\b([1-9])\b")

# Verificação de números: valores da resposta precisam existir nos trechos ou na pergunta.
_RE_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")
_RE_MARCADOR_DE_LISTA = re.compile(r"^\s*(?:[-*•]\s*)?\(?\d{1,2}[.)]\s+", re.MULTILINE)
_NUMEROS_POR_EXTENSO = {
    "um": "1", "uma": "1", "dois": "2", "duas": "2", "tres": "3", "quatro": "4", "cinco": "5", "seis": "6",
    "sete": "7", "oito": "8", "nove": "9", "dez": "10", "quinze": "15", "vinte": "20", "trinta": "30",
    "quarenta": "40", "cinquenta": "50", "sessenta": "60", "noventa": "90", "cem": "100", "primeira": "1",
    "primeiro": "1", "segunda": "2", "segundo": "2", "terceira": "3", "terceiro": "3", "metade": "50", "meio": "5",
}
_INSTRUCAO_SO_NUMEROS_DOS_TRECHOS = (
    "Atenção: use somente datas e valores em reais que aparecem escritos nos trechos. "
    f"Se o valor pedido não estiver nos trechos, escreva apenas: {MENSAGEM_NAO_LOCALIZADA}"
)
RESPOSTA_IDENTIDADE = (
    "Sou o assistente virtual da UNIVERSO para os alunos do campus São Gonçalo. Respondo com base no Manual "
    "do Aluno e na matriz curricular: matrícula, trancamento, avaliações, médias, segunda chamada, estágio, "
    "TCC, atividades complementares, biblioteca e outros serviços. Qual é a sua dúvida?"
)
_MAX_RESPOSTAS_EM_CACHE = 500

# Como o aluno fala -> como os documentos escrevem. O modelo recebe a equivalência junto com a pergunta:
# sem ela, lê "Não haverá 2ª chamada" e responde que não achou nada sobre "segunda chamada".
_EQUIVALENCIAS = (
    (r"\bsegunda chamada\b", '"segunda chamada" aparece como "2ª chamada"'),
    (r"\bfaltas?\b|\bfaltar\b", '"faltas" aparecem como "frequência" (reprovar por falta = reprovado por frequência)'),
    (r"\bmaterias?\b|\bcadeiras?\b", '"matéria" aparece como "disciplina"'),
    (r"\bprovas?\b", '"prova" aparece como "avaliação" ou "verificação"'),
    (r"\bmensalidades?\b|\bboletos?\b", '"mensalidade" aparece como "parcela" ou "semestralidade"'),
    (r"\bdevendo\b|\bdivida\b|\binadimplente\b", '"dever mensalidade" aparece como "débito" ou "inadimplemento"'),
    (r"\bformatura\b|\bme formar\b", '"formatura" aparece como "colação de grau" e "tempo de curso" como "integralização"'),
    (r"\bfaculdade\b", '"faculdade" é a "universidade" ou "instituição"'),
    (r"\brecuperacao\b", '"recuperação" aparece como "R1" ou "Verificação Suplementar (VS)"'),
    (r"\bexpuls\w+\b", '"expulsão" aparece como "desligamento"'),
    (r"\bgravid\w+\b", '"grávida" aparece como "gestante"'),
)
_LINHAS_NO_FOCO = 8
# Segunda leitura quando o modelo recusa: ele só aponta a frase; quem responde é o texto do documento.
# Modelos pequenos acham a frase certa com muito mais acerto do que decidem se ela "responde" à pergunta.
_SISTEMA_CITACAO = (
    "Abaixo estão linhas de documentos oficiais da UNIVERSO. Copie literalmente, sem alterar nenhuma palavra, "
    "a única linha ou frase que responde à pergunta do aluno ou que trata exatamente do mesmo assunto.\n"
    "Use exatamente este formato:\nCITAÇÃO: <frase copiada>\n"
    "Se nenhuma linha tratar do assunto da pergunta, escreva apenas: CITAÇÃO: NENHUMA"
)
_RE_CITACAO = re.compile(r"CITA[ÇC][ÃA]O:\s*(.+)", re.IGNORECASE | re.DOTALL)
_COBERTURA_MINIMA_DO_ACERVO = 0.6   # abaixo disso a pergunta fala de algo que a base não tem: não há segunda leitura
_COBERTURA_MINIMA_DA_CITACAO = 0.69 # a frase citada precisa conter quase todos os termos da pergunta
_SEMELHANCA_MINIMA_DA_CITACAO = 0.8
# Frase que o modelo citou literalmente (existe nos trechos): o modelo já afirmou que ela responde, então
# basta uma cobertura menor. Ex.: "posso trancar devendo mensalidade?" -> "O inadimplemento financeiro não
# impede a concessão de trancamento" (cobertura 0,56: a frase não repete "matrícula" nem "mensalidade").
_COBERTURA_MINIMA_DA_CITACAO_LITERAL = 0.5
INTRO_CITACAO = "Sobre esse assunto, o documento oficial diz:"


def _dica_de_equivalencias(pergunta: str) -> str:
    normalizada = _normalizar(pergunta)
    dicas = [texto for padrao, texto in _EQUIVALENCIAS if re.search(padrao, normalizada)]
    return f"(Nos documentos, {'; '.join(dicas)}.)" if dicas else ""


_RE_SIGLA_DEPOIS_DO_NOME = re.compile(r"((?:[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wçãõáéíóúâêô]+(?:\s+(?:de|da|do|e|por|em|no|na|a|ao)\b)*\s+){1,6})\(([A-Z]{2,6})\)")
_RE_NOME_DEPOIS_DA_SIGLA = re.compile(r"\b([A-Z]{2,6})\s+\(((?:[A-ZÁÉÍÓÚÂÊÔÃÕÇ][\wçãõáéíóúâêô]+\s*(?:(?:de|da|do|e|por|em|no|na|a|ao)\s+)*){1,6})\)")


def glossario_de_siglas(blocos: list[dict]) -> dict[str, str]:
    """Siglas definidas nos próprios documentos: "Avaliação por Trabalho (AT)", "VS (Verificação Suplementar)".

    Só entra a definição cujas iniciais formam a sigla, para não inventar significados.
    """
    def iniciais(nome: str) -> str:
        return "".join(palavra[0] for palavra in nome.split() if palavra[0].isupper()).upper()

    glossario: dict[str, str] = {}
    for bloco in blocos:
        texto = str(bloco.get("texto", ""))
        pares = [(m.group(2), m.group(1)) for m in _RE_SIGLA_DEPOIS_DO_NOME.finditer(texto)]
        pares += [(m.group(1), m.group(2)) for m in _RE_NOME_DEPOIS_DA_SIGLA.finditer(texto)]
        for sigla, nome in pares:
            palavras = nome.split()
            # "prestar a Avaliação Suplementar (AS)": só as últimas palavras cujas iniciais formam a sigla
            for inicio in range(len(palavras)):
                candidato = " ".join(palavras[inicio:])
                if palavras[inicio][0].isupper() and _normalizar(iniciais(candidato)) == sigla.lower():
                    glossario.setdefault(sigla, candidato.strip())
                    break
    return glossario
RESPOSTA_SAUDACAO = (
    "Olá! Sou o assistente virtual da UNIVERSO. Posso ajudar com dúvidas sobre matrícula, avaliações, "
    "médias, segunda chamada, disciplinas, estágio, atividades complementares e serviços do campus "
    "São Gonçalo. Qual é a sua dúvida?"
)
RESPOSTA_AGRADECIMENTO = "Por nada! Se surgir outra dúvida, é só perguntar."
RESPOSTA_DESPEDIDA = "Até mais! Quando precisar, é só voltar aqui."


@dataclass
class Resposta:
    resposta: str
    fontes: list[dict]
    modo: str  # "llm", "validado", "extrativo", "esclarecimento", "sem_resultado" ou "conversa"
    trechos: list[dict]
    tempo_ms: int


@dataclass
class EsclarecimentoPendente:
    pergunta_original: str
    campos: list[str]
    respostas: dict[str, str] = field(default_factory=dict)
    # "codigo" (regras fixas), "recuperacao" (trechos com regras das duas modalidades) ou "modelo" (pedido validado)
    origem: str = "codigo"


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _modelo_pediu_dado(texto: str) -> bool:
    normalizado = _normalizar(texto).strip()
    if _RE_PEDIDO_DO_MODELO.search(normalizado):
        return True
    return normalizado.endswith("?") and len(normalizado) < _LIMITE_PERGUNTA_CURTA


def _interpretar_esclarecimento(campo: str, resposta: str) -> str | None:
    normalizada = _normalizar(resposta).strip()
    if campo == "modalidade":
        presencial = bool(re.search(r"\bpresencia(?:l|is)\b", normalizada))
        ead = bool(re.search(r"\b(ead|a distancia|online)\b", normalizada))
        if presencial != ead:
            return "modalidade presencial" if presencial else "modalidade EaD"
        return None
    if campo == "avaliacao":
        if re.search(r"\b(todas|tudo|todas as provas|todas as avaliacoes)\b", normalizada):
            return "todas as avaliações"
        encontrados = re.findall(r"\b(v1|v2|vs|r1|r2)\b", normalizada)
        if "segunda chamada" in normalizada:
            encontrados.append("segunda chamada")
        return "avaliação " + ", ".join(dict.fromkeys(encontrados)).upper() if encontrados else None
    if campo == "semestre":
        encontrado = _RE_SEMESTRE.search(normalizada)
        return f"semestre {encontrado.group(0)}" if encontrado else None
    if campo == "curso":
        genericas = {"meu curso", "nao sei", "não sei", "curso", "graduacao", "graduação"}
        if normalizada not in {_normalizar(v) for v in genericas} and 2 <= len(resposta.strip()) <= 120:
            return f"curso {resposta.strip()}"
    if campo == "nivel":
        pos = bool(_RE_POS.search(normalizada) or re.search(r"\bpos\b", normalizada))
        graduacao = bool(re.search(r"\b(graduacao|bacharelado|licenciatura|tecnologo)\b", normalizada)) and not pos
        if pos != graduacao:
            return "pós-graduação" if pos else "graduação"
    return None


def _resposta_social(pergunta: str) -> str | None:
    """Resposta para mensagens que são apenas saudação, agradecimento ou despedida."""
    normalizada = re.sub(r"[^\w\s,-]", " ", _normalizar(pergunta))
    normalizada = " ".join(normalizada.replace(",", " ").split())
    if _RE_SAUDACAO.fullmatch(normalizada):
        return RESPOSTA_SAUDACAO
    if _RE_AGRADECIMENTO.fullmatch(normalizada):
        return RESPOSTA_AGRADECIMENTO
    if _RE_DESPEDIDA.fullmatch(normalizada):
        return RESPOSTA_DESPEDIDA
    if _RE_IDENTIDADE.fullmatch(normalizada):
        return RESPOSTA_IDENTIDADE
    return None


def _chave_de_cache(texto: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", _normalizar(texto)).split())


def _numero_canonico(numero: str) -> str:
    """"7,0", "7.0" e "7" viram "7"; "3.000" vira "3000"; "0251" vira "251"."""
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", numero):
        numero = numero.replace(".", "")
    numero = numero.replace(",", ".")
    if "." in numero:
        numero = numero.rstrip("0").rstrip(".")
    return numero.lstrip("0") or "0"


def _numeros_do_texto(texto: str) -> set[str]:
    numeros = {_numero_canonico(n) for n in _RE_NUMERO.findall(texto)}
    for bruto in _RE_NUMERO.findall(texto):  # "10/09" e "1.275" também valem pelas partes
        numeros.update(_numero_canonico(parte) for parte in re.split(r"[.,]", bruto) if parte)
    numeros.update(_NUMEROS_POR_EXTENSO[p] for p in re.findall(r"[a-z]+", _normalizar(texto)) if p in _NUMEROS_POR_EXTENSO)
    return numeros


_RE_DATA_NA_RESPOSTA = re.compile(
    r"\b(\d{1,2})(?:\s*(?:º|°))?\s+de\s+(janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|"
    r"novembro|dezembro)\b|\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b"
)
_RE_VALOR_EM_REAIS = re.compile(r"r\$\s*(\d+(?:[.,]\d+)*)")
_NUMERO_DO_MES = {nome: n for n, nome in enumerate(
    ("janeiro", "fevereiro", "marco", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
     "novembro", "dezembro"), start=1)}


def _datas_e_valores(texto: str) -> set[str]:
    """Datas (dia e mês) e valores em reais escritos no texto, em forma comparável: "14/9", "R$3"."""
    normalizado = _normalizar(texto)
    achados = set()
    for dia, mes, dia_barra, mes_barra in _RE_DATA_NA_RESPOSTA.findall(normalizado):
        if dia:
            achados.add(f"{int(dia)}/{_NUMERO_DO_MES[mes]}")
        elif 1 <= int(mes_barra) <= 12:
            achados.add(f"{int(dia_barra)}/{int(mes_barra)}")
    # "09 a 28 de novembro": o primeiro dia do intervalo também é uma data do texto
    for dia, mes in re.findall(r"\b(\d{1,2})\s*(?:a|e|ate)\s*\d{1,2}\s+de\s+([a-z]+)", normalizado):
        if mes in _NUMERO_DO_MES:
            achados.add(f"{int(dia)}/{_NUMERO_DO_MES[mes]}")
    achados.update(f"R${_numero_canonico(valor)}" for valor in _RE_VALOR_EM_REAIS.findall(normalizado))
    return achados


def _numeros_sem_apoio(resposta: str, apoio: str) -> set[str]:
    """Datas e valores em reais da resposta que não aparecem nos trechos nem na pergunta.

    A verificação é restrita a datas e dinheiro, onde um valor inventado prejudica o aluno. Testada em
    2026-11-09 para qualquer número, recusou respostas corretas: "25% de faltas" (o texto diz 75% de
    frequência), contas pedidas pelo aluno e um CEP com hífen que o PDF traz sem hífen.
    """
    return _datas_e_valores(resposta) - _datas_e_valores(apoio)


def _combinar_com_anterior(anterior: str, pergunta: str) -> str:
    """Junta a pergunta de continuação à anterior, trocando o que a nova pergunta redefine."""
    nova = _normalizar(pergunta)
    if _RE_MODALIDADE.search(nova) or _RE_NIVEL_NA_PERGUNTA.search(nova):
        anterior = re.sub(r"\s*\([^)]*\)\s*$", "", anterior)  # esclarecimento anterior: "(modalidade presencial)"
        anterior = re.sub(r"(?i)\b(?:n[ao]s?\s+)?(?:presencia(?:l|is)|ead|a dist[âa]ncia)\b", "", anterior)
    numero_novo = _RE_NUMERO_SOLTO.search(nova)
    if numero_novo and len(nova.split()) <= 4 and _RE_NUMERO_SOLTO.search(anterior):
        return _RE_NUMERO_SOLTO.sub(numero_novo.group(1), anterior, count=1)  # "e do 4?" depois de "... do 3 periodo?"
    return f"{anterior.strip()} {pergunta}"


_LIMITE_RECUSA_CURTA = 260
DICA_CALENDARIO = (
    "Datas e prazos, como entregas de atividades e períodos de provas, são divulgados no Calendário "
    "Letivo, disponível no Espaço do Aluno."
)
_CANDIDATOS_REORDENACAO = 30
_AJUSTE_MODALIDADE = 0.05
# Assuntos em que presencial e EaD têm regras diferentes (médias, notas, avaliações).
# "reprovado" sozinho não entra: "posso trancar se estiver reprovado por falta?" vale para as duas.
_RE_ASSUNTO_POR_MODALIDADE = re.compile(
    r"\b(media|medias|nota|notas|passar|passo|recuperacao|suplementar|avaliacao|avaliacoes)\b"
)


def _modalidade_do_texto(normalizado: str) -> str | None:
    ead, presencial = bool(_RE_EAD.search(normalizado)), bool(_RE_PRESENCIAL.search(normalizado))
    if ead != presencial:
        return "ead" if ead else "presencial"
    return None


def _escopo_do_bloco(bloco: dict) -> str | None:
    """Modalidade a que o bloco se refere, pelo parágrafo de escopo (§) herdado ou pelo primeiro do texto."""
    escopo = bloco.get("contexto") or ""
    if not escopo:
        texto = str(bloco.get("texto", ""))[:400]
        encontrado = re.search(r"§\s*\d+\s*[ºo°]?\s+(?:Nos?|Nas?|Para)\s[^:;.§]{5,200}", texto)
        escopo = encontrado.group(0) if encontrado else ""
    return _modalidade_do_texto(_normalizar(escopo)) if escopo else None


def _ajuste_de_modalidade(bloco: dict, modalidade: str) -> float:
    escopo = _escopo_do_bloco(bloco)
    if escopo is None:
        return 0.0
    return _AJUSTE_MODALIDADE if escopo == modalidade else -_AJUSTE_MODALIDADE


def _modalidade_ambigua(pergunta: str, trechos: list[dict], blocos_do_indice: list[dict] | None = None) -> bool:
    """A pergunta depende da modalidade, não a informa, e há regras das duas para o mesmo assunto.

    Basta um trecho com escopo de uma modalidade se a mesma seção do índice tiver a regra da outra:
    a busca pode ter trazido só a regra do presencial, e a resposta valeria apenas para esses alunos.
    """
    normalizada = _normalizar(pergunta)
    if not _RE_ASSUNTO_POR_MODALIDADE.search(normalizada) or _RE_MODALIDADE.search(normalizada):
        return False
    if _siglas_da_consulta(pergunta) & (_SIGLAS_EAD | _SIGLAS_PRESENCIAL):
        return False
    escopos = {_escopo_do_bloco(t) for t in trechos} - {None}
    if {"ead", "presencial"} <= escopos:
        return True
    if not escopos or not blocos_do_indice:
        return False
    secoes = {(t.get("arquivo"), t.get("secao")) for t in trechos if _escopo_do_bloco(t)}
    escopos_das_secoes = {
        _escopo_do_bloco(b) for b in blocos_do_indice if (b.get("arquivo"), b.get("secao")) in secoes
    }
    return {"ead", "presencial"} <= escopos_das_secoes


def _padronizar_recusa(texto: str, pergunta: str) -> str | None:
    """Troca a recusa curta do modelo pelo texto padrão, que diz onde o aluno pode buscar a informação."""
    normalizado = _normalizar(texto)
    if "nao localizei" not in normalizado or len(texto) > _LIMITE_RECUSA_CURTA:
        return None
    if _RE_CONSULTA_TEMPORAL.search(_normalizar(pergunta)) or re.search(r"\b(prazo|ate quando)\b", _normalizar(pergunta)):
        return f"{MENSAGEM_NAO_ENCONTRADA} {DICA_CALENDARIO}"
    return MENSAGEM_NAO_ENCONTRADA


def _pedido_formal(texto: str) -> str | None:
    """Extrai a pergunta que o modelo quer fazer ao aluno, quando ele usa o formato combinado."""
    encontrado = _RE_PEDIDO_FORMAL.search(texto)
    return encontrado.group(1).strip() if encontrado else None


def _campo_do_pedido(pergunta: str, pedido: str, trechos: list[dict]) -> str | None:
    """Aceita o pedido do modelo só quando ele é necessário: dado ausente na pergunta e regras diferentes
    nos trechos para cada caso. Curso, semestre e avaliação continuam decididos apenas pelo código."""
    pedido_n, pergunta_n = _normalizar(pedido), _normalizar(pergunta)
    conteudo = _normalizar(" ".join(f"{t.get('secao', '')} {t.get('texto', '')}" for t in trechos))
    if (_RE_EAD.search(pedido_n) or _RE_PRESENCIAL.search(pedido_n) or "modalidade" in pedido_n):
        informado = _RE_MODALIDADE.search(pergunta_n) or _siglas_da_consulta(pergunta) & _SIGLAS_EAD
        if not informado and _RE_EAD.search(conteudo) and _RE_PRESENCIAL.search(conteudo):
            return "modalidade"
        return None
    if "graduacao" in pedido_n:
        graduacao_sem_pos = re.search(r"(?<!pos-)(?<!pos )\bgraduacao\b", conteudo)
        if not _RE_NIVEL_NA_PERGUNTA.search(pergunta_n) and _RE_POS.search(conteudo) and graduacao_sem_pos:
            return "nivel"
    return None


def _siglas_da_consulta(consulta: str) -> set[str]:
    """Extrai siglas acadêmicas e converte plurais informais, como ATs/AFs, para AT/AF."""
    siglas: set[str] = set()
    for token in re.findall(r"\b[A-Za-z]+[0-9]?\b", consulta):
        # Alunos escrevem "ats", "vs", "v1" em minúsculas; "as" minúsculo é artigo e fica de fora.
        if not token.isupper():
            if token.lower() == "as" or token.upper() not in _SIGLAS_ACADEMICAS | set(_PLURAIS_DE_SIGLAS):
                continue
            token = token.upper()
        sigla = _PLURAIS_DE_SIGLAS.get(token, token)
        if sigla in _SIGLAS_ACADEMICAS:
            siglas.add(sigla)
    return siglas


def _expandir_siglas(consulta: str, siglas: set[str]) -> str:
    if not siglas:
        return consulta
    expansoes = " ".join(sorted(siglas))
    normalizada = _normalizar(consulta)
    if _RE_CONSULTA_TEMPORAL.search(normalizada) or re.search(r"\b(prazo|final|fechamento|encerra)\b", normalizada):
        return f"data final fechamento período de realização Avaliações {expansoes} calendário acadêmico"
    return f"{consulta} Avaliações {expansoes} calendário prazo período"


def _contem_siglas(bloco: dict, siglas: set[str]) -> bool:
    conteudo = f"{bloco.get('secao', '')}\n{bloco.get('texto', '')}".upper()
    return all(re.search(rf"\b{re.escape(sigla)}\b", conteudo) for sigla in siglas)


_RE_LINHA_COM_DATA = re.compile(
    r"^[✓•\-\s]*(\d{1,2})(?:\s*(?:a|e)\s*\d{1,2})?(?:\s+de\s+([a-zç]+)(?:\s+de\s+(20\d{2}))?)?\s*[–-]\s*(.+)$",
    re.IGNORECASE,
)
_NOTA =r"(\d{1,2}(?:[,.]\d{1,2})?)(?!\d)"
_CONECTOR_SIGLA_NOTA = r"\s*(?:=|:|igual a|foi|deu|de|com|(?:eu\s+)?(?:tirei|fiz|tive|peguei)(?:\s+nota)?)?\s*"


def _notas_citadas(pergunta: str, siglas: tuple[str, ...]) -> dict[str, Decimal] | None:
    """Notas que o aluno informou, em "9 na V1" ou em "V1 igual a 9" (também "V1 = 9", "V1: 9").

    None quando a mesma avaliação aparece com dois valores diferentes: não adivinhar qual vale.
    """
    alternativas = "|".join(siglas)
    nota_antes = re.compile(rf"(?<![\d.,]){_NOTA}\s*(?:pontos?)?\s*(?:na|no|em|de)\s*({alternativas})\b", re.IGNORECASE)
    sigla_antes = re.compile(rf"\b({alternativas}){_CONECTOR_SIGLA_NOTA}{_NOTA}", re.IGNORECASE)
    pares = [(s, v) for v, s in nota_antes.findall(pergunta)] + sigla_antes.findall(pergunta)
    notas: dict[str, Decimal] = {}
    for sigla, valor in pares:
        sigla, valor = sigla.upper(), Decimal(valor.replace(",", "."))
        if notas.get(sigla, valor) != valor:
            return None
        notas[sigla] = valor
    return notas


def _pt_decimal(valor: Decimal) -> str:
    escrito = format(valor, "f")
    return (escrito.rstrip("0").rstrip(".") if "." in escrito else escrito).replace(".", ",")


def _resposta_factual_de_notas_presenciais(pergunta: str, blocos: list[dict]) -> tuple[str, list[dict]] | None:
    """Calcula a MS somente quando o manual indexado comprova fórmula, limites e modalidade."""
    consulta = _normalizar(pergunta)
    if re.search(r"\b(ead|a distancia|semipresencial)\b", consulta) or not re.search(
        r"\b(media|passei|passo|aprovad[oa]|aprovacao|vs)\b", consulta
    ):
        return None
    notas = _notas_citadas(pergunta, ("V1", "VT", "V2", "R1"))
    if notas is None or not {"V1", "VT", "V2"} <= notas.keys() or any(n < 0 or n > 10 for n in notas.values()):
        return None

    por_secao: dict[tuple[str, str], list[dict]] = {}
    for bloco in blocos:
        por_secao.setdefault((bloco.get("arquivo", ""), bloco.get("secao", "")), []).append(bloco)
    for grupo in por_secao.values():
        def evidencia(frase: str) -> dict | None:
            return next((b for b in grupo if frase in _normalizar(str(b.get("texto", ""))).replace("×", "x")), None)

        escopo = evidencia("nos cursos de graduacao presenciais")
        formula = evidencia("ms = [(v1 x 2) + vt + (v2 x 2)]/5")
        aprovacao = evidencia("media semestral for igual ou superior a 7,0")
        reprovacao = evidencia("ms seja inferior a 4,0")
        suplementar = evidencia("devera prestar a vs")
        if not all((escopo, formula, aprovacao, reprovacao, suplementar)):
            continue
        usados = [escopo, formula, aprovacao, reprovacao, suplementar]
        v1 = notas["V1"]
        detalhe_r1 = ""
        if "R1" in notas:
            elegibilidade = evidencia("r1 sera oferecida aos alunos que obtiverem nota igual ou inferior a 6,0")
            substituicao = evidencia("nota obtida na r1 substituira a nota da v1, desde que seja superior")
            limite_r1 = evidencia("valor maximo da nota da avaliacao r1 sera de 7,0")
            if not all((elegibilidade, substituicao, limite_r1)) or v1 > 6 or notas["R1"] > 7:
                return None
            usados.extend((elegibilidade, substituicao, limite_r1))
            if notas["R1"] > v1:
                v1 = notas["R1"]
                detalhe_r1 = "A R1 substitui a V1 porque a nota foi maior. "
            else:
                detalhe_r1 = "A R1 não substitui a V1 porque a nota não foi maior. "

        ms = (v1 * 2 + notas["VT"] + notas["V2"] * 2) / 5

        if ms >= 7:
            conclusao = "Pelas notas, houve aprovação direta por média."
        elif ms < 4:
            conclusao = "Pelas notas, houve reprovação por média."
        else:
            conclusao = "Pelas notas, não houve aprovação direta; é necessária a VS."
        texto = (
            f"{detalhe_r1}Média Semestral (MS) = [({_pt_decimal(v1)} × 2) + {_pt_decimal(notas['VT'])} + "
            f"({_pt_decimal(notas['V2'])} × 2)] / 5 = {_pt_decimal(ms)}. {conclusao}"
        )
        frequencia = evidencia("frequencia minima de 75%")
        if frequencia:
            texto += " A aprovação também exige frequência mínima de 75%."
            usados.append(frequencia)
        return texto, [dict(b, similaridade=1.0) for b in dict((b["id"], b) for b in usados).values()]
    return None


_RE_MEDIA_SEMESTRAL_INFORMADA = re.compile(
    rf"\b(?:media semestral|media|ms)\s*(?:\(ms\)\s*)?(?:foi|era|deu|ficou(?: em)?|de|=|:|igual a)?\s*{_NOTA}"
    rf"|(?<![\d.,]){_NOTA}\s*(?:de|na|no)\s*(?:media|ms)\b"
)


def _resposta_factual_de_vs(pergunta: str, blocos: list[dict]) -> tuple[str, list[dict]] | None:
    """Resultado da VS (graduação presencial) a partir das notas informadas e das regras indexadas.

    São duas condições, e o modelo costuma aplicar só a média: nota mínima de 5,0 na própria VS
    e MF = (MS + VS)/2 de pelo menos 5,0. Com VS abaixo de 5,0 a resposta não depende da média.
    """
    consulta = _normalizar(pergunta)
    if re.search(r"\b(ead|a distancia|semipresencial|pos|lato|stricto)\b", consulta):
        return None
    notas = _notas_citadas(pergunta, ("VS",))
    if not notas or "VS" not in notas or not 0 <= notas["VS"] <= 10:
        return None
    vs = notas["VS"]
    achada = _RE_MEDIA_SEMESTRAL_INFORMADA.search(consulta.replace("vs", " "))
    ms = Decimal((achada.group(1) or achada.group(2)).replace(",", ".")) if achada else None
    if ms is not None and not 4 <= ms < 7:
        return None  # fora da faixa da VS: a pergunta tem outro problema, que o modelo explica melhor
    if ms is None and vs >= 5:
        return None  # aprovação depende da média, que não foi informada

    por_secao: dict[tuple[str, str], list[dict]] = {}
    for bloco in blocos:
        por_secao.setdefault((bloco.get("arquivo", ""), bloco.get("secao", "")), []).append(bloco)
    for grupo in por_secao.values():
        def evidencia(frase: str) -> dict | None:
            return next((b for b in grupo if frase in _normalizar(str(b.get("texto", "")))), None)

        escopo = evidencia("nos cursos de graduacao presenciais")
        minimo_vs = evidencia("aproveitamento inferior a 5,0 (cinco) na vs")
        media_final = evidencia("(ms + vs)/2 e devera ser igual ou superior a 5,0")
        if not all((escopo, minimo_vs, media_final)):
            continue
        usados = [escopo, minimo_vs, media_final]
        partes = []
        if ms is not None:
            mf = (ms + vs) / 2
            partes.append(
                f"Média Final (MF) = (MS + VS) / 2 = ({_pt_decimal(ms)} + {_pt_decimal(vs)}) / 2 = {_pt_decimal(mf)}."
            )
        if vs < 5:
            partes.append(
                f"Não há aprovação: além da MF de pelo menos 5,0, o manual exige nota mínima de 5,0 na própria VS, "
                f"e a sua foi {_pt_decimal(vs)}."
            )
        elif mf < 5:
            partes.append("Não há aprovação: a MF precisa ser de pelo menos 5,0.")
        else:
            partes.append("Há aprovação: a nota na VS é de pelo menos 5,0 e a MF também.")
            frequencia = evidencia("frequencia minima de 75%")
            if frequencia:
                partes.append("A aprovação também exige frequência mínima de 75%.")
                usados.append(frequencia)
        return " ".join(partes), [dict(b, similaridade=1.0) for b in dict((b["id"], b) for b in usados).values()]
    return None


def _resposta_factual_de_notas_ead(
    pergunta: str, conversa: list[tuple[str, str]], blocos: list[dict]
) -> tuple[str, list[dict]] | None:
    """Calcula MP e a nota mínima na AS a partir das notas do aluno e das regras indexadas."""
    normalizada = _normalizar(pergunta)
    notas_na_pergunta = bool(_notas_citadas(pergunta, ("AP", "AF", "AT")))
    quer_nota_as = bool(re.search(r"\b(?:quanto|quantos|nota minima|qual nota)\b", normalizada)) or bool(
        re.search(r"\btirar quando\b", normalizada) and "aprovad" in normalizada
    )
    menciona_as = "AS" in _siglas_da_consulta(pergunta) or bool(re.search(r"\b(?:na|da|de) as\b", normalizada))
    if not notas_na_pergunta and not (quer_nota_as and menciona_as):
        return None
    if re.search(r"\b(presencial|presenciais)\b", normalizada) and not re.search(r"\b(ead|a distancia)\b", normalizada):
        return None

    notas: dict[str, Decimal] = {}
    perguntas = [pergunta] + ([anterior for anterior, _ in reversed(conversa)] if quer_nota_as else [])
    for texto in perguntas:
        desta_pergunta = _notas_citadas(texto, ("AP", "AF", "AT"))
        if desta_pergunta is None:
            return None
        if {"AP", "AF", "AT"} <= desta_pergunta.keys():
            notas = desta_pergunta
            break
    if not notas and not (quer_nota_as and menciona_as):
        return None
    if notas and not (0 <= notas["AP"] <= 6 and 0 <= notas["AF"] <= 2 and 0 <= notas["AT"] <= 2):
        return None

    por_secao: dict[tuple[str, str], list[dict]] = {}
    for bloco in blocos:
        por_secao.setdefault((bloco.get("arquivo", ""), bloco.get("secao", "")), []).append(bloco)
    for grupo in por_secao.values():
        def evidencia(frase: str) -> dict | None:
            return next((b for b in grupo if frase in _normalizar(str(b.get("texto", "")))), None)

        escopo = evidencia("nos cursos de graduacao na modalidade de educacao a distancia")
        formula = evidencia("mp = (ap+af+at)")
        limite_ap = evidencia("avaliacao presencial (ap)")
        limite_af = evidencia("avaliacao formativa (af)")
        limite_at = evidencia("avaliacao por trabalho (at)")
        aprovacao = evidencia("media parcial for igual ou superior a 6,0")
        reprovacao = evidencia("mp seja inferior a 4,0")
        suplementar = evidencia("igual ou superior a 4,5")
        regra_as = evidencia("(mp + as)/2 e devera ser igual ou superior a 6,0")
        if not all((escopo, formula, limite_ap, limite_af, limite_at, aprovacao, reprovacao, suplementar, regra_as)):
            continue
        usados = [escopo, formula, limite_ap, limite_af, limite_at, aprovacao, reprovacao, suplementar, regra_as]
        if not notas:
            texto = (
                "A nota necessária na AS depende da sua Média Parcial (MP): "
                "MF = (MP + AS) / 2 deve ser pelo menos 6,0. "
                "Informe suas notas de AP, AF e AT para eu calcular quanto você precisa tirar na AS."
            )
            return texto, [dict(b, similaridade=1.0) for b in dict((b["id"], b) for b in usados).values()]
        mp = notas["AP"] + notas["AF"] + notas["AT"]
        conta = (
            f"Média Parcial (MP) = AP + AF + AT = {_pt_decimal(notas['AP'])} + "
            f"{_pt_decimal(notas['AF'])} + {_pt_decimal(notas['AT'])} = {_pt_decimal(mp)}. "
        )
        if mp >= 6:
            conclusao = "Pelas notas, há aprovação direta por média."
        elif mp < 4:
            conclusao = "Pelas notas, há reprovação por média."
        elif mp < Decimal("4.5"):
            conclusao = (
                "O manual não define claramente a situação da faixa de MP entre 4,0 e 4,5; "
                "confirme a regra com a instituição."
            )
        elif quer_nota_as and menciona_as:
            minima = 12 - mp
            conclusao = (
                f"Para alcançar MF = (MP + AS) / 2 ≥ 6,0, você precisa tirar pelo menos "
                f"{_pt_decimal(minima)} na AS: ({_pt_decimal(mp)} + {_pt_decimal(minima)}) / 2 = 6,0."
            )
        else:
            conclusao = "Pelas notas, é necessária a Avaliação Suplementar (AS)."
        frequencia = evidencia("frequencia minima de 75%")
        if frequencia:
            conclusao += " A aprovação também exige frequência mínima de 75%."
            usados.append(frequencia)
        return conta + conclusao, [dict(b, similaridade=1.0) for b in dict((b["id"], b) for b in usados).values()]
    return None
_RE_EVENTO_DE_INICIO = re.compile(r"\b(abertura|inicio|periodo de (?:realizacao|aplicacao))\b")
_RE_EVENTO_DE_FIM = re.compile(r"\b(fechamento|data limite|data-limite|prazo final|termino|periodo de (?:realizacao|aplicacao))\b")
_RE_PERGUNTA_DE_INICIO = re.compile(r"\b(comeca|comecam|inicio|inicia|iniciam|abertura|abre|abrem|a partir)\b")
_RE_PERGUNTA_DE_FIM = re.compile(r"\b(termina|terminam|fim|final|fechamento|encerra|encerram|ate quando|prazo|limite|fecha)\b")


def _resposta_factual_de_siglas(pergunta: str, trechos: list[dict]) -> tuple[str, list[dict]] | None:
    """Datas de avaliações (AT, AF, V1...) lidas direto das linhas do calendário, sem o modelo.

    Só valem linhas que começam com uma data. A resposta lista a abertura e o fechamento de cada
    avaliação citada; se a pergunta pede só o início ou só o fim, lista só esse lado.
    """
    siglas = _siglas_da_consulta(pergunta)
    normalizada = _normalizar(pergunta)
    if not siglas or not (
        _RE_CONSULTA_TEMPORAL.search(normalizada) or _RE_PERGUNTA_DE_FIM.search(normalizada)
        or _RE_PERGUNTA_DE_INICIO.search(normalizada)
    ):
        return None
    quer_inicio = bool(_RE_PERGUNTA_DE_INICIO.search(normalizada))
    quer_fim = bool(_RE_PERGUNTA_DE_FIM.search(normalizada))
    if not quer_inicio and not quer_fim:
        quer_inicio = quer_fim = True

    achados: dict[str, tuple[tuple[int, int, int], dict]] = {}
    for trecho in trechos:
        secao = _normalizar(str(trecho.get("secao", "")))
        mes_da_secao = next((nome for nome in _MESES if _normalizar(nome) in secao), None)
        ano_da_secao = re.search(r"\b20\d{2}\b", secao)
        for linha in str(trecho.get("texto", "")).splitlines():
            data = _RE_LINHA_COM_DATA.match(linha.strip())
            if not data or not any(re.search(rf"\b{re.escape(s)}\b", linha.upper()) for s in siglas):
                continue
            evento = _normalizar(data.group(4))
            # "Fechamento do período de realização" é só fim; "Período de realização" (um intervalo) vale para os dois.
            if re.match(r"(fechamento|data.limite|prazo final|termino)", evento):
                serve = quer_fim
            elif re.match(r"(abertura|inicio)", evento):
                serve = quer_inicio
            else:
                serve = bool(_RE_EVENTO_DE_INICIO.search(evento) or _RE_EVENTO_DE_FIM.search(evento))
            if not serve:
                continue
            mes = next((nome for nome in _MESES if _normalizar(nome) == _normalizar(data.group(2) or "")), mes_da_secao)
            if not mes:
                continue  # sem mês não há data: a linha não responde "quando"
            ano = data.group(3) or (ano_da_secao.group(0) if ano_da_secao else "")
            if data.group(2):
                texto = linha.strip().lstrip("-•✓ ").rstrip(".")
            else:
                dias = re.match(r"^[✓•\-\s]*(\d{1,2}(?:\s*(?:a|e)\s*\d{1,2})?)", linha.strip()).group(1)
                texto = f"{dias} de {mes}" + (f" de {ano}" if ano else "") + f" – {data.group(4).strip().rstrip('.')}"
            ordem = (int(ano or 0), _MESES.index(mes), int(data.group(1)))
            achados.setdefault(texto, (ordem, trecho))
    if not achados:
        return None

    ordenados = sorted(achados.items(), key=lambda item: item[1][0])
    documento = ordenados[0][1][1].get("documento", "calendário")
    if len(ordenados) == 1:
        resposta = f"Segundo o {documento}: {ordenados[0][0]}."
    else:
        resposta = f"Segundo o {documento}:\n" + "\n".join(f"- {texto}" for texto, _ in ordenados)
    usados: list[dict] = []
    for _, (_, trecho) in ordenados:
        if trecho not in usados:
            usados.append(trecho)
    return resposta, usados


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
        self._esclarecimentos: OrderedDict[str, EsclarecimentoPendente] = OrderedDict()
        self._lock = threading.Lock()
        # Respostas já geradas para perguntas que não dependem da conversa: a repetição é imediata.
        self._cache: OrderedDict[str, tuple[str, str, list[dict]]] = OrderedDict()
        self._versao_do_cache: tuple = ()

    # ------------------------------------------------------------ cache de respostas
    def _cache_valido(self) -> None:
        """Esvazia o cache quando mudam os documentos, os parâmetros ou o dia (o prompt cita a data de hoje)."""
        versao = (id(self.indice), repr(self.parametros), getattr(self.llm, "modelo", ""), date.today())
        if versao != self._versao_do_cache:
            self._cache.clear()
            self._versao_do_cache = versao

    def _ler_cache(self, chave: str) -> tuple[str, str, list[dict]] | None:
        with self._lock:
            self._cache_valido()
            achado = self._cache.get(chave)
            if achado:
                self._cache.move_to_end(chave)
            return achado

    def _gravar_cache(self, chave: str, texto: str, modo: str, trechos: list[dict]) -> None:
        with self._lock:
            self._cache_valido()
            self._cache[chave] = (texto, modo, trechos)
            while len(self._cache) > _MAX_RESPOSTAS_EM_CACHE:
                self._cache.popitem(last=False)

    def esquecer_resposta(self, pergunta: str) -> None:
        """Tira do cache as respostas dessa pergunta (usado quando o aluno marca "não útil")."""
        alvo = _chave_de_cache(pergunta)
        with self._lock:
            for chave in [c for c in self._cache if alvo and alvo in c]:
                del self._cache[chave]

    def _parece_continuacao(self, pergunta: str) -> bool:
        """A pergunta depende da anterior: tem marca de continuação ou não cita nenhum assunto próprio."""
        normalizada = " ".join(re.sub(r"[^\w\s]", " ", _normalizar(pergunta)).split())
        if _RE_MARCA_DE_CONTINUACAO.search(normalizada):
            return True
        if len(normalizada.split()) > _LIMITE_PALAVRAS_CONTINUACAO or _siglas_da_consulta(pergunta):
            return False
        lexico = getattr(self.indice, "lexico", None)
        # Número solto não é assunto: em "quais são as 32?" o 32 retoma a resposta anterior, e só parece
        # termo raro porque numera alguma seção do manual.
        sem_numeros = re.sub(r"\b\d+\b", " ", normalizada)
        return lexico is not None and not lexico.tem_termo_especifico(sem_numeros)

    # ------------------------------------------------------------ recuperação
    def recuperar(self, consulta: str) -> list[dict]:
        """Busca híbrida: similaridade vetorial acima do limiar, reordenada pela nota lexical (BM25)."""
        indice, p = self.indice, self.parametros
        if not indice.blocos:
            return []
        siglas = _siglas_da_consulta(consulta)
        vetor = self.embedder.embed_consulta(_expandir_siglas(consulta, siglas))
        resultados = indice.buscar(vetor, len(indice.blocos))

        lexico = getattr(indice, "lexico", None)
        peso = float(getattr(p, "peso_lexical", 0.0))
        notas = lexico.pontuar(consulta) if lexico is not None and peso > 0 else None
        maximo = float(notas.max()) if notas is not None and len(notas) else 0.0
        # Regras de presencial e EaD têm textos quase iguais; o escopo do bloco desempata a busca.
        modalidade = None if siglas else _modalidade_do_texto(_normalizar(consulta))

        candidatos: list[tuple[int, float]] = []
        for i, similaridade in resultados:
            bloco = indice.blocos[i]
            if siglas:
                if not _contem_siglas(bloco, siglas):
                    continue
            elif similaridade < p.limiar:
                continue
            nota = similaridade
            if maximo > 0:
                nota += peso * float(notas[i]) / maximo
            if modalidade:
                nota += _ajuste_de_modalidade(bloco, modalidade)
            candidatos.append((i, nota))
        candidatos.sort(key=lambda par: -par[1])
        return [
            dict(indice.blocos[i], similaridade=round(min(s, 1.0), 4))
            for i, s in candidatos[:p.top_k]
        ]

    def _resposta_factual_da_matriz(self, pergunta: str) -> tuple[str, list[dict]] | None:
        # "SI" só é expandido quando o aluno o usa como nome de curso.
        pergunta_matriz = pergunta
        if re.search(
            r"\b(?:curso|bacharelado|graduação|matriz|grade)(?:\s+(?:de|do|da|em))?\s+SI\b",
            pergunta,
            re.IGNORECASE,
        ):
            pergunta_matriz = re.sub(r"\bSI\b", "Sistemas de Informação", pergunta, flags=re.IGNORECASE)
        consulta = responder_consulta_matriz(pergunta_matriz, self.indice.blocos)
        if consulta:
            return consulta

        normalizada = _normalizar(pergunta_matriz)
        pergunta_contagem = bool(
            re.search(r"\b(quantas|quantos|numero|total)\b", normalizada)
            and re.search(r"\b(materias?|disciplinas?|componentes?)\b", normalizada)
        )
        cita_a_matriz = re.search(r"\b(sistemas de informacao|fluxograma|curriculo|grade|matriz|109an)\b", normalizada)
        if not pergunta_contagem or not cita_a_matriz:
            return None

        matriz = blocos_da_matriz(self.indice.blocos)
        blocos_periodos = [
            bloco for bloco in matriz
            if str(bloco.get("secao", "")).startswith("Disciplinas por período")
        ]
        componentes: dict[str, str] = {}
        for bloco in blocos_periodos:
            for linha in bloco.get("texto", "").splitlines():
                colunas = [coluna.strip() for coluna in linha.split("|")]
                if len(colunas) >= 2 and re.fullmatch(r"N\d{3}|OPT", colunas[0]):
                    componentes[colunas[0]] = colunas[1]
        if not componentes:
            return None

        optativos = {
            codigo for codigo, nome in componentes.items()
            if codigo == "OPT" or "optativa" in _normalizar(nome)
        }
        total = len(componentes)
        nao_optativos = total - len(optativos)
        pergunta_obrigatorias = bool(re.search(r"\bobrigatori[oa]s?\b", normalizada))
        if pergunta_obrigatorias:
            texto = (
                f"Considerando como obrigatórios os componentes que a matriz 109AN não identifica como optativos, "
                f"são {nao_optativos}. A matriz lista ainda {len(optativos)} componente optativo, totalizando "
                f"{total} componentes curriculares. A marcação (OL) não foi usada nessa contagem porque o documento "
                "não apresenta sua legenda."
            )
        else:
            texto = (
                f"A matriz curricular 109AN de Sistemas de Informação lista {total} componentes curriculares nos "
                f"oito períodos: {nao_optativos} não identificados como optativos e {len(optativos)} optativo. "
                "Esse total inclui dois estágios supervisionados e dois Trabalhos de Conclusão de Curso."
            )

        resumo = next(
            (
                bloco for bloco in matriz
                if bloco.get("secao") == "Contagem dos componentes curriculares"
            ),
            None,
        )
        fontes = [dict(resumo, similaridade=1.0)] if resumo else [
            dict(bloco, similaridade=1.0) for bloco in blocos_periodos
        ]
        return texto, fontes

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
                sessao_removida, _ = self._sessoes.popitem(last=False)
                self._esclarecimentos.pop(sessao_removida, None)

    # ------------------------------------------------------------ esclarecimentos
    def _cursos_indexados(self) -> set[str]:
        cursos = set()
        for bloco in self.indice.blocos:
            documento = _normalizar(str(bloco.get("documento", "")))
            if bloco.get("categoria") == "curso" or documento.startswith("curso de "):
                cursos.add(re.sub(r"^curso de\s+", "", documento).strip())
        return {curso for curso in cursos if curso}

    def _tem_curso_explicito(self, pergunta: str) -> bool:
        normalizada = _normalizar(pergunta)
        if any(curso in normalizada for curso in self._cursos_indexados()):
            return True
        return bool(re.search(r"\bcurso\s+(?:de|em)\s+(?!meu\b|minha\b|qual\b)[a-z]", normalizada))

    def _semestres_indexados(self) -> set[str]:
        semestres = set()
        for bloco in self.indice.blocos:
            cabecalho = f"{bloco.get('documento', '')} {bloco.get('arquivo', '')}"
            semestres.update(re.findall(r"20\d{2}[._ -]?[12]\b", _normalizar(cabecalho)))
        return {re.sub(r"[._ -]", "", semestre) for semestre in semestres}

    def _campos_para_esclarecer(self, pergunta: str) -> list[str]:
        normalizada = _normalizar(pergunta)
        campos: list[str] = []
        pergunta_sobre_prova = bool(_RE_ASSUNTO_AVALIACAO.search(normalizada))
        pergunta_temporal = bool(_RE_CONSULTA_TEMPORAL.search(normalizada))
        siglas = _siglas_da_consulta(pergunta)

        if pergunta_sobre_prova and pergunta_temporal:
            if not _RE_MODALIDADE.search(normalizada) and not siglas & _SIGLAS_EAD:
                campos.append("modalidade")
            if not _RE_TIPO_AVALIACAO.search(normalizada) and not siglas:
                campos.append("avaliacao")

        referencia_generica_ao_curso = bool(
            re.search(
                r"\b(meu curso|minha grade|minha matriz|meu estagio|meu tcc|"
                r"qual (?:e )?a grade|qual (?:e )?a matriz|carga horaria do curso|"
                r"duracao do curso|disciplinas do curso)\b",
                normalizada,
            )
        )
        if (
            _RE_DEPENDE_CURSO.search(normalizada)
            and referencia_generica_ao_curso
            and not self._tem_curso_explicito(pergunta)
        ):
            campos.append("curso")

        menciona_semestre_generico = bool(re.search(r"\b(meu|este|esse|proximo|qual)\s+semestre\b", normalizada))
        varios_semestres = len(self._semestres_indexados()) > 1
        if (menciona_semestre_generico or (pergunta_temporal and varios_semestres)) and not _RE_SEMESTRE.search(normalizada):
            campos.append("semestre")

        return list(dict.fromkeys(campos))

    def _iniciar_esclarecimento(
        self, sessao_id: str, pergunta: str, campos: list[str], origem: str = "codigo"
    ) -> str:
        with self._lock:
            self._esclarecimentos[sessao_id] = EsclarecimentoPendente(pergunta, list(campos), origem=origem)
            self._esclarecimentos.move_to_end(sessao_id)
        return PERGUNTAS_ESCLARECIMENTO[campos[0]]

    def _continuar_esclarecimento(
        self, sessao_id: str, resposta: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Retorna nova pergunta ou (consulta para a busca, pergunta contextualizada para o LLM, resumo)."""
        with self._lock:
            pendencia = self._esclarecimentos.get(sessao_id)
            if pendencia is None:
                return None, None, None, None
            campo = pendencia.campos[0]
            valor = _interpretar_esclarecimento(campo, resposta)
            if valor is None:
                return f"Não consegui identificar esse dado. {PERGUNTAS_ESCLARECIMENTO[campo]}", None, None, None

            pendencia.respostas[campo] = valor
            pendencia.campos.pop(0)
            self._esclarecimentos.move_to_end(sessao_id)
            if pendencia.campos:
                return PERGUNTAS_ESCLARECIMENTO[pendencia.campos[0]], None, None, None

            self._esclarecimentos.pop(sessao_id, None)
            contexto = "; ".join(pendencia.respostas.values())
            termos_busca: list[str] = []
            if "avaliacao" in pendencia.respostas:
                if _RE_CONSULTA_TEMPORAL.search(_normalizar(pendencia.pergunta_original)):
                    termos_busca.append("data período aplicação calendário letivo")
                else:
                    termos_busca.append("provas avaliação")
            for campo_resposta, valor_resposta in pendencia.respostas.items():
                if campo_resposta == "modalidade" and valor_resposta == "modalidade presencial":
                    termos_busca.append("disciplinas presenciais")
                else:
                    termos_busca.append(valor_resposta)
            if pendencia.respostas.get("avaliacao") == "todas as avaliações":
                # "Todas" vira a lista de siglas da modalidade: as datas saem direto das linhas do calendário.
                ead = pendencia.respostas.get("modalidade") == "modalidade EaD"
                termos_busca.append("AF AT AP AS" if ead else "V1 R1 V2 VS")
            # A busca usa a pergunta original mais os dados informados: só os dados ("disciplinas
            # presenciais") trariam trechos sem relação com a dúvida.
            consulta = f"{pendencia.pergunta_original} {' '.join(termos_busca) or contexto}"
            pergunta_contextualizada = (
                f"Pergunta original: {pendencia.pergunta_original}\n"
                f"Informações fornecidas pelo usuário: {contexto}.\n"
                "Responda à pergunta original considerando esses esclarecimentos."
            )
            # Forma da pergunta guardada no histórico: a original já com o dado informado.
            resumo = f"{pendencia.pergunta_original} ({contexto})"
            return None, consulta, pergunta_contextualizada, resumo

    def _tem_esclarecimento(self, sessao_id: str | None) -> bool:
        if not sessao_id:
            return False
        with self._lock:
            return sessao_id in self._esclarecimentos

    @staticmethod
    def _resposta_simples(texto: str, modo: str, inicio: float) -> Resposta:
        return Resposta(texto, [], modo, [], int((time.perf_counter() - inicio) * 1000))

    # ------------------------------------------------------------ prompt (Quadro 2 do TCC)
    def montar_mensagens(self, pergunta: str, trechos: list[dict], historico: list[tuple[str, str]]) -> list[dict]:
        partes = []
        for n, t in enumerate(trechos, start=1):
            atualizado = f", atualizado em {t['atualizado_em']}" if t.get("atualizado_em") else ""
            contexto = f"(Continuação de: {t['contexto']})\n" if t.get("contexto") else ""
            if t.get("tabela"):
                contexto += f"(Continuação da tabela: {t['tabela']})\n"
            partes.append(f"[Trecho {n}] Fonte: {t['documento']} – {t['secao']}{atualizado}\n{contexto}{t['texto']}")
        # A parte fixa (prompt, regras, data) abre o sistema: o Ollama reaproveita esse prefixo entre
        # perguntas. Testado em 2026-11-08: mover os trechos para a mensagem do usuário baixou o acerto
        # de 34/35 para 31/35 sem ganho de tempo; os trechos ficam no sistema.
        sistema = (
            f"{self.parametros.prompt_sistema.strip()}\n\n{REGRAS_RESPOSTA}\n\n"
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
    def _trechos_em_foco(self, consulta: str, trechos: list[dict]) -> list[dict]:
        """Só as linhas dos trechos que têm palavras da pergunta, com as vizinhas, para uma segunda leitura.

        Um contexto curto e direto é lido com muito mais acerto por modelos pequenos. Lista vazia quando
        nenhuma linha tem relação com a pergunta: aí a recusa do modelo está certa e não há segunda leitura.
        """
        lexico = getattr(self.indice, "lexico", None)
        if lexico is None:
            return []
        candidatas: list[tuple[float, int, int]] = []
        linhas_por_trecho: list[list[str]] = []
        for n, trecho in enumerate(trechos):
            linhas = [l for l in str(trecho.get("texto", "")).split("\n") if l.strip()]
            linhas_por_trecho.append(linhas)
            for i, nota in enumerate(lexico.linhas_relevantes(consulta, linhas)):
                if nota > 0:
                    candidatas.append((nota, n, i))
        escolhidas: dict[int, set[int]] = {}
        for _, n, i in sorted(candidatas, reverse=True)[:_LINHAS_NO_FOCO]:
            escolhidas.setdefault(n, set()).update(
                j for j in (i - 1, i, i + 1) if 0 <= j < len(linhas_por_trecho[n])
            )
        focados = []
        for n in sorted(escolhidas):
            partes, anterior = [], None
            for i in sorted(escolhidas[n]):
                if anterior is not None and i > anterior + 1:
                    partes.append("[...]")
                partes.append(linhas_por_trecho[n][i])
                anterior = i
            focados.append(dict(trechos[n], texto="\n".join(partes)))
        return focados

    def _glossario(self) -> dict[str, str]:
        if getattr(self, "_glossario_de", None) is not self.indice:
            self._glossario_de, self._glossario_cache = self.indice, glossario_de_siglas(self.indice.blocos)
        return self._glossario_cache

    def _dicas_para_o_modelo(self, pergunta: str) -> str:
        """Equivalências de vocabulário e significado das siglas citadas, como os documentos escrevem."""
        partes = [_dica_de_equivalencias(pergunta)]
        glossario = self._glossario()
        siglas = [t.upper() for t in re.findall(r"\b[A-Za-z]{2,6}\b", pergunta)
                  if t.upper() in glossario and (t.isupper() or t.upper() in _SIGLAS_ACADEMICAS and t.lower() != "as")]
        if siglas:
            definicoes = "; ".join(f"{sigla} = {glossario[sigla]}" for sigla in dict.fromkeys(siglas))
            partes.append(f"(Siglas nos documentos: {definicoes}.)")
        return "\n".join(parte for parte in partes if parte)

    def _restricoes_para_o_modelo(self, pergunta: str, trechos: list[dict]) -> str:
        """Frases de restrição dos trechos que tratam do caso perguntado, destacadas para o modelo.

        Modelos pequenos aplicam a regra geral e passam por cima da exceção escrita logo abaixo
        ("poderá requerer a 2ª chamada" x "Não haverá 2ª chamada para a VS e/ou R1"), mesmo com a
        exceção no primeiro trecho.

        Só vale para perguntas que citam uma avaliação (R1, VS...): a frase precisa citar essa avaliação
        e tratar do assunto perguntado, cobrindo pelo menos metade dos demais termos da pergunta. Sem a
        segunda condição, "quem pode fazer a R1?" recebia a restrição da 2ª chamada só por citar a R1.
        Em perguntas gerais, sem avaliação citada, o destaque puxava a resposta para a exceção
        ("como tranco a matrícula?" virava a condição de reprovação por frequência), então não há destaque.
        """
        lexico = getattr(self.indice, "lexico", None)
        if lexico is None:
            return ""
        glossario = self._glossario()
        siglas = {t.upper() for t in re.findall(r"\b[A-Za-z0-9]{2,6}\b", pergunta)
                  if t.upper() in _SIGLAS_ACADEMICAS and (t.isupper() or t.lower() != "as")}
        if not siglas:
            return ""
        # o assunto da pergunta, sem as siglas: é ele que a frase de restrição precisa cobrir
        consulta = re.sub(rf"\b(?:{'|'.join(siglas)})\b", " ", pergunta, flags=re.IGNORECASE)
        candidatas: dict[str, float] = {}
        for trecho in trechos:
            for linha in str(trecho.get("texto", "")).split("\n"):
                linha = linha.strip(" -•\t")
                normalizada = _normalizar(linha)
                if len(linha) < 20 or not _RE_RESTRICAO.search(normalizada):
                    continue
                if any(not re.search(rf"\b{s.lower()}\b", normalizada)
                       and _normalizar(glossario.get(s, s)) not in normalizada for s in siglas):
                    continue
                presentes, total = lexico.linhas_relevantes(consulta, [linha, consulta])
                if total and presentes / total >= _COBERTURA_MINIMA_DA_RESTRICAO:
                    candidatas[linha] = max(candidatas.get(linha, 0.0), presentes / total)
        escolhidas = sorted(candidatas, key=candidatas.get, reverse=True)[:_MAX_RESTRICOES]
        if not escolhidas:
            return ""
        frases = " ".join(f"“{linha}”" for linha in escolhidas)
        citadas = ", ".join(sorted(siglas))
        return (f"(Restrição nos trechos que se aplica a {citadas}: {frases} "
                "Responda de acordo com essa restrição; ela prevalece sobre a regra geral. A restrição é a "
                "informação pedida: não diga que a informação não foi localizada.)")

    def _citacao_do_documento(self, pergunta: str, trechos: list[dict]) -> tuple[str, list[dict]] | None:
        """Segunda leitura: o modelo aponta a frase e o código devolve o texto original do documento.

        A frase só é aceita se existir nos trechos e contiver quase todos os termos da pergunta; assim uma
        pergunta sem resposta na base continua recebendo a recusa.
        """
        lexico = getattr(self.indice, "lexico", None)
        if lexico is None or lexico.cobertura(pergunta) < _COBERTURA_MINIMA_DO_ACERVO:
            return None
        # Para comparar palavras, a pergunta ganha o nome por extenso das siglas que cita (VS -> Verificação Suplementar).
        glossario = self._glossario()
        nomes = [glossario[t.upper()] for t in re.findall(r"\b[A-Za-z]{2,6}\b", pergunta)
                 if t.upper() in glossario and (t.isupper() or t.upper() in _SIGLAS_ACADEMICAS and t.lower() != "as")]
        consulta = " ".join([pergunta, *nomes])
        focados = self._trechos_em_foco(consulta, trechos)
        if not focados:
            return None
        linhas_do_foco = [
            (linha.strip(), trecho) for trecho in focados
            for linha in str(trecho["texto"]).split("\n") if linha.strip() and linha.strip() != "[...]"
        ]

        candidatas: list[tuple[str, dict]] = []
        literal: tuple[str, dict] | None = None
        # 1ª candidata: a linha com mais palavras da pergunta.
        notas = lexico.linhas_relevantes(consulta, [linha for linha, _ in linhas_do_foco])
        if notas and max(notas) > 0:
            candidatas.append(linhas_do_foco[notas.index(max(notas))])
        # 2ª candidata: a linha apontada pelo modelo, trocada pela linha original mais parecida.
        texto_do_foco = "\n\n".join(f"Fonte: {f['documento']} – {f['secao']}\n{f['texto']}" for f in focados)
        resposta = self.llm.gerar(
            [{"role": "system", "content": f"{_SISTEMA_CITACAO}\n\nLinhas dos documentos:\n\n{texto_do_foco}"},
             {"role": "user", "content": f"{pergunta}\n{self._dicas_para_o_modelo(pergunta)}".strip()}],
            0.0,
        )
        logger.info("Segunda leitura para %r: %r", pergunta[:80], resposta[:160])
        achado = _RE_CITACAO.search(resposta)
        citado = _normalizar((achado.group(1) if achado else "").split("\nRESPOSTA")[0].strip().strip('"“”«» '))
        if not citado or citado.startswith("nenhuma"):
            return None  # o modelo confirma que nenhuma linha trata do assunto: vale a recusa
        if citado:
            palavras_citadas = set(re.findall(r"\w+", citado))

            def semelhanca(linha: str) -> float:
                palavras = set(re.findall(r"\w+", _normalizar(linha)))
                if citado in _normalizar(linha):
                    return 0.99
                return len(palavras & palavras_citadas) / max(len(palavras | palavras_citadas), 1)

            parecida = max(linhas_do_foco, key=lambda par: semelhanca(par[0]))
            if semelhanca(parecida[0]) >= _SEMELHANCA_MINIMA_DA_CITACAO and parecida not in candidatas:
                candidatas.append(parecida)
            # citação literal e com corpo de frase (não um "sim" ou um nome solto)
            if semelhanca(parecida[0]) >= 0.99 and len(citado) >= 25:
                literal = parecida

        melhor: tuple[float, str, str, dict] | None = None
        for linha, trecho in candidatas:
            contexto = ""
            if " | " in linha:  # linha de tabela: precisa da frase que apresenta a tabela e dos nomes das colunas
                original = next((t for t in trechos if t.get("id") == trecho.get("id")), trecho)
                anteriores = str(original.get("texto", "")).split(linha)[0].split("\n")
                legenda = next((l.strip() for l in reversed(anteriores) if l.strip() and " | " not in l), "")
                cabecalho = next(
                    (l.strip() for l in reversed(anteriores) if " | " in l and not re.search(r"\d", l)), ""
                )
                contexto = "\n".join(x for x in (legenda, cabecalho) if x) or str(original.get("tabela") or "")
            apoio = f"{trecho.get('secao', '')} {contexto} {linha}"
            presentes, total = lexico.linhas_relevantes(consulta, [apoio, consulta])
            # parcela dos termos da pergunta que a frase contém, descontando os termos que nem existem na base
            cobertura = (presentes / total) * lexico.cobertura(pergunta) if total else 0.0
            logger.info("Citação candidata (cobertura %.2f): %r", cobertura, linha[:110])
            minimo = (_COBERTURA_MINIMA_DA_CITACAO_LITERAL if (linha, trecho) == literal
                      else _COBERTURA_MINIMA_DA_CITACAO)
            if cobertura >= minimo and (melhor is None or cobertura > melhor[0]):
                melhor = (cobertura, linha, contexto, trecho)
        if melhor is None:
            return None
        _, linha, contexto, trecho = melhor
        corpo = f"{contexto}\n{linha}" if contexto else linha
        return f"{INTRO_CITACAO}\n\n“{corpo}”\n\n({trecho['documento']} – {trecho['secao']})", [trecho]

    def _resposta_validada(self, sessao_id, pergunta_guardada, texto, trechos, inicio) -> Resposta:
        self._guardar(sessao_id, pergunta_guardada, texto)
        return Resposta(
            resposta=texto,
            fontes=montar_fontes(trechos),
            modo="validado",
            trechos=[{k: t.get(k) for k in ("id", "documento", "arquivo", "secao", "similaridade")} for t in trechos],
            tempo_ms=int((time.perf_counter() - inicio) * 1000),
        )

    def _gerar_com_verificacoes(
        self, pergunta: str, pergunta_modelo: str, trechos: list[dict], historico: list[tuple[str, str]], ao_token,
        pode_esclarecer: bool = False,
    ) -> tuple[str, str, list[dict]]:
        """Gera a resposta e aplica as proteções. Devolve (texto, modo, trechos)."""
        p = self.parametros
        pergunta_original = pergunta_modelo
        dica = "\n".join(d for d in (self._dicas_para_o_modelo(pergunta_modelo),
                                     self._restricoes_para_o_modelo(pergunta_original, trechos)) if d)
        if dica:
            pergunta_modelo = f"{pergunta_modelo}\n{dica}"
        mensagens = self.montar_mensagens(pergunta_modelo, trechos, historico)
        extra = {"ao_token": ao_token} if ao_token is not None else {}
        texto = self.llm.gerar(mensagens, p.temperatura, **extra)

        pedido = _pedido_formal(texto)
        if pedido and pode_esclarecer:
            campo = _campo_do_pedido(pergunta_modelo, pedido, trechos)
            if campo:
                return campo, "esclarecer", []  # pedido necessário: o código faz a pergunta ao aluno
        if _pedido_formal(texto) or _modelo_pediu_dado(texto):
            reforco = f"{pergunta_modelo}\n\n{_INSTRUCAO_SEM_PERGUNTAS}"
            texto = self.llm.gerar(self.montar_mensagens(reforco, trechos, historico), p.temperatura)
            if _pedido_formal(texto) or _modelo_pediu_dado(texto):
                logger.warning("Modelo insistiu em pedir dados; resposta substituída: %r", texto[:120])
                return MENSAGEM_NAO_ENCONTRADA, "sem_resultado", []

        recusa = _padronizar_recusa(texto, pergunta)
        if recusa:
            citacao = self._citacao_do_documento(pergunta_original, trechos)
            if citacao:
                return citacao[0], "citacao", citacao[1]
            return recusa, "sem_resultado", []  # sem fontes: os trechos não continham a resposta

        if _datas_e_valores(texto):
            apoio = " ".join(
                [pergunta_modelo, p.prompt_sistema, REGRAS_RESPOSTA, f"{date.today():%d/%m/%Y}"]
                + [f"{a} {b}" for a, b in historico]
                + [" ".join(str(t.get(c, "")) for c in ("documento", "secao", "contexto", "tabela", "texto", "atualizado_em"))
                   for t in trechos]
            )
            sem_apoio = _numeros_sem_apoio(texto, apoio)
            if sem_apoio:
                logger.warning("Números sem apoio nos trechos %s; gerando de novo: %r", sorted(sem_apoio), texto[:160])
                reforco = f"{pergunta_modelo}\n\n{_INSTRUCAO_SO_NUMEROS_DOS_TRECHOS}"
                texto = self.llm.gerar(self.montar_mensagens(reforco, trechos, historico), p.temperatura)
                recusa = _padronizar_recusa(texto, pergunta)
                if recusa:
                    return recusa, "sem_resultado", []
                sem_apoio = _numeros_sem_apoio(texto, apoio)
                if sem_apoio or _modelo_pediu_dado(texto):
                    logger.warning("Números sem apoio persistiram %s; resposta substituída: %r", sorted(sem_apoio), texto[:160])
                    return (_padronizar_recusa("Não localizei", pergunta) or MENSAGEM_NAO_ENCONTRADA), "sem_resultado", []
        return texto, "llm", trechos

    def responder(self, pergunta: str, sessao_id: str | None = None, ao_token=None) -> Resposta:
        """ao_token(texto), se informado, recebe a resposta do modelo enquanto ela é gerada.

        O texto parcial é provisório: a Resposta devolvida é a definitiva (as verificações
        podem trocá-la por uma recusa padronizada ou por uma pergunta de esclarecimento).
        """
        inicio = time.perf_counter()
        p = self.parametros
        conversa = self.historico(sessao_id)
        conversa = conversa[-p.historico_trocas:] if p.historico_trocas > 0 else []

        social = _resposta_social(pergunta)
        if social and not self._tem_esclarecimento(sessao_id):
            self._guardar(sessao_id, pergunta, social)
            return self._resposta_simples(social, "conversa", inicio)

        consulta = pergunta            # texto usado na busca
        pergunta_modelo = pergunta     # texto enviado ao modelo
        pergunta_guardada = pergunta   # texto guardado no histórico da conversa
        historico: list[tuple[str, str]] = []  # só perguntas de continuação levam a conversa ao modelo
        pode_esclarecer = bool(sessao_id)

        if sessao_id and self._tem_esclarecimento(sessao_id):
            nova_pergunta, consulta_refinada, pergunta_contextualizada, resumo = self._continuar_esclarecimento(
                sessao_id, pergunta
            )
            if nova_pergunta:
                self._guardar(sessao_id, pergunta, nova_pergunta)
                return self._resposta_simples(nova_pergunta, "esclarecimento", inicio)
            if consulta_refinada:
                consulta = consulta_refinada
                pergunta_modelo = pergunta_contextualizada or pergunta
                pergunta_guardada = resumo or pergunta
                pode_esclarecer = False  # um esclarecimento por pergunta
            factual_matriz = self._resposta_factual_da_matriz(pergunta_modelo)
        else:
            # Dado exato da matriz vem antes: "Em qual semestre é Banco de Dados?" não precisa de esclarecimento.
            factual_matriz = self._resposta_factual_da_matriz(pergunta)
            if not factual_matriz and conversa and self._parece_continuacao(pergunta):
                historico = conversa
                consulta = _combinar_com_anterior(conversa[-1][0], pergunta)
                factual_matriz = self._resposta_factual_da_matriz(consulta)
            if not factual_matriz and sessao_id:
                campos = self._campos_para_esclarecer(consulta)
                if campos:
                    texto = self._iniciar_esclarecimento(sessao_id, consulta, campos)
                    self._guardar(sessao_id, pergunta, texto)
                    return self._resposta_simples(texto, "esclarecimento", inicio)

        if factual_matriz:
            return self._resposta_validada(sessao_id, pergunta_guardada, *factual_matriz, inicio)

        factual_notas = _resposta_factual_de_notas_presenciais(pergunta_modelo, self.indice.blocos)
        if factual_notas:
            return self._resposta_validada(sessao_id, pergunta_guardada, *factual_notas, inicio)

        factual_vs = _resposta_factual_de_vs(pergunta_modelo, self.indice.blocos)
        if factual_vs:
            return self._resposta_validada(sessao_id, pergunta_guardada, *factual_vs, inicio)

        factual_ead = _resposta_factual_de_notas_ead(pergunta_modelo, conversa, self.indice.blocos)
        if factual_ead:
            return self._resposta_validada(sessao_id, pergunta_guardada, *factual_ead, inicio)

        trechos = self.recuperar(consulta)
        if not trechos and conversa and not historico:
            # nada encontrado para a pergunta sozinha: tenta com a anterior como contexto
            historico = conversa
            consulta = f"{conversa[-1][0]} {consulta}"
            trechos = self.recuperar(consulta)

        if trechos and pode_esclarecer and _modalidade_ambigua(consulta, trechos, self.indice.blocos):
            texto = self._iniciar_esclarecimento(sessao_id, consulta, ["modalidade"], origem="recuperacao")
            self._guardar(sessao_id, pergunta, texto)
            return self._resposta_simples(texto, "esclarecimento", inicio)

        lexico = getattr(self.indice, "lexico", None)
        sem_palavra_em_comum = (
            lexico is not None and lexico.n > 0 and not _siglas_da_consulta(consulta)
            and float(lexico.pontuar(consulta).max()) == 0.0
        )
        # Datas de avaliações: todas as linhas do calendário que citam as siglas, não só os trechos mais próximos.
        siglas = _siglas_da_consulta(consulta)
        factual = _resposta_factual_de_siglas(consulta, [
            dict(b, similaridade=1.0) for b in self.indice.blocos
            if any(_contem_siglas(b, {sigla}) for sigla in siglas)
        ]) if siglas else None
        if factual:
            return self._resposta_validada(sessao_id, pergunta_guardada, *factual, inicio)

        if not trechos or sem_palavra_em_comum:
            # Nenhuma palavra da pergunta existe na base: fora do escopo, sem gastar tempo do modelo.
            texto = _padronizar_recusa("Não localizei", pergunta) or MENSAGEM_NAO_ENCONTRADA
            modo, trechos = "sem_resultado", []
        else:
            chave = None if historico else _chave_de_cache(f"{consulta} | {pergunta_modelo}")
            guardado = self._ler_cache(chave) if chave else None
            if guardado:
                texto, modo, trechos = guardado
                if ao_token is not None:
                    ao_token(texto)
            else:
                try:
                    texto, modo, trechos = self._gerar_com_verificacoes(
                        pergunta, pergunta_modelo, trechos, historico, ao_token, pode_esclarecer
                    )
                    if modo == "esclarecer":
                        texto = self._iniciar_esclarecimento(sessao_id, consulta, [texto], origem="modelo")
                        self._guardar(sessao_id, pergunta, texto)
                        return self._resposta_simples(texto, "esclarecimento", inicio)
                    if chave:
                        self._gravar_cache(chave, texto, modo, trechos)
                except LLMIndisponivel as erro:
                    logger.warning("LLM indisponível, usando modo extrativo: %s", erro)
                    texto, modo = self.resposta_extrativa(consulta, trechos), "extrativo"

        self._guardar(sessao_id, pergunta_guardada, texto)
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
