"""Recupera no Markdown o conteúdo que o Docling deixa de fora ao exportar um PDF.

O Docling pode classificar uma página diagramada (como um fluxograma curricular) como figura.
Os textos continuam no modelo estruturado, com suas posições, mas a exportação troca a figura
inteira por um marcador. Este módulo usa essas posições para remontar o conteúdo:
- fluxograma por períodos: uma tabela de disciplinas por período, pré-requisitos e resumo;
- demais figuras com texto: o texto em ordem de leitura;
- logotipos e figuras sem texto: removidos;
- tabelas com título mesclado: título e pares "item: valor", em vez de colunas repetidas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MARCADOR_FIGURA = "<!-- image -->"

_RE_PERIODO = re.compile(r"^(\d+)º\s*Período$", re.IGNORECASE)
_RE_CODIGO = re.compile(r"^([A-Z]{1,3}\d{2,4}|OPT)(?:\s*\((OL)\))?$")
_RE_CODIGO_LIVRE = re.compile(r"\b[A-Z]{1,3}\d{2,4}\b")
_RE_NUMERO = re.compile(r"^\d{1,4}$")
_RE_REGRA_PRE_REQUISITO = re.compile(r".+?DISCIPLINA\s+\S+\s*\(\d+º\s*PER\.\)")

# Distância máxima ao centro do código para o texto ser título do cartão, e não a coluna lateral.
_MARGEM_TITULO = 22.0
_ALTURA_MAXIMA_CARTAO = 60.0
# Figura pequena e com pouco texto é tratada como logotipo.
_AREA_LOGOTIPO = 0.10
_PALAVRAS_LOGOTIPO = 6


@dataclass(frozen=True)
class _Caixa:
    texto: str
    l: float
    t: float  # topo, com o eixo y crescendo para cima da página
    r: float
    b: float

    @property
    def cx(self) -> float:
        return (self.l + self.r) / 2

    @property
    def cy(self) -> float:
        return (self.t + self.b) / 2

    @property
    def largura(self) -> float:
        return self.r - self.l


def _caixa_de(prov: dict, texto: str, paginas: dict) -> _Caixa | None:
    caixa = prov.get("bbox") or {}
    try:
        l, t, r, b = (float(caixa[k]) for k in "ltrb")
    except (KeyError, TypeError, ValueError):
        return None
    if str(caixa.get("coord_origin", "BOTTOMLEFT")).upper().endswith("TOPLEFT"):
        altura = float((paginas.get(str(prov.get("page_no"))) or {}).get("size", {}).get("height", 0))
        t, b = altura - t, altura - b
    return _Caixa(" ".join(str(texto).split()), l, t, r, b)


def _caixas_da_figura(figura: dict, estruturado: dict) -> list[_Caixa]:
    textos = estruturado.get("texts") or []
    paginas = estruturado.get("pages") or {}
    caixas = []
    for filho in figura.get("children") or []:
        correspondencia = re.fullmatch(r"#/texts/(\d+)", str(filho.get("$ref", "")))
        if not correspondencia or int(correspondencia.group(1)) >= len(textos):
            continue
        item = textos[int(correspondencia.group(1))]
        if not item.get("prov") or not str(item.get("text", "")).strip():
            continue
        caixa = _caixa_de(item["prov"][0], item["text"], paginas)
        if caixa:
            caixas.append(caixa)
    return caixas


def _eh_logotipo(figura: dict, caixas: list[_Caixa], estruturado: dict) -> bool:
    palavras = sum(len(c.texto.split()) for c in caixas)
    if palavras > _PALAVRAS_LOGOTIPO or not figura.get("prov"):
        return palavras == 0
    prov = figura["prov"][0]
    caixa = prov.get("bbox") or {}
    pagina = (estruturado.get("pages") or {}).get(str(prov.get("page_no"))) or {}
    tamanho = pagina.get("size") or {}
    area_pagina = float(tamanho.get("width", 0)) * float(tamanho.get("height", 0))
    if not area_pagina or not all(k in caixa for k in "ltrb"):
        return False
    area = abs(float(caixa["r"]) - float(caixa["l"])) * abs(float(caixa["t"]) - float(caixa["b"]))
    return area / area_pagina < _AREA_LOGOTIPO


# ---------------------------------------------------------------- texto em ordem de leitura
def _em_linhas(caixas: list[_Caixa]) -> list[list[_Caixa]]:
    linhas: list[list[_Caixa]] = []
    for caixa in sorted(caixas, key=lambda c: -c.t):
        if linhas and min(c.b for c in linhas[-1]) < caixa.cy:
            linhas[-1].append(caixa)
        else:
            linhas.append([caixa])
    return [sorted(linha, key=lambda c: c.l) for linha in linhas]


def _texto_em_ordem(caixas: list[_Caixa]) -> str:
    return "\n\n".join(" ".join(c.texto for c in linha) for linha in _em_linhas(caixas))


# ---------------------------------------------------------------- fluxograma por períodos
def _separar_periodos(caixas: list[_Caixa]) -> tuple[dict[int, _Caixa], list[_Caixa]]:
    periodos: dict[int, _Caixa] = {}
    restantes = []
    for caixa in caixas:
        correspondencia = _RE_PERIODO.match(caixa.texto)
        if correspondencia and int(correspondencia.group(1)) not in periodos:
            periodos[int(correspondencia.group(1))] = caixa
        else:
            restantes.append(caixa)
    return periodos, restantes


def _distribuir_em_colunas(periodos: dict[int, _Caixa], caixas: list[_Caixa]) -> tuple[dict[int, list[_Caixa]], list[_Caixa]]:
    """Coloca cada texto no período mais próximo; textos largos não pertencem a uma coluna."""
    centros = sorted(c.cx for c in periodos.values())
    espaco = min((b - a for a, b in zip(centros, centros[1:])), default=float("inf"))
    colunas: dict[int, list[_Caixa]] = {numero: [] for numero in periodos}
    fora = []
    for caixa in caixas:
        if caixa.largura > espaco:
            fora.append(caixa)
            continue
        numero = min(periodos, key=lambda n: abs(periodos[n].cx - caixa.cx))
        colunas[numero].append(caixa)
    return colunas, fora


def _ler_cartoes(cabecalho: _Caixa, coluna: list[_Caixa]) -> tuple[list[dict], list[_Caixa]]:
    # Códigos de pré-requisito ficam à direita do centro da coluna, na faixa lateral do cartão.
    ancoras = sorted(
        (c for c in coluna if _RE_CODIGO.match(c.texto) and c.cx <= cabecalho.cx),
        key=lambda c: -c.t,
    )
    usados: set[_Caixa] = set(ancoras)
    cartoes = []
    for n, ancora in enumerate(ancoras):
        piso = ancora.t - _ALTURA_MAXIMA_CARTAO
        if n + 1 < len(ancoras):
            piso = max(piso, ancoras[n + 1].t)
        membros = [c for c in coluna if c not in usados and piso < c.cy < ancora.t + 2]
        usados.update(membros)
        titulo = [c for c in membros if abs(c.cx - ancora.cx) < _MARGEM_TITULO]
        lateral = sorted((c for c in membros if c not in titulo), key=lambda c: -c.t)
        numeros = [c.texto for c in lateral if _RE_NUMERO.match(c.texto)]
        requisitos: list[str] = []
        for caixa in lateral:
            requisitos += [r for r in _RE_CODIGO_LIVRE.findall(caixa.texto) if r not in requisitos]
        codigo = _RE_CODIGO.match(ancora.texto)
        cartoes.append({
            "codigo": codigo.group(1),
            "marcacao": codigo.group(2) or "—",
            "nome": " ".join(c.texto for c in sorted(titulo, key=lambda c: -c.t)),
            "horas": f"{int(numeros[0])} h" if numeros else "Não indicado",
            "creditos": str(int(numeros[1])) if len(numeros) > 1 else "Não indicado",
            "requisitos": ", ".join(requisitos) or "Não indicado",
        })
    return cartoes, [c for c in coluna if c not in usados]


def _tabela_de_cartoes(cartoes: list[dict]) -> str:
    linhas = [
        "| Código | Disciplina | Carga horária | Créditos | Pré-requisitos | Marcação |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    linhas += [
        f"| {c['codigo']} | {c['nome']} | {c['horas']} | {c['creditos']} | {c['requisitos']} | {c['marcacao']} |"
        for c in cartoes
    ]
    return "\n".join(linhas)


def _bloco_pre_requisitos(caixas: list[_Caixa]) -> str:
    partes = ["## Pré-requisitos oficiais"]
    for caixa in sorted(caixas, key=lambda c: -c.t):
        if re.search(r"PR[ÉE]-?REQUISITOS", caixa.texto, re.IGNORECASE):
            continue
        regras = _RE_REGRA_PRE_REQUISITO.findall(caixa.texto)
        partes.append("\n".join(f"- {r.strip()}" for r in regras) if regras else caixa.texto)
    return "\n\n".join(partes)


def _renderizar_fluxograma(periodos: dict[int, _Caixa], caixas: list[_Caixa]) -> str | None:
    """Disciplinas por período; devolve None se a figura não tiver cartões com código."""
    colunas, fora = _distribuir_em_colunas(periodos, caixas)
    por_periodo: dict[int, list[dict]] = {}
    sobras = list(fora)
    for numero in sorted(colunas):
        cartoes, resto = _ler_cartoes(periodos[numero], colunas[numero])
        sobras += resto
        if cartoes:
            por_periodo[numero] = cartoes
    if not por_periodo:
        return None

    partes = ["## Disciplinas por período"]
    for numero, cartoes in por_periodo.items():
        partes.append(f"### {numero}º período\n\n{_tabela_de_cartoes(cartoes)}")

    componentes = {c["codigo"]: c for cartoes in por_periodo.values() for c in cartoes}
    optativos = sum(1 for c in componentes.values() if c["codigo"] == "OPT" or "optativa" in c["nome"].lower())
    texto_optativos = "1 identificado como optativo" if optativos == 1 else f"{optativos} identificados como optativos"
    partes.append(
        "## Contagem dos componentes curriculares\n\n"
        f"A matriz apresenta {len(componentes)} componentes curriculares em {len(por_periodo)} períodos: "
        f"{len(componentes) - optativos} não identificados como optativos e {texto_optativos}."
    )

    if any(re.search(r"PR[ÉE]-?REQUISITOS", c.texto, re.IGNORECASE) for c in sobras):
        partes.append(_bloco_pre_requisitos(sobras))
    elif sobras:
        partes.append(_texto_em_ordem(sobras))
    return "\n\n".join(partes)


def _renderizar_resumo_por_periodo(periodos: dict[int, _Caixa], caixas: list[_Caixa]) -> str | None:
    """Quadro com rótulos e valores em cada período (carga horária, aulas semanais etc.)."""
    colunas, _ = _distribuir_em_colunas(periodos, caixas)
    rotulos: list[str] = []
    linhas: dict[int, dict[str, str]] = {}
    for numero in sorted(colunas):
        valores: dict[str, str] = {}
        for linha in _em_linhas(colunas[numero]):
            if len(linha) >= 2:
                rotulo, valor = " ".join(c.texto for c in linha[:-1]), linha[-1].texto
            elif re.match(r"^[\d.]+\s*horas$", linha[0].texto, re.IGNORECASE):
                rotulo, valor = "Total", linha[0].texto
            else:
                continue
            valores[rotulo] = valor
            if rotulo not in rotulos:
                rotulos.append(rotulo)
        if valores:
            linhas[numero] = valores
    if not linhas:
        return None
    tabela = [
        "| Período | " + " | ".join(rotulos) + " |",
        "| --- | " + " | ".join("---" for _ in rotulos) + " |",
    ]
    tabela += [
        f"| {numero}º período | " + " | ".join(valores.get(r, "—") for r in rotulos) + " |"
        for numero, valores in linhas.items()
    ]
    return "## Resumo da carga horária por período\n\n" + "\n".join(tabela)


# ---------------------------------------------------------------- figuras
def _renderizar_figura(figura: dict, estruturado: dict) -> str:
    caixas = _caixas_da_figura(figura, estruturado)
    if _eh_logotipo(figura, caixas, estruturado):
        return ""
    periodos, restantes = _separar_periodos(caixas)
    if len(periodos) >= 2:
        conteudo = _renderizar_fluxograma(periodos, restantes) or _renderizar_resumo_por_periodo(periodos, restantes)
        if conteudo:
            return conteudo
    return _texto_em_ordem(caixas)


def _substituir_figuras(markdown: str, estruturado: dict) -> str:
    figuras = estruturado.get("pictures") or []
    partes = [_renderizar_figura(figura, estruturado) for figura in figuras]
    pedacos = markdown.split(MARCADOR_FIGURA)
    if len(pedacos) - 1 != len(partes):
        # Sem correspondência segura entre marcadores e figuras, nada se perde: o texto vai para o final.
        return "\n\n".join([p for p in ["".join(pedacos).strip(), *partes] if p])
    saida = [pedacos[0]]
    for parte, pedaco in zip(partes, pedacos[1:]):
        saida += [parte, pedaco]
    return "".join(saida)


# ---------------------------------------------------------------- tabelas
def _tabela_com_titulo(tabela: dict) -> str | None:
    """Reescreve tabela cuja primeira linha é um título mesclado; devolve None para as demais."""
    grade = (tabela.get("data") or {}).get("grid") or []
    if len(grade) < 2:
        return None
    textos = [[" ".join(str(c.get("text", "")).split()) for c in linha] for linha in grade]
    titulo = textos[0][0]
    if not titulo or len(set(textos[0])) != 1 or len(textos[0]) % 2:
        return None
    if titulo.isupper():
        titulo = titulo.capitalize()
    itens = []
    for linha in textos[1:]:
        if len(set(linha)) == 1:
            itens.append(f"- {linha[0]}")
        else:
            itens += [f"- {linha[i]}: {linha[i + 1]}" for i in range(0, len(linha), 2)]
    return f"## {titulo}\n\n" + "\n".join(itens)


def _substituir_tabelas(markdown: str, estruturado: dict) -> str:
    tabelas = estruturado.get("tables") or []
    blocos = list(re.finditer(r"(?:^\|.*(?:\n|$))+", markdown, re.MULTILINE))
    # Tabelas criadas por este módulo vêm depois das do Docling; só há troca com contagem exata.
    gerados = len(re.findall(r"^\| --- ", markdown, re.MULTILINE))
    if len(blocos) - gerados != len(tabelas):
        return markdown
    originais = [b for b in blocos if not re.search(r"^\| --- ", b.group(0), re.MULTILINE)]
    if len(originais) != len(tabelas):
        return markdown
    for bloco, tabela in reversed(list(zip(originais, tabelas))):
        novo = _tabela_com_titulo(tabela)
        if novo:
            fim = "\n" if bloco.group(0).endswith("\n") else ""
            markdown = markdown[:bloco.start()] + novo + fim + markdown[bloco.end():]
    return markdown


# ---------------------------------------------------------------- cabeçalhos e rodapés de página
_RE_NUMERO_PAGINA = re.compile(
    r"^(?:\d+º\s+SEMESTRE\s+DE\s+\d{4}\s*)?(?:Página|Pág\.?)\s*\d+\s*(?:de|/)\s*\d+$", re.IGNORECASE
)
# A partir de quantas repetições uma linha curta é tratada como texto de cabeçalho/rodapé de página.
_REPETICOES_DE_PAGINA = 4
_TAMANHO_MAXIMO_REPETIDA = 120


def _sem_marcacao(linha: str) -> str:
    return re.sub(r"^#{1,6}\s+", "", linha.strip())


def remover_textos_de_pagina(markdown: str) -> str:
    """Remove cabeçalhos e rodapés que se repetem em cada página.

    O Docling pode exportar o cabeçalho da página como título de seção. Repetido a cada página, ele
    substitui o título verdadeiro (por exemplo, "17. AVALIAÇÃO DO RENDIMENTO ESCOLAR") e os trechos
    perdem o contexto. Linhas de tabela e itens de lista nunca são removidos.
    """
    linhas = markdown.splitlines()
    contagem: dict[str, int] = {}
    for linha in linhas:
        texto = _sem_marcacao(linha)
        if texto and not texto.startswith(("|", "-", "*")) and len(texto) <= _TAMANHO_MAXIMO_REPETIDA:
            contagem[texto] = contagem.get(texto, 0) + 1
    repetidas = {texto for texto, n in contagem.items() if n >= _REPETICOES_DE_PAGINA}
    saida = [
        linha for linha in linhas
        if _sem_marcacao(linha) not in repetidas and not _RE_NUMERO_PAGINA.match(_sem_marcacao(linha))
    ]
    return "\n".join(saida)


def markdown_estruturado(markdown: str, estruturado: dict) -> str:
    """Devolve o Markdown do Docling com figuras e tabelas reconstruídas a partir das posições."""
    from .docling_calendario import markdown_do_calendario

    calendario = markdown_do_calendario(markdown, estruturado)
    if calendario:
        return calendario
    resultado = _substituir_figuras(markdown, estruturado)
    resultado = _substituir_tabelas(resultado, estruturado)
    resultado = remover_textos_de_pagina(resultado)
    return re.sub(r"\n{3,}", "\n\n", resultado).strip()
