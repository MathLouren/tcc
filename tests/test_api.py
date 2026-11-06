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
    assert dados["vector_store"] == "local"
    assert dados["busca"] in {"faiss", "numpy"}
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
    assert cliente.post("/admin/documentos/campus_teste.html/reindexar", headers=cabecalhos).status_code == 401
    assert cliente.get("/admin/logs", headers=cabecalhos).status_code == 401
    assert cliente.get("/admin/config", headers=cabecalhos).status_code == 401
    assert cliente.put("/admin/config", json={"top_k": 2}, headers=cabecalhos).status_code == 401
    assert cliente.put("/admin/vector-store", json={"vector_store": "local"}, headers=cabecalhos).status_code == 401
    assert cliente.delete("/admin/documentos/campus_teste.html", headers=cabecalhos).status_code == 401


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


def test_admin_vector_store_local_persiste_e_reindexa(cliente, ambiente):
    resposta = cliente.put("/admin/vector-store", json={"vector_store": "local"}, headers=ADMIN)
    assert resposta.status_code == 200
    dados = resposta.json()
    assert dados["ok"] is True
    assert dados["vector_store"] == "local"
    assert dados["busca"] in {"faiss", "numpy"}
    assert dados["n_documentos"] == 3
    assert '"vector_store": "local"' in ambiente.parametros_arquivo.read_text(encoding="utf-8")


def test_admin_vector_store_rejeita_valor_invalido(cliente):
    resposta = cliente.put("/admin/vector-store", json={"vector_store": "banana"}, headers=ADMIN)
    assert resposta.status_code == 422


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


def test_pagina_admin_e_servida(cliente):
    resposta = cliente.get("/admin")
    assert resposta.status_code == 200
    assert "Adicionar arquivo" in resposta.text
    assert cliente.get("/admin.js").status_code == 200


def test_admin_upload_salva_manifesto_e_reindexa(cliente, ambiente):
    resposta = cliente.post(
        "/admin/upload",
        headers=ADMIN,
        data={
            "titulo": "Manual do Aluno",
            "origem": "Coordenação do curso",
            "categoria": "manual",
            "atualizado_em": "2026-11-06",
        },
        files={"arquivo": ("manual_aluno.md", b"# Manual do Aluno\n\n## Matricula\nA matricula deve ser renovada no portal.")},
    )
    assert resposta.status_code == 200
    dados = resposta.json()
    assert dados["ok"] is True
    assert dados["titulo"] == "Manual do Aluno"
    assert dados["n_documentos"] == 4
    assert (ambiente.docs_dir / dados["arquivo"]).exists()
    manifesto = ambiente.manifesto.read_text(encoding="utf-8")
    assert '"categoria": "manual"' in manifesto
    assert cliente.get("/health").json()["n_documentos"] == 4


def test_admin_upload_rejeita_formato_invalido(cliente, ambiente):
    resposta = cliente.post(
        "/admin/upload",
        headers=ADMIN,
        files={"arquivo": ("imagem.png", b"nao e documento")},
    )
    assert resposta.status_code == 415
    assert not list(ambiente.docs_dir.glob("*.png"))


def test_admin_delete_remove_documento_manifesto_e_reindexa(cliente, ambiente):
    resposta = cliente.delete("/admin/documentos/campus_teste.html", headers=ADMIN)
    assert resposta.status_code == 200
    assert resposta.json()["n_documentos"] == 2
    assert not (ambiente.docs_dir / "campus_teste.html").exists()
    manifesto = ambiente.manifesto.read_text(encoding="utf-8")
    assert "campus_teste.html" not in manifesto
    assert cliente.get("/health").json()["n_documentos"] == 2


def test_admin_delete_documento_inexistente_retorna_404(cliente):
    resposta = cliente.delete("/admin/documentos/inexistente.md", headers=ADMIN)
    assert resposta.status_code == 404


def test_admin_reindexa_documento_especifico(cliente):
    resposta = cliente.post("/admin/documentos/campus_teste.html/reindexar", headers=ADMIN)
    assert resposta.status_code == 200
    assert resposta.json()["arquivo"] == "campus_teste.html"
    assert resposta.json()["n_documentos"] == 3
