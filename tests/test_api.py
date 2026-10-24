"""Testes da API REST e da interface web (seção 3.7 e Quadro 3 do TCC)."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import criar_app

from conftest import PERGUNTA_TRANCAMENTO, EmbedderFalso, LLMFalso

ADMIN = {"X-Admin-Key": "chave-teste"}


@pytest.fixture
def cliente(ambiente):
    return TestClient(criar_app(ambiente, embedder=EmbedderFalso(), llm=LLMFalso()))


def perguntar(cliente, pergunta=PERGUNTA_TRANCAMENTO, sessao_id=None):
    resposta = cliente.post("/chat", json={"pergunta": pergunta, "sessao_id": sessao_id})
    assert resposta.status_code == 200
    return resposta.json()


def test_health_informa_documentos_e_modelos(cliente):
    dados = cliente.get("/health").json()
    assert dados["status"] == "ok"
    assert dados["n_documentos"] == 3
    assert dados["n_blocos"] > 0
    assert dados["llm"]["disponivel"] is True
    assert dados["ultima_indexacao"]
    assert {d["titulo"] for d in dados["documentos"]} >= {"Calendário Letivo de Teste"}


def test_chat_retorna_resposta_fontes_sessao_e_tempo(cliente):
    dados = perguntar(cliente)
    assert dados["resposta"]
    assert dados["modo"] == "llm"
    assert dados["fontes"][0]["secao"] == "Setembro"
    assert dados["sessao_id"]
    assert dados["id"]
    assert dados["tempo_ms"] >= 0


def test_chat_mantem_a_sessao_informada(cliente):
    primeira = perguntar(cliente)
    segunda = perguntar(cliente, "segunda chamada requerimento justificativa", primeira["sessao_id"])
    assert segunda["sessao_id"] == primeira["sessao_id"]


@pytest.mark.parametrize("corpo", [{"pergunta": ""}, {"pergunta": "   "}, {}, {"pergunta": "x" * 1001}])
def test_chat_rejeita_pergunta_invalida(cliente, corpo):
    assert cliente.post("/chat", json=corpo).status_code == 422


def test_feedback_registra_avaliacao(cliente):
    interacao = perguntar(cliente)
    resposta = cliente.post("/feedback", json={"interacao_id": interacao["id"], "util": True})
    assert resposta.status_code == 200
    assert resposta.json() == {"ok": True}


def test_feedback_de_interacao_inexistente_retorna_404(cliente):
    resposta = cliente.post("/feedback", json={"interacao_id": "nao-existe", "util": False})
    assert resposta.status_code == 404


@pytest.mark.parametrize("cabecalhos", [{}, {"X-Admin-Key": "errada"}])
def test_rotas_administrativas_exigem_chave(cliente, cabecalhos):
    assert cliente.post("/admin/reindex", headers=cabecalhos).status_code == 401
    assert cliente.get("/admin/logs", headers=cabecalhos).status_code == 401
    assert cliente.get("/admin/config", headers=cabecalhos).status_code == 401
    assert cliente.put("/admin/config", json={"top_k": 2}, headers=cabecalhos).status_code == 401


def test_rotas_administrativas_desativadas_sem_admin_key(ambiente):
    cliente = TestClient(criar_app(replace(ambiente, admin_key=""), embedder=EmbedderFalso(), llm=LLMFalso()))
    assert cliente.get("/admin/logs", headers=ADMIN).status_code == 503


def test_admin_config_altera_e_persiste_parametros(cliente, ambiente):
    resposta = cliente.put("/admin/config", json={"top_k": 2, "limiar": 0.3}, headers=ADMIN)
    assert resposta.status_code == 200
    assert resposta.json()["top_k"] == 2
    assert cliente.get("/admin/config", headers=ADMIN).json()["limiar"] == 0.3
    assert '"top_k": 2' in ambiente.parametros_arquivo.read_text(encoding="utf-8")


@pytest.mark.parametrize("corpo", [{"top_k": 0}, {"limiar": 1.5}, {"prompt_sistema": "curto"}, {}])
def test_admin_config_rejeita_valores_invalidos(cliente, corpo):
    assert cliente.put("/admin/config", json=corpo, headers=ADMIN).status_code == 422


def test_admin_logs_lista_interacoes_com_avaliacao(cliente):
    interacao = perguntar(cliente)
    cliente.post("/feedback", json={"interacao_id": interacao["id"], "util": True})
    dados = cliente.get("/admin/logs", headers=ADMIN).json()
    assert dados["total_interacoes"] == 1
    assert dados["avaliacoes"] == {"uteis": 1, "nao_uteis": 0}
    registro = dados["interacoes"][0]
    assert registro["pergunta"] == PERGUNTA_TRANCAMENTO
    assert registro["avaliacao"]["util"] is True
    assert registro["trechos"][0]["secao"] == "Setembro"


def test_admin_reindex_inclui_documento_novo(cliente, ambiente):
    (ambiente.docs_dir / "regulamento_estagio.md").write_text(
        "# Regulamento de Estágio\n\n## Carga horária\nO estágio supervisionado exige 300 horas.\n",
        encoding="utf-8",
    )
    resposta = cliente.post("/admin/reindex", headers=ADMIN)
    assert resposta.status_code == 200
    assert resposta.json()["n_documentos"] == 4

    dados = perguntar(cliente, "Quantas horas de estágio supervisionado são exigidas?")
    assert dados["fontes"][0]["documento"] == "regulamento estagio"


def test_interface_web_e_servida_na_raiz(cliente):
    resposta = cliente.get("/")
    assert resposta.status_code == 200
    assert "Chatbot Institucional UNIVERSO" in resposta.text
    assert cliente.get("/app.js").status_code == 200
