"""Reconstrução do Markdown a partir das posições dos textos no modelo do Docling."""
from app.docling_layout import MARCADOR_FIGURA, markdown_estruturado, remover_textos_de_pagina


def _item(texto, l, t, r=None, b=None):
    return {
        "text": texto,
        "prov": [{"page_no": 1, "bbox": {"l": l, "t": t, "r": r or l + 20, "b": b or t - 6, "coord_origin": "BOTTOMLEFT"}}],
    }


def _documento(figuras, tabelas=None):
    """Monta o modelo estruturado; cada figura é (caixa da figura, itens de texto)."""
    textos, pictures = [], []
    for caixa, itens in figuras:
        filhos = []
        for item in itens:
            filhos.append({"$ref": f"#/texts/{len(textos)}"})
            textos.append(item)
        l, t, r, b = caixa
        pictures.append({
            "children": filhos,
            "prov": [{"page_no": 1, "bbox": {"l": l, "t": t, "r": r, "b": b, "coord_origin": "BOTTOMLEFT"}}],
        })
    return {
        "texts": textos,
        "pictures": pictures,
        "tables": tabelas or [],
        "pages": {"1": {"size": {"width": 800, "height": 600}}},
    }


def _cartao(codigo, titulo, x, topo, horas="80", creditos="04", requisitos=()):
    itens = [_item(codigo, x, topo, x + 30)]
    itens += [_item(linha, x, topo - 15 - 7 * n, x + 30) for n, linha in enumerate(titulo)]
    itens += [_item(horas, x + 50, topo, x + 56), _item(creditos, x + 50, topo - 15, x + 56)]
    if requisitos:
        itens.append(_item("P.R.:", x + 50, topo - 23, x + 58))
        itens += [_item(r, x + 50, topo - 29 - 6 * n, x + 60) for n, r in enumerate(requisitos)]
    return itens


FLUXOGRAMA = [
    _item("1º Período", 110, 530, 150),
    _item("2º Período", 210, 530, 250),
    *_cartao("N014", ["Lógica de", "Programação I"], 100, 500),
    *_cartao("N004 (OL)", ["Ciências Exatas"], 100, 430),
    *_cartao("N101", ["Lógica de", "Programação II"], 200, 500, requisitos=["N014"]),
    *_cartao("OPT", ["Optativa em", "Sistemas"], 200, 430, horas="180", creditos="09"),
    _item("PRÉ-REQUISITOS (DISCIPLINAS QUE PRENDEM OUTRAS DISCIPLINAS)", 100, 360, 400),
    _item("(1ºPER.) N014 - PRENDE A DISCIPLINA N101 (2º PER.) (2ºPER.) N101 - PRENDE A DISCIPLINA N200 (3º PER.)", 100, 340, 500),
]
RESUMO = [
    _item("1º Período", 110, 130, 150),
    _item("2º Período", 210, 130, 250),
    _item("Carga Horária", 90, 120, 130), _item("320", 150, 120, 160),
    _item("350 horas", 110, 100, 150),
    _item("Carga Horária", 190, 120, 230), _item("420", 250, 120, 260),
    _item("450 horas", 210, 100, 250),
]


def _converter_fluxograma():
    estruturado = _documento([((90, 540, 500, 330), FLUXOGRAMA), ((90, 140, 500, 90), RESUMO)])
    return markdown_estruturado(f"# Grade\n\n{MARCADOR_FIGURA}\n\n{MARCADOR_FIGURA}\n", estruturado)


def test_fluxograma_vira_tabela_de_disciplinas_por_periodo():
    markdown = _converter_fluxograma()
    assert MARCADOR_FIGURA not in markdown
    assert "## Disciplinas por período" in markdown
    assert "### 1º período" in markdown and "### 2º período" in markdown
    assert "| N014 | Lógica de Programação I | 80 h | 4 | Não indicado | — |" in markdown
    assert "| N004 | Ciências Exatas | 80 h | 4 | Não indicado | OL |" in markdown
    assert "| N101 | Lógica de Programação II | 80 h | 4 | N014 | — |" in markdown
    assert "| OPT | Optativa em Sistemas | 180 h | 9 | Não indicado | — |" in markdown


def test_codigo_de_pre_requisito_nao_vira_disciplina():
    markdown = _converter_fluxograma()
    linhas_n014 = [linha for linha in markdown.splitlines() if linha.startswith("| N014 |")]
    assert len(linhas_n014) == 1
    assert "A matriz apresenta 4 componentes curriculares em 2 períodos" in markdown
    assert "3 não identificados como optativos e 1 identificado como optativo." in markdown


def test_regras_de_pre_requisito_ficam_uma_por_linha():
    markdown = _converter_fluxograma()
    assert "## Pré-requisitos oficiais" in markdown
    assert "- (1ºPER.) N014 - PRENDE A DISCIPLINA N101 (2º PER.)\n" in markdown
    assert "- (2ºPER.) N101 - PRENDE A DISCIPLINA N200 (3º PER.)" in markdown


def test_quadro_por_periodo_vira_tabela_de_resumo():
    markdown = _converter_fluxograma()
    assert "## Resumo da carga horária por período" in markdown
    assert "| Período | Carga Horária | Total |" in markdown
    assert "| 1º período | 320 | 350 horas |" in markdown
    assert "| 2º período | 420 | 450 horas |" in markdown


def test_logotipo_e_removido_sem_perder_o_restante():
    logo = ((0, 590, 100, 560), [_item("JUmiGiali", 10, 585, 80)])
    markdown = markdown_estruturado(f"{MARCADOR_FIGURA}\n\n## Título", _documento([logo]))
    assert markdown == "## Título"


def test_figura_sem_texto_e_removida():
    markdown = markdown_estruturado(f"Antes\n\n{MARCADOR_FIGURA}\n\nDepois", _documento([((0, 500, 700, 100), [])]))
    assert markdown == "Antes\n\nDepois"


def test_figura_comum_tem_o_texto_preservado_em_ordem_de_leitura():
    itens = [
        _item("Segunda linha do diagrama explicativo", 100, 300, 300),
        _item("Primeira parte", 100, 400, 200),
        _item("na mesma linha com vários detalhes", 210, 400, 400),
    ]
    markdown = markdown_estruturado(MARCADOR_FIGURA, _documento([((90, 420, 500, 280), itens)]))
    assert markdown == "Primeira parte na mesma linha com vários detalhes\n\nSegunda linha do diagrama explicativo"


def test_marcadores_em_numero_diferente_nao_perdem_texto():
    itens = [_item("Conteúdo importante da figura em questão aqui", 100, 300, 400)]
    markdown = markdown_estruturado("Sem marcador", _documento([((90, 420, 500, 280), itens)]))
    assert "Sem marcador" in markdown
    assert "Conteúdo importante da figura em questão aqui" in markdown


def test_cabecalho_repetido_em_cada_pagina_deixa_de_ser_titulo():
    cabecalho = "## UNIVERSIDADE SALGADO DE OLIVEIRA - MANUAL"
    paginas = [
        "## 17. AVALIAÇÃO DO RENDIMENTO ESCOLAR\n\nRegra A.",
        "Regra B, continuação da seção 17.",
        "Regra C.\n\n## 18. REVISÃO\n\nPrazo de 5 dias.",
        "Texto final.",
    ]
    markdown = "\n\n".join(f"{cabecalho}\n\n2º SEMESTRE DE 2026 Página {n} de 80\n\n{p}" for n, p in enumerate(paginas, 1))
    resultado = remover_textos_de_pagina(markdown)
    assert "UNIVERSIDADE SALGADO" not in resultado
    assert "Página" not in resultado
    titulos = [l for l in resultado.splitlines() if l.startswith("#")]
    assert titulos == ["## 17. AVALIAÇÃO DO RENDIMENTO ESCOLAR", "## 18. REVISÃO"]
    assert "Regra B, continuação da seção 17." in resultado


def test_linhas_de_tabela_e_lista_repetidas_sao_preservadas():
    markdown = "\n".join(["| N001 | Disciplina | 80 h |"] * 5 + ["- 80 h"] * 5 + ["## Título único"])
    assert remover_textos_de_pagina(markdown) == markdown


def test_titulo_que_se_repete_poucas_vezes_e_mantido():
    markdown = "## Observações\n\nA.\n\n## Observações\n\nB.\n\n## Observações\n\nC."
    assert remover_textos_de_pagina(markdown) == markdown


def _grade(linhas):
    return {"data": {"grid": [[{"text": t} for t in linha] for linha in linhas]}}


def test_tabela_com_titulo_mesclado_vira_titulo_e_pares():
    tabela = _grade([
        ["TOTALIZAÇÃO DE CARGA HORÁRIA"] * 4,
        ["Horas Teóricas", "2320 h", "Mínimo de Optativas", "80 h"],
        ["MÍNIMO = 3.000 horas"] * 4,
    ])
    markdown = markdown_estruturado(
        "Antes\n\n| TOTAL - A | TOTAL - B |\n|---|---|\n| x | y |\n\nDepois",
        {"tables": [tabela], "pictures": [], "texts": []},
    )
    assert "## Totalização de carga horária" in markdown
    assert "- Horas Teóricas: 2320 h\n- Mínimo de Optativas: 80 h\n- MÍNIMO = 3.000 horas" in markdown
    assert "| TOTAL" not in markdown
    assert markdown.endswith("Depois")


def test_tabela_comum_permanece_como_o_docling_gerou():
    original = "| Nome | Nota |\n|---|---|\n| Ana | 8 |"
    tabela = _grade([["Nome", "Nota"], ["Ana", "8"]])
    assert markdown_estruturado(original, {"tables": [tabela], "pictures": [], "texts": []}) == original


def test_tabelas_geradas_nao_deslocam_as_tabelas_do_docling():
    tabela = _grade([["TÍTULO GERAL"] * 2, ["Item", "Valor"]])
    estruturado = _documento([((90, 540, 500, 330), FLUXOGRAMA)], [tabela])
    markdown = markdown_estruturado(f"{MARCADOR_FIGURA}\n\n| A | B |\n|---|---|\n| 1 | 2 |", estruturado)
    assert "| N014 |" in markdown
    assert "## Título geral\n\n- Item: Valor" in markdown
    assert "| A | B |" not in markdown


def _mini_calendario(pagina, topo, base, primeira=""):
    celulas = [{"text": t} for t in (["D", "S", "T", "Q", "Q", "S", "S"] + [primeira or "01"] + [f"{d:02d}" for d in range(2, 29)])]
    return {"prov": [{"page_no": pagina, "bbox": {"l": 50, "t": topo, "r": 200, "b": base, "coord_origin": "BOTTOMLEFT"}}],
            "data": {"table_cells": celulas}}


def _texto(texto, topo, base, esquerda=210, rotulo="list_item", pagina=1):
    return {"text": texto, "label": rotulo,
            "prov": [{"page_no": pagina, "bbox": {"l": esquerda, "t": topo, "r": 560, "b": base, "coord_origin": "BOTTOMLEFT"}}]}


def test_calendario_diagramado_atribui_cada_evento_ao_mes_pela_posicao():
    from app.docling_calendario import markdown_do_calendario

    estruturado = {
        "pages": {"1": {"size": {"width": 595, "height": 842}}},
        "tables": [_mini_calendario(1, 730, 610, "JULHO 01"), _mini_calendario(1, 585, 405), _mini_calendario(1, 380, 250)],
        "texts": [
            _texto("CALENDÁRIO ACADÊMICO", 815, 790, 330, "section_header"),
            _texto("2º SEMESTRE DE 2026 Página 1 de 2", 773, 753, 486, "text"),
            _texto("AGOSTO", 509, 472, 38, "section_header"),
            _texto("SETEMBRO", 324, 280, 38, "section_header"),
            _texto("D S T Q Q S S ✓ 01 a 31- Período para matrículas", 731, 701, 65),
            _texto("19 a 31 - Recesso para os docentes", 618, 611),
            _texto("04 - Início das aulas - veteranos", 537, 529),
            _texto("26 - Webinário", 435, 428),
            _texto("17 - Abertura do período de realização da Avaliação AF", 424, 416),
            _texto("D S T Q Q S S ✓ 01 a 10 - Matrículas fora do prazo", 373, 352, 65),
            _texto("14 - Abertura do período de realização da Avaliação AT", 326, 318),
            _texto("30 - Prazo final para solicitação de trancamento de matrícula", 247, 239),
        ],
    }
    original = "## AGOSTO\n\n## SETEMBRO\n\n## OBSERVAÇÕES:\n\n- 4) Não há 2ª chamada para avaliações de AP e AS.\n\n| D | S | T | Q | Q | S | S |\n|---|---|---|---|---|---|---|\n| 01 | 02 | 03 | 04 | 05 | 06 | 07 |"

    markdown = markdown_do_calendario(original, estruturado)

    julho, agosto, setembro = (markdown.split("## ")[i] for i in (1, 2, 3))
    assert julho.startswith("Julho de 2026") and "01 a 31 de julho de 2026 – Período para matrículas" in julho
    assert "19 a 31 de julho de 2026 – Recesso para os docentes" in julho
    assert agosto.startswith("Agosto de 2026") and "04 de agosto de 2026 – Início das aulas - veteranos" in agosto
    assert "17 de agosto de 2026 – Abertura do período de realização da Avaliação AF" in agosto  # fora de ordem no PDF
    assert "14 de setembro de 2026 – Abertura do período de realização da Avaliação AT" in setembro
    assert "30 de setembro de 2026 – Prazo final" in setembro
    assert "Não há 2ª chamada" in markdown and "| D | S |" not in markdown


def test_documento_comum_nao_e_tratado_como_calendario():
    from app.docling_calendario import markdown_do_calendario

    assert markdown_do_calendario("# Manual", {"pages": {}, "tables": [], "texts": [_texto("10 - item", 500, 490)]}) is None
