"""Testes da extração, limpeza, segmentação e indexação dos documentos (seção 3.5 do TCC)."""
import json

from app.documentos import extrair_secoes, juntar_paragrafos, limpar_linhas
from app.indexacao import ARQ_BLOCOS, ARQ_INFO, ARQ_VETORES, carregar_indice, construir_indice, dividir_em_blocos

from conftest import EmbedderFalso


def contar_palavras(texto):
    return len(texto.split())


def test_limpeza_remove_numeros_de_pagina_hifenizacao_e_espacos():
    texto = "Página 1 de 2\nA avaliação é funda-\nmentada no regimento.\n12\nTexto   com    espaços"
    assert limpar_linhas(texto) == ["A avaliação é fundamentada no regimento.", "Texto com espaços"]


def test_linhas_quebradas_no_meio_da_frase_sao_reunidas():
    linhas = ["O aluno que perder a V1", "poderá fazer a segunda chamada.", "", "Novo parágrafo."]
    assert juntar_paragrafos(linhas) == [
        "O aluno que perder a V1 poderá fazer a segunda chamada.",
        "Novo parágrafo.",
    ]


def test_markdown_gera_uma_secao_por_titulo(tmp_path):
    arquivo = tmp_path / "calendario.md"
    arquivo.write_text(
        "# Calendário\n\n## Setembro\n- 30 de setembro – Prazo de trancamento.\n\n## Outubro\n- 05 a 10 de outubro – V1.\n",
        encoding="utf-8",
    )
    secoes = extrair_secoes(arquivo)
    assert [s.titulo for s in secoes] == ["Setembro", "Outubro"]
    assert secoes[0].texto == "30 de setembro – Prazo de trancamento."


def test_texto_detecta_titulos_numerados_e_artigos(tmp_path):
    arquivo = tmp_path / "regras.txt"
    arquivo.write_text(
        "CRITÉRIOS DE AVALIAÇÃO\n"
        "1.1. Normas Institucionais para Avaliação\n"
        "Texto introdutório das normas.\n"
        "Artigo 78 Para efeito de aprovação considera-se o rendimento.\n"
        "Artigo 79 A avaliação será expressa por notas.\n",
        encoding="utf-8",
    )
    secoes = extrair_secoes(arquivo)
    assert [s.titulo for s in secoes] == ["1.1. Normas Institucionais para Avaliação", "Art. 78", "Art. 79"]
    assert "Para efeito de aprovação" in secoes[1].texto


def test_html_ignora_menu_scripts_e_rodape(tmp_path):
    from conftest import CAMPUS_HTML

    arquivo = tmp_path / "campus.html"
    arquivo.write_text(CAMPUS_HTML, encoding="utf-8")
    secoes = extrair_secoes(arquivo)
    tudo = " ".join(s.titulo + " " + s.texto for s in secoes)
    assert [s.titulo for s in secoes] == ["Endereço", "Biblioteca"]
    assert "Rua Lambari" in tudo
    for indesejado in ("Menu principal secreto", "nao indexar", "Rodapé institucional"):
        assert indesejado not in tudo


def test_blocos_respeitam_tamanho_e_tem_sobreposicao():
    texto = " ".join(f"Frase número {i} com algumas palavras de teste." for i in range(40))
    blocos = dividir_em_blocos(texto, 50, 10, contar_palavras)
    assert len(blocos) > 1
    assert all(contar_palavras(b) <= 50 for b in blocos)
    for anterior, seguinte in zip(blocos, blocos[1:]):
        ultima_frase = anterior.split("\n")[-1]
        assert seguinte.startswith(ultima_frase)


def test_frase_maior_que_o_bloco_e_dividida_sem_perder_palavras():
    texto = " ".join(["palavra"] * 130)
    blocos = dividir_em_blocos(texto, 50, 0, contar_palavras)
    assert all(contar_palavras(b) <= 50 for b in blocos)
    assert sum(contar_palavras(b) for b in blocos) == 130


def test_indice_persistido_com_metadados_do_manifesto(ambiente):
    indice, info = construir_indice(ambiente, EmbedderFalso())
    for nome in (ARQ_VETORES, ARQ_BLOCOS, ARQ_INFO):
        assert (ambiente.indice_dir / nome).exists()
    assert info["n_documentos"] == 3
    assert info["n_blocos"] == len(indice.blocos) > 0

    blocos = json.loads((ambiente.indice_dir / ARQ_BLOCOS).read_text(encoding="utf-8"))
    for bloco in blocos:
        for campo in ("documento", "arquivo", "secao", "url", "atualizado_em", "texto"):
            assert campo in bloco
    calendario = [b for b in blocos if b["arquivo"] == "calendario_teste.md"]
    assert calendario[0]["documento"] == "Calendário Letivo de Teste"
    assert calendario[0]["url"] == "https://universo.edu.br/calendarios/"

    recarregado = carregar_indice(ambiente)
    assert recarregado is not None
    assert len(recarregado[0].blocos) == len(indice.blocos)


def test_documento_marcado_para_nao_indexar_e_ignorado(ambiente):
    manifesto = json.loads(ambiente.manifesto.read_text(encoding="utf-8"))
    manifesto["documentos"][0]["indexar"] = False
    ambiente.manifesto.write_text(json.dumps(manifesto, ensure_ascii=False), encoding="utf-8")
    _, info = construir_indice(ambiente, EmbedderFalso())
    assert info["n_documentos"] == 2
