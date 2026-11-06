"""Validação conservadora de respostas com datas e eventos documentais."""
from __future__ import annotations

import re
import unicodedata

MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
_RE_NUMERO = re.compile(r"^[✓•▪◦*-]?\s*(\d{1,2})(?:\s*(?:a|e)\s*(\d{1,2}))?\s*[–—-]\s*(.+)$", re.I)
_RE_DATA_COMPLETA = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](20\d{2}))?\b")
_RE_DATA_TEXTO = re.compile(
    r"\b(\d{1,2})\s+de\s+(janeiro|fevereiro|mar[cç]o|abril|maio|junho|julho|agosto|"
    r"setembro|outubro|novembro|dezembro)(?:\s+de\s+(20\d{2}))?\b", re.I
)
_RE_CODIGO_AVALIACAO = re.compile(r"\b(AT|AF|AP|AS|AV)\b")
_RE_PRAZO_FINAL = re.compile(r"prazo\s*(?:limite|final)|data\s*limite|ate\s+quando|termin|encerra|fecha|fechamento", re.I)


def _normalizar(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


def codigo_avaliacao_e_prazo(pergunta: str) -> str | None:
    """Reconhece siglas acadêmicas apenas em perguntas sobre o prazo final."""
    codigo = _RE_CODIGO_AVALIACAO.search(pergunta)
    if codigo and _RE_PRAZO_FINAL.search(_normalizar(pergunta)):
        return codigo.group(1)
    return None


def eventos_de_prazo(trechos: list[dict], codigo: str) -> list[dict]:
    """Eventos que fecham o período da avaliação indicada no mesmo documento."""
    return [
        evento for evento in eventos_das_evidencias(trechos)
        if re.search(rf"\b{re.escape(codigo)}\b", evento["evento"])
        and re.search(r"fechamento|encerramento|termino|data.limite|prazo.final", _normalizar(evento["evento"]))
        and evento["mes"] is not None and evento["ano"] is not None
    ]


def _mes_secao(secao: str) -> int | None:
    texto = _normalizar(secao)
    for nome, numero in MESES.items():
        if re.search(rf"\b{nome}\b", texto):
            return numero
    return None


def _ano_bloco(bloco: dict) -> int | None:
    if str(bloco.get("ano", "")).isdigit():
        return int(bloco["ano"])
    periodo = str(bloco.get("periodo", ""))
    encontrado = re.search(r"\b(20\d{2})\b", periodo)
    if encontrado:
        return int(encontrado.group(1))
    encontrado = re.search(r"\b(20\d{2})\b", str(bloco.get("texto", "")))
    return int(encontrado.group(1)) if encontrado else None


def eventos_das_evidencias(trechos: list[dict]) -> list[dict]:
    """Converte linhas de calendário em fatos com data, seção e evento."""
    eventos = []
    for bloco in trechos:
        mes = int(bloco["mes"]) if str(bloco.get("mes", "")).isdigit() else _mes_secao(str(bloco.get("secao", "")))
        ano = _ano_bloco(bloco)
        for linha in str(bloco.get("texto", "")).splitlines():
            encontrado = _RE_NUMERO.search(linha.strip())
            if not encontrado:
                continue
            inicio = int(encontrado.group(1))
            fim = int(encontrado.group(2) or inicio)
            eventos.append({
                "inicio": inicio,
                "fim": fim,
                "mes": mes,
                "ano": ano,
                "evento": encontrado.group(3).strip(),
                "linha": linha.strip(),
                "bloco": bloco,
            })
    return eventos


def _datas_da_resposta(texto: str) -> list[tuple[int, int | None, int | None]]:
    datas = []
    for match in _RE_DATA_COMPLETA.finditer(texto):
        datas.append((int(match.group(1)), int(match.group(2)), int(match.group(3)) if match.group(3) else None))
    for match in _RE_DATA_TEXTO.finditer(_normalizar(texto)):
        datas.append((int(match.group(1)), MESES[match.group(2).replace("ç", "c")], int(match.group(3)) if match.group(3) else None))
    return datas


def pergunta_exige_validacao_de_data(pergunta: str) -> bool:
    texto = _normalizar(pergunta)
    if codigo_avaliacao_e_prazo(pergunta):
        return True
    return bool(re.search(
        r"calendario|data|datas|quando|inicio|comeco|termino|fim|aula|semestre|"
        r"ferias|prova|avaliacao",
        texto,
    ))


def validar_resposta_datas(pergunta: str, resposta: str, trechos: list[dict]) -> tuple[bool, list[str]]:
    """Valida presença e associação mínima entre data, mês, ano e evento."""
    if not pergunta_exige_validacao_de_data(pergunta):
        return True, []
    eventos = eventos_das_evidencias(trechos)
    datas = _datas_da_resposta(resposta)
    codigo = codigo_avaliacao_e_prazo(pergunta)
    if codigo:
        prazos = eventos_de_prazo(trechos, codigo)
        if not prazos:
            return False, [f"O prazo final da avaliação {codigo} não está nas evidências recuperadas."]
        if not any(
            mes == evento["mes"] and ano == evento["ano"]
            and dia == evento["fim"]
            for dia, mes, ano in datas for evento in prazos
        ):
            return False, [f"A resposta não associou o prazo final da avaliação {codigo} à data do evento."]
        # Também rejeita datas citadas como prazo final que pertencem a outro evento.
        return True, []
    if not datas:
        return False, ["A resposta não apresentou uma data verificável."]
    motivos = []
    texto_resposta = _normalizar(resposta)
    for dia, mes, ano in datas:
        candidatos = [e for e in eventos if e["inicio"] <= dia <= e["fim"] and (mes is None or e["mes"] in (None, mes))]
        if ano is not None:
            candidatos = [e for e in candidatos if e["ano"] in (None, ano)]
        if not candidatos:
            motivos.append(f"A data {dia:02d}/{mes or 0:02d}/{ano or 0} não foi localizada nas evidências.")

    # Para perguntas de calendário, uma resposta sem mês/ano é ambígua mesmo
    # quando o texto traz apenas números soltos como "04" ou "23".
    if re.search(r"calendario|semestre|inicio.*termino|datas?", _normalizar(pergunta)):
        if not any(mes is not None and ano is not None for _, mes, ano in datas):
            motivos.append("A resposta não associou as datas a mês e ano.")

    # A validação de associação exige que algum termo central da intenção
    # também apareça no evento associado, evitando validar só o número.
    termos = set(re.findall(r"[a-z]{4,}", texto_resposta))
    termos_criticos = termos & {"inicio", "termino", "aulas", "avaliacao", "prova", "matricula", "semestre", "chamada"}
    if termos_criticos and eventos:
        eventos_texto = " ".join(_normalizar(e["evento"]) for e in eventos)
        if not any(t in eventos_texto for t in termos_criticos):
            motivos.append("Os eventos citados não foram associados às evidências recuperadas.")
    return not motivos, motivos


def resposta_factual_de_eventos(pergunta: str, trechos: list[dict]) -> str:
    """Fallback determinístico quando a LLM produz datas não verificáveis."""
    texto = _normalizar(pergunta)
    codigo = codigo_avaliacao_e_prazo(pergunta)
    if codigo:
        prazos = eventos_de_prazo(trechos, codigo)
        if not prazos:
            return ""
        linhas = []
        for evento in prazos:
            data = f"{evento['fim']:02d}/{evento['mes']:02d}/{evento['ano']}"
            fonte = evento["bloco"]
            pagina = f", página {fonte['pagina']}" if fonte.get("pagina") else ""
            linhas.append(f"O prazo final para realizar a avaliação {codigo} é {data}, conforme o fechamento do período de realização informado em {fonte.get('documento', 'calendário')} — {fonte.get('secao', 'seção')}{pagina}.")
        return "\n".join(dict.fromkeys(linhas))
    eventos = eventos_das_evidencias(trechos)
    filtros = []
    inicio_e_fim = bool(re.search(r"inicio.*(termino|fim)|termino.*(inicio|comeco)|comeco.*fim", texto))
    if inicio_e_fim:
        filtros.extend([r"inicio[^\n]{0,40}aulas", r"termino[^\n]{0,40}semestre"])
    if re.search(r"inicio|comeco|aulas", texto):
        if not inicio_e_fim:
            filtros.append(r"inicio[^\n]{0,40}aulas|aulas[^\n]{0,40}inicio" if "aula" in texto else r"inicio|comeco")
    if re.search(r"termino|fim|encerramento", texto) and not inicio_e_fim:
        filtros.append(r"termino|fechamento|encerramento")
    if not filtros and re.search(r"prova|avaliacao", texto):
        filtros.append(r"prova|avaliacao|notas")
    if filtros:
        eventos = [e for e in eventos if any(re.search(filtro, _normalizar(e["evento"])) for filtro in filtros)]
    elif re.search(r"prova|avaliacao", texto):
        eventos = [e for e in eventos if re.search(r"prova|avaliacao|notas", _normalizar(e["evento"]))]

    linhas = []
    vistos = set()
    for evento in eventos:
        chave = (evento["linha"], evento["mes"], evento["ano"])
        if chave in vistos:
            continue
        vistos.add(chave)
        if evento["mes"] and evento["ano"]:
            data = f"{evento['inicio']:02d}/{evento['mes']:02d}/{evento['ano']}"
            if evento["fim"] != evento["inicio"]:
                data += f" a {evento['fim']:02d}/{evento['mes']:02d}/{evento['ano']}"
            linhas.append(f"- {data}: {evento['evento']}")
        else:
            linhas.append(f"- {evento['linha']}")
    if "segunda chamada" in texto or "2a chamada" in texto:
        for bloco in trechos:
            for linha in str(bloco.get("texto", "")).splitlines():
                if re.search(r"2\s*[ªaº]?\s*chamada|segunda chamada", _normalizar(linha)):
                    linhas.append(f"- {linha.strip()}")
    if not linhas:
        return ""
    return "Segundo as evidências recuperadas:\n" + "\n".join(linhas)
