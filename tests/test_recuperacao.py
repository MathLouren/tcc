"""Testes da recuperação semântica e do filtro de relevância (seção 3.6 do TCC)."""
import numpy as np
import pytest

from app.indexacao import IndiceVetorial

from conftest import PERGUNTA_FORA, PERGUNTA_TRANCAMENTO


def test_trecho_mais_relevante_vem_primeiro(servico):
    trechos = servico.motor.recuperar(PERGUNTA_TRANCAMENTO)
    assert trechos, "a pergunta deveria recuperar ao menos um trecho"
    assert trechos[0]["secao"] == "Setembro"
    assert "trancamento" in trechos[0]["texto"]


def test_similaridades_em_ordem_decrescente(servico):
    servico.atualizar_parametros({"limiar": 0.0})
    trechos = servico.motor.recuperar("segunda chamada da V1 com requerimento")
    similaridades = [t["similaridade"] for t in trechos]
    assert similaridades == sorted(similaridades, reverse=True)


def test_limiar_descarta_trechos_irrelevantes(servico):
    assert servico.motor.recuperar(PERGUNTA_FORA) == []


def test_top_k_limita_a_quantidade_de_trechos(servico):
    servico.atualizar_parametros({"top_k": 1, "limiar": 0.0})
    assert len(servico.motor.recuperar("matrícula prazo aluno")) == 1


def test_busca_numpy_equivale_a_faiss(servico):
    pytest.importorskip("faiss")
    indice = servico.indice
    assert indice.usa_faiss
    alternativo = IndiceVetorial(indice.vetores, indice.blocos)
    alternativo._faiss = None  # força a busca em NumPy
    vetor = servico.embedder.embed_consulta("segunda chamada da V1")
    com_faiss = indice.buscar(vetor, 3)
    com_numpy = alternativo.buscar(vetor, 3)
    assert com_faiss[0][0] == com_numpy[0][0]
    assert np.allclose([s for _, s in com_faiss], [s for _, s in com_numpy], atol=1e-5)


def test_indice_vazio_nao_quebra_a_busca():
    indice = IndiceVetorial(np.zeros((0, 0), dtype="float32"), [])
    assert indice.buscar(np.ones(8, dtype="float32"), 4) == []
