from collections import Counter
from types import SimpleNamespace

from avaliacao.avaliar_sao_goncalo import absteve, carregar_perguntas, metricas_recuperacao, resumir
from avaliacao.avaliar_usuarios import resumir as resumir_usuarios


def test_benchmark_tem_cotas_e_gabaritos() -> None:
    perguntas = carregar_perguntas()
    assert len(perguntas) == 120
    assert Counter(q["dificuldade"] for q in perguntas) == {"facil": 40, "medio": 40, "dificil": 40}
    assert sum(q["respondivel"] for q in perguntas) == 100
    assert all(q["resposta_esperada"] and q["evidencias"] for q in perguntas if q["respondivel"])


def test_metricas_multiplas_evidencias() -> None:
    resultado = metricas_recuperacao([2, 4, 3, 5], [{2}, {3}])
    assert resultado["hit_at_k"] == 1
    assert resultado["mrr"] == 1
    assert resultado["precision_at_k"] == 0.5
    assert resultado["recall_at_k"] == 1
    assert 0 < resultado["ndcg_at_k"] < 1
    assert metricas_recuperacao([8, 9], [{2}, {3}])["recall_at_k"] == 0
    assert metricas_recuperacao([8, 9], [])["hit_at_k"] is None


def test_detector_reconhece_variantes_de_recusa() -> None:
    resposta = SimpleNamespace(modo="llm", resposta="Não a localizou nos documentos oficiais.")
    assert absteve(resposta)
    assert not absteve(SimpleNamespace(modo="llm", resposta="A unidade fica em São Gonçalo."))


def test_resumo_separa_recuperacao_e_abstencao() -> None:
    base = {"categoria": "campus", "dificuldade": "facil", "familia": "f1",
            "modo": "llm", "juiz": {"fidelidade": 4, "pertinencia": 3, "completude": 2, "abstencao": 4},
            "decisao_adequada": 1,
            "tempo_ms": 100, "hit_at_k": 1, "mrr": 1, "precision_at_k": 0.5,
            "recall_at_k": 1, "ndcg_at_k": 0.9, "acerto_termos": 1}
    resultados = [{**base, "respondivel": True, "abstencao": None},
                  {**base, "categoria": "fora_escopo", "familia": "f2", "respondivel": False,
                   "abstencao": 1, "acerto_termos": None}]
    resumo = resumir(resultados)["global"]
    assert resumo["n"] == 2
    assert resumo["hit_at_k"] == 1
    assert resumo["abstencao_correta"] == 1


def test_resumo_usuarios_nao_cria_participantes() -> None:
    tarefa = {"participante": "P01", "grupo": "aluno", "dificuldade": "facil",
              "concluiu": 1, "clareza_1_a_5": 4, "confianca_1_a_5": 3,
              "utilidade_fontes_1_a_5": 5}
    resumo = resumir_usuarios([tarefa])["global"]
    assert resumo["participantes"] == 1
    assert resumo["tarefas"] == 1
    assert resumo["conclusao"] == 1
