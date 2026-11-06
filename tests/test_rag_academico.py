import numpy as np
from types import SimpleNamespace

from app.documentos import inferir_metadados
from app.indexacao import IndiceQdrant, IndiceVetorial
from app.rag import MotorRAG, interpretar_consulta
from app.validacao import resposta_factual_de_eventos, validar_resposta_datas
from app.config import Parametros


def _calendario_blocos():
    base = {
        "document_id": "doc-calendario",
        "arquivo": "calendario.pdf",
        "documento": "Calendário EAD 2026.2",
        "tipo_documento": "calendario",
        "modalidade": "ead",
        "periodo": "2026.2",
        "ano": 2026,
        "url": "https://universo.edu.br/calendario.pdf",
    }
    return [
        {**base, "chunk_id": "agosto", "chunk_ordem": 0, "secao": "Agosto", "mes": 8,
         "texto": "✓ 04 – Início das aulas – veteranos\n✓ 10 – Início das aulas – calouros"},
        {**base, "chunk_id": "dezembro", "chunk_ordem": 1, "secao": "Dezembro", "mes": 12,
         "texto": "✓ 23 – Término do Semestre"},
    ]


def test_inferencia_nao_inventa_campus_e_reconhece_calendario_ead():
    from pathlib import Path

    secoes = [type("Secao", (), {"titulo": "Agosto", "texto": "2º SEMESTRE DE 2026 aulas EAD"})()]
    meta = inferir_metadados(Path("calendario_ead_2026_2.pdf"), secoes, {"url": "https://universo.edu.br/calendario.pdf"})
    assert meta["tipo_documento"] == "calendario"
    assert meta["modalidade"] == "ead"
    assert meta["periodo"] == "2026.2"
    assert "campus" not in meta


def test_calendario_expande_secoes_do_mesmo_documento():
    blocos = _calendario_blocos()

    class Embedder:
        def embed_consulta(self, texto):
            return np.array([1.0, 0.0], dtype="float32")

    indice = IndiceVetorial(np.array([[1.0, 0.0], [0.99, 0.01]], dtype="float32"), blocos)
    motor = MotorRAG(indice, Embedder(), object(), Parametros(top_k=1, limiar=0.0))
    trechos = motor.recuperar("Qual e o periodo de inicio e termino do calendario EAD 2026.2?")
    assert [t["secao"] for t in trechos] == ["Agosto", "Dezembro"]


def test_validacao_confirma_associacao_de_data_e_evento():
    blocos = _calendario_blocos()
    pergunta = "Qual e o periodo de inicio e termino do calendario EAD 2026.2?"
    assert validar_resposta_datas(
        pergunta,
        "As aulas começam em 04/08/2026 e 10/08/2026; o semestre termina em 23/12/2026.",
        blocos,
    )[0]
    assert not validar_resposta_datas(pergunta, "O semestre termina em 04/09/2026.", blocos)[0]


def test_fallback_factual_preserva_mes_ano_e_evento():
    resposta = resposta_factual_de_eventos(
        "Qual e o periodo de inicio e termino do calendario EAD 2026.2?",
        _calendario_blocos(),
    )
    assert "04/08/2026" in resposta
    assert "23/12/2026" in resposta
    assert "Início das aulas" in resposta
    assert "Término do Semestre" in resposta


def test_resposta_llm_com_data_incompleta_e_substituida_por_evidencia():
    blocos = _calendario_blocos()

    class Embedder:
        def embed_consulta(self, texto):
            return np.array([1.0, 0.0], dtype="float32")

    class LLM:
        def gerar(self, mensagens, temperatura):
            return "As principais datas são 04 e 23."

    indice = IndiceVetorial(np.array([[1.0, 0.0], [0.99, 0.01]], dtype="float32"), blocos)
    motor = MotorRAG(indice, Embedder(), LLM(), Parametros(top_k=2, limiar=0.0))
    resposta = motor.responder("Qual e o periodo de inicio e termino do calendario EAD 2026.2?")
    assert resposta.modo == "validado"
    assert "04/08/2026" in resposta.resposta
    assert "23/12/2026" in resposta.resposta


def test_prazo_da_at_prioriza_fechamento_no_calendario_legado():
    """O manual pode liderar a busca vetorial; a sigla precisa achar seu evento."""
    calendario = {
        "id": 7, "arquivo": "calendario_ead_2026_2.pdf", "documento": "Calendário EAD 2026.2",
        "secao": "Novembro", "pagina": 2,
        "texto": "✓ 14 – Fechamento do período de realização das Avaliações AF e AT\n"
                 "✓ 09 a 28 – Período de realização da Avaliação AP - Presencial",
    }
    manual = {
        "id": 8, "arquivo": "manual_2026.pdf", "documento": "Manual do Aluno 2026",
        "secao": "Tratamento excepcional", "texto": "Prazo para AP e AS conforme regulamento.",
    }

    class Embedder:
        def embed_consulta(self, texto):
            return np.array([1.0, 0.0], dtype="float32")

        def embed_documentos(self, textos):
            return np.array([[0.8, 0.6] for _ in textos], dtype="float32")

    class LLM:
        def gerar(self, mensagens, temperatura):
            raise AssertionError("O prazo já foi comprovado pelo evento do calendário")

    indice = IndiceVetorial(np.array([[0.75, 0.66], [0.99, 0.14]], dtype="float32"), [calendario, manual])
    motor = MotorRAG(indice, Embedder(), LLM(), Parametros(top_k=1, limiar=0.6))
    pergunta = "Qual é o prazo limite para realizar a AT?"
    assert interpretar_consulta(pergunta)["codigo_prazo"] == "AT"
    resposta = motor.responder(pergunta)
    assert resposta.modo == "validado"
    assert "14/11/2026" in resposta.resposta
    assert "Novembro, página 2" in resposta.resposta
    assert [fonte["documento"] for fonte in resposta.fontes] == ["Calendário EAD 2026.2"]


def test_data_de_outro_evento_nao_valida_prazo_at():
    trechos = [{
        "arquivo": "calendario_ead_2026_2.pdf", "documento": "Calendário EAD 2026.2",
        "secao": "Novembro", "mes": 11, "ano": 2026,
        "texto": "✓ 14 – Fechamento do período de realização das Avaliações AF e AT\n"
                 "✓ 23 – Entrega de resultados aos alunos",
    }]
    pergunta = "Qual é o prazo limite para realizar a AT?"
    assert not validar_resposta_datas(pergunta, "A AT pode ser realizada até 23/11/2026.", trechos)[0]
    assert validar_resposta_datas(pergunta, "O prazo final da AT é 14/11/2026.", trechos)[0]


def test_prazo_at_encontra_payload_legado_no_qdrant_sem_metadados_novos():
    calendario = SimpleNamespace(id=12, score=0.7, payload={
        "arquivo": "calendario_ead_2026_2.pdf", "documento": "Calendário EAD 2026.2",
        "secao": "Novembro", "pagina": 2,
        "texto": "✓ 14 – Fechamento do período de realização das Avaliações AF e AT",
    })
    manual = SimpleNamespace(id=13, score=0.9, payload={
        "arquivo": "manual.pdf", "documento": "Manual do Aluno",
        "secao": "Tratamento excepcional", "texto": "Prazo geral para outras avaliações.",
    })

    class Cliente:
        def query_points(self, **kwargs):
            return SimpleNamespace(points=[] if kwargs.get("query_filter") else [manual])

        def scroll(self, **kwargs):
            return ([] if kwargs.get("scroll_filter") else [calendario, manual]), None

    class Embedder:
        def embed_consulta(self, texto):
            return np.array([1.0, 0.0], dtype="float32")

        def embed_documentos(self, textos):
            return np.array([[0.8, 0.6] for _ in textos], dtype="float32")

    motor = MotorRAG(IndiceQdrant(Cliente(), "teste"), Embedder(), object(), Parametros(top_k=1, limiar=0.6))
    resposta = motor.responder("Qual é o prazo limite para realizar a AT?")
    assert resposta.modo == "validado"
    assert "14/11/2026" in resposta.resposta
    assert [f["documento"] for f in resposta.fontes] == ["Calendário EAD 2026.2"]
