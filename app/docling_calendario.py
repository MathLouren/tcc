"""Reconstrução de calendários acadêmicos diagramados (mini-calendário de cada mês ao lado dos eventos).

O Docling lê esse tipo de página coluna por coluna: os títulos dos meses saem vazios e os eventos de
meses diferentes caem na mesma lista, sem dizer a que mês pertencem. Aqui cada evento é atribuído ao
mês pela posição na página (o mini-calendário do mês fica ao lado dos seus eventos) e escrito com a
data completa, por exemplo "14 de setembro de 2026 – Abertura do período de realização da Avaliação AT".
Uma linha assim continua correta mesmo quando o bloco é cortado ou lido isoladamente pelo modelo.
"""
from __future__ import annotations

import re
import unicodedata

MESES = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)
_MESES_SEM_ACENTO = [unicodedata.normalize("NFKD", m).encode("ascii", "ignore").decode() for m in MESES]
_RE_MES = re.compile(r"\b(" + "|".join(_MESES_SEM_ACENTO) + r")\b")
# "01 a 31- Período...", "04 - Prova", "03 e 04 - Webinário", com ou sem o cabeçalho "D S T Q Q S S ✓" colado
_RE_EVENTO = re.compile(
    r"^(?:[DSTQ]\s+){0,7}[✓•\s]*(\d{1,2})(?:\s*(a|e)\s*(\d{1,2}))?\s*[-–]\s*(.+)$", re.DOTALL
)
_RE_SEMESTRE = re.compile(r"(\d)\s*[ºo°]\s*SEMESTRE\s+DE\s+(20\d{2})", re.IGNORECASE)
_RE_SO_DIAS = re.compile(r"^(?:[DSTQ]\s*){5,}$|^(?:\d{1,2}\s*)+$")
_RE_PAGINA = re.compile(r"\s*P[áa]gina\s+\d+\s+de\s+\d+\s*", re.IGNORECASE)
_MINIMO_DE_MESES = 3
_MINIMO_DE_EVENTOS = 8


def _sem_acento(texto: str) -> str:
    return unicodedata.normalize("NFKD", str(texto).lower()).encode("ascii", "ignore").decode()


def _caixa(item: dict, alturas: dict[int, float]) -> tuple[int, float, float, float] | None:
    """(página, topo, base, esquerda) com origem no topo da página."""
    prov = (item.get("prov") or [None])[0]
    if not prov or not prov.get("bbox"):
        return None
    caixa, pagina = prov["bbox"], int(prov.get("page_no", 1))
    topo, base = float(caixa["t"]), float(caixa["b"])
    if str(caixa.get("coord_origin", "BOTTOMLEFT")).upper().startswith("BOTTOM"):
        altura = alturas.get(pagina, 0.0)
        topo, base = altura - topo, altura - base
    return pagina, min(topo, base), max(topo, base), float(caixa["l"])


def _celulas(tabela: dict) -> list[str]:
    return [str(c.get("text", "")).strip() for c in (tabela.get("data") or {}).get("table_cells", [])]


def _eh_mini_calendario(tabela: dict) -> bool:
    celulas = [c for c in _celulas(tabela) if c]
    if len(celulas) < 20:
        return False
    dias = sum(1 for c in celulas if re.fullmatch(r"(?:[A-Za-zçÇ]+\s+)?\d{1,2}|[DSTQ](?:\s*-\s*\d{1,2})?", c))
    return dias / len(celulas) >= 0.8


def _mes_no_texto(texto: str) -> int | None:
    achado = _RE_MES.search(_sem_acento(texto))
    return _MESES_SEM_ACENTO.index(achado.group(1)) + 1 if achado else None


def _faixas_dos_meses(estruturado: dict, alturas: dict[int, float]) -> list[dict]:
    """Uma faixa vertical por mês, definida pelo mini-calendário; o nome vem do título ao lado ou da sequência."""
    faixas = []
    for tabela in estruturado.get("tables") or []:
        caixa = _caixa(tabela, alturas)
        if caixa and _eh_mini_calendario(tabela):
            pagina, topo, base, _ = caixa
            faixas.append({"pagina": pagina, "topo": topo, "base": base,
                           "mes": _mes_no_texto(" ".join(_celulas(tabela)))})
    faixas.sort(key=lambda f: (f["pagina"], f["topo"]))

    for texto in estruturado.get("texts") or []:
        mes = _mes_no_texto(texto.get("text", "")) if len(str(texto.get("text", ""))) <= 12 else None
        caixa = _caixa(texto, alturas)
        if not mes or not caixa:
            continue
        pagina, topo, base, _ = caixa
        centro = (topo + base) / 2
        for faixa in faixas:
            if faixa["pagina"] == pagina and faixa["topo"] <= centro <= faixa["base"]:
                faixa["mes"] = faixa["mes"] or mes

    # Meses sem título reconhecido seguem a sequência do vizinho (julho, agosto, setembro...).
    for i, faixa in enumerate(faixas):
        if faixa["mes"] is None and i and faixas[i - 1]["mes"]:
            faixa["mes"] = faixas[i - 1]["mes"] % 12 + 1
    for i in range(len(faixas) - 2, -1, -1):
        if faixas[i]["mes"] is None and faixas[i + 1]["mes"]:
            faixas[i]["mes"] = (faixas[i + 1]["mes"] - 2) % 12 + 1
    return [f for f in faixas if f["mes"]]


def _data_por_extenso(dia: str, ligacao: str | None, dia_final: str | None, mes: int, ano: str) -> str:
    sufixo = f" de {MESES[mes - 1]}" + (f" de {ano}" if ano else "")
    if dia_final:
        return f"{int(dia):02d} {ligacao} {int(dia_final):02d}{sufixo}"
    return f"{int(dia):02d}{sufixo}"


def _resto_do_markdown(markdown: str) -> str:
    """Do Markdown original, o que não é mês: observações, síntese de dias letivos e versão."""
    linhas = markdown.split("\n")
    inicio = next((i for i, l in enumerate(linhas) if l.startswith("#") and "OBSERVA" in l.upper()), None)
    if inicio is None:
        return ""
    saida: list[str] = []
    pulando_tabela = False
    for linha in linhas[inicio:]:
        limpa = linha.strip()
        if limpa.startswith("|"):
            celulas = [c.strip() for c in limpa.strip("|").split("|")]
            if not pulando_tabela and not (saida and saida[-1].startswith("|")):
                # mini-calendário solto ou legenda de cores: tabelas sem informação para o aluno
                legenda = "domingos e feriados" in _sem_acento(limpa)
                dias = sum(1 for c in celulas if re.fullmatch(r"[DSTQ](?:\s*-\s*\d{1,2})?", c)) >= 5
                dias = dias and not any(len(c) > 3 and c.isalpha() for c in celulas)  # "DIAS LETIVOS | TOTAL" fica
                pulando_tabela = legenda or dias
            if pulando_tabela:
                continue
        else:
            pulando_tabela = False
        if _RE_SO_DIAS.match(limpa) or re.match(r"^- (?:[DSTQ]\s+){0,7}[✓\s]*\d{1,2}(?:\s*[ae]\s*\d{1,2})?\s*[-–]", limpa):
            continue
        if limpa.startswith("## ") and ("CALENDÁRIO" in limpa.upper() or "CURSOS NOS FORMATOS" in limpa.upper()
                                        or _mes_no_texto(limpa)):
            continue
        if _RE_SEMESTRE.search(limpa) or limpa.upper().startswith("CENTRO UNIVERSIT"):
            continue
        saida.append(linha)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(saida)).strip()


def markdown_do_calendario(markdown: str, estruturado: dict) -> str | None:
    """Markdown com um título por mês e a data completa em cada evento; None se o PDF não é um calendário."""
    alturas = {
        int(numero): float((pagina.get("size") or {}).get("height", 0.0))
        for numero, pagina in (estruturado.get("pages") or {}).items()
    }
    faixas = _faixas_dos_meses(estruturado, alturas)
    if len(faixas) < _MINIMO_DE_MESES:
        return None

    textos = estruturado.get("texts") or []
    semestre = next((m for t in textos if (m := _RE_SEMESTRE.search(str(t.get("text", ""))))), None)
    ano = semestre.group(2) if semestre else ""

    eventos: dict[int, list[tuple[float, str]]] = {}
    for item in textos:
        achado = _RE_EVENTO.match(" ".join(str(item.get("text", "")).split()))
        caixa = _caixa(item, alturas)
        if not achado or not caixa:
            continue
        pagina, topo, base, _ = caixa
        da_pagina = [f for f in faixas if f["pagina"] == pagina]
        if not da_pagina:
            continue
        # O primeiro evento de um mês pode ocupar várias linhas: vale o topo, que fica alinhado ao mini-calendário.
        referencia = topo + min(4.0, (base - topo) / 2)
        faixa = min(
            da_pagina,
            key=lambda f: 0.0 if f["topo"] <= referencia <= f["base"] else min(abs(referencia - f["topo"]), abs(referencia - f["base"])),
        )
        dia, ligacao, dia_final, descricao = achado.groups()
        linha = f"- {_data_por_extenso(dia, ligacao, dia_final, faixa['mes'], ano)} – {descricao.strip().rstrip('.')}"
        eventos.setdefault(faixas.index(faixa), []).append((topo, linha))
    if sum(len(v) for v in eventos.values()) < _MINIMO_DE_EVENTOS:
        return None

    titulos = [
        _RE_PAGINA.sub(" ", str(t.get("text", ""))).strip()
        for t in textos if t.get("label") == "section_header" and (c := _caixa(t, alturas)) and c[0] == 1 and c[1] < 90
    ]
    partes = ["# " + (" – ".join(dict.fromkeys(titulos)) or "Calendário Acadêmico")]
    if semestre:
        partes.append(f"{semestre.group(1)}º semestre de {ano}")
    for indice, faixa in enumerate(faixas):
        if indice not in eventos:
            continue
        nome = MESES[faixa["mes"] - 1].capitalize() + (f" de {ano}" if ano else "")
        linhas = [linha for _, linha in sorted(eventos[indice], key=lambda par: par[0])]
        partes.append(f"## {nome}\n\n" + "\n".join(linhas))
    resto = _resto_do_markdown(markdown)
    if resto:
        partes.append(resto)
    return "\n\n".join(partes)
