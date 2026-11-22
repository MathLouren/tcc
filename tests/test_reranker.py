"""Fase de recuperação: corte relativo de similaridade (margem_relativa) e reordenação por cross-encoder."""
import numpy as np
import pytest

from app.config import Parametros
from app.rag import MotorRAG
from app.reranker import Reranker, criar_reranker

CONSULTA = "qual o prazo de trancamento"


class IndiceFixo:
    """Busca vetorial com similaridades definidas pelo teste (já em ordem decrescente)."""

    def __init__(self, similaridades):
        self.blocos = [
            {"id": i, "documento": "Manual", "arquivo": "manual.md", "secao": f"Seção {i}", "texto": f"texto {i}"}
            for i in range(len(similaridades))
        ]
        self._similaridades = similaridades
        self.lexico = None

    def buscar(self, vetor, k):
        return list(enumerate(self._similaridades))[:k]


class EmbedderQualquer:
    def embed_consulta(self, texto):
        return np.zeros(3, dtype="float32")


class RerankerFalso(Reranker):
    def __init__(self, notas, candidatos=12, falhar=False):
        super().__init__("falso", candidatos)
        self.notas, self.falhar, self.vistos = notas, falhar, []

    def pontuar(self, consulta, textos):
        if self.falhar:
            raise RuntimeError("modelo não carregou")
        self.vistos.append(len(textos))
        return [self.notas[int(t.split("texto ")[1])] for t in textos]


def motor(similaridades, reranker=None, **parametros):
    return MotorRAG(IndiceFixo(similaridades), EmbedderQualquer(), None, Parametros(**parametros), reranker)


def ids(trechos):
    return [t["id"] for t in trechos]


def test_sem_margem_vale_so_o_limiar():
    m = motor([0.92, 0.89, 0.86, 0.84, 0.50], limiar=0.6, top_k=4)
    assert ids(m.recuperar(CONSULTA)) == [0, 1, 2, 3]


def test_margem_descarta_trechos_longe_do_melhor():
    m = motor([0.92, 0.905, 0.89, 0.84], limiar=0.6, top_k=4, margem_relativa=0.025)
    assert ids(m.recuperar(CONSULTA)) == [0, 1]  # 0,89 e 0,84 ficam a mais de 0,025 do melhor (0,92)


def test_margem_nunca_fica_abaixo_do_limiar():
    m = motor([0.62, 0.61, 0.58], limiar=0.6, top_k=4, margem_relativa=0.5)
    assert ids(m.recuperar(CONSULTA)) == [0, 1]


def test_margem_fora_do_intervalo_e_recusada():
    with pytest.raises(ValueError, match="margem_relativa"):
        Parametros(margem_relativa=1.5).validar()


def test_reranker_reordena_os_candidatos_e_corta_em_top_k():
    reranker = RerankerFalso(notas=[0.1, 0.2, 5.0, 3.0, 9.0])
    m = motor([0.92, 0.91, 0.90, 0.89, 0.88], reranker, limiar=0.6, top_k=2)
    trechos = m.recuperar(CONSULTA)
    assert ids(trechos) == [4, 2]
    assert trechos[0]["similaridade"] == 0.88  # a similaridade exibida continua a da busca


def test_reranker_so_ve_os_primeiros_candidatos():
    reranker = RerankerFalso(notas=[0, 0, 0, 0, 0, 0, 0, 9], candidatos=3)
    m = motor([0.95, 0.94, 0.93, 0.92, 0.91, 0.90, 0.89, 0.88], reranker, limiar=0.6, top_k=2)
    assert 7 not in ids(m.recuperar(CONSULTA))
    assert reranker.vistos == [3]


def test_falha_do_reranker_mantem_a_ordem_da_busca():
    m = motor([0.92, 0.91, 0.90], RerankerFalso(notas=[], falhar=True), limiar=0.6, top_k=2)
    assert ids(m.recuperar(CONSULTA)) == [0, 1]


def test_criar_reranker_desligado_por_padrao():
    class Config:
        reranker_modelo = ""

    assert criar_reranker(Config()) is None
