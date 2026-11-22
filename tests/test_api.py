"""Testes da API REST e da interface web (seção 3.7 e Quadro 3 do TCC)."""
import json
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
    resposta = cliente.post("/chat", json={"nome": "Maria", "pergunta": pergunta, "sessao_id": sessao_id})
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


def test_chat_mantem_estado_de_esclarecimento_na_sessao(cliente):
    primeira = perguntar(cliente, "Quando começam as minhas provas?")
    assert primeira["modo"] == "esclarecimento"
    assert primeira["fontes"] == []

    segunda = perguntar(cliente, "Presenciais.", primeira["sessao_id"])
    assert segunda["modo"] == "esclarecimento"
    assert "V1" in segunda["resposta"]

    terceira = perguntar(cliente, "V1.", primeira["sessao_id"])
    assert terceira["modo"] == "validado"
    assert terceira["fontes"][0]["secao"] == "Outubro"


@pytest.mark.parametrize("corpo", [{"nome": "Ana", "pergunta": ""}, {"nome": "Ana", "pergunta": "   "}, {}, {"nome": "Ana", "pergunta": "x" * 1001}, {"pergunta": "Oi"}, {"nome": "  ", "pergunta": "Oi"}, {"nome": "x" * 81, "pergunta": "Oi"}])
def test_chat_rejeita_pergunta_invalida(cliente, corpo):
    assert cliente.post("/chat", json=corpo).status_code == 422


def perguntar_stream(cliente, pergunta=PERGUNTA_TRANCAMENTO, sessao_id=None):
    resposta = cliente.post("/chat/stream", json={"nome": "Maria", "pergunta": pergunta, "sessao_id": sessao_id})
    assert resposta.status_code == 200
    assert resposta.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(linha) for linha in resposta.text.splitlines() if linha.strip()]


def test_chat_stream_envia_partes_e_depois_a_resposta_final(cliente):
    eventos = perguntar_stream(cliente)
    parciais = [e for e in eventos if e["tipo"] == "parcial"]
    assert len(parciais) > 1
    final = eventos[-1]
    assert final["tipo"] == "final"
    assert [e["tipo"] for e in eventos].count("final") == 1
    assert "".join(e["texto"] for e in parciais).strip() == final["resposta"]
    assert final["modo"] == "llm"
    assert final["fontes"][0]["secao"] == "Setembro"
    assert final["id"] and final["sessao_id"]
    # registrada como qualquer interação: aceita avaliação
    assert cliente.post("/feedback", json={"interacao_id": final["id"], "util": True}).status_code == 200


def test_chat_stream_sem_modelo_envia_so_a_resposta_final(cliente):
    eventos = perguntar_stream(cliente, "Quando começam as minhas provas?")
    assert [e["tipo"] for e in eventos] == ["final"]
    assert eventos[0]["modo"] == "esclarecimento"


def test_chat_stream_resposta_final_substitui_recusa_do_modelo(ambiente):
    cliente = TestClient(criar_app(ambiente, embedder=EmbedderFalso(), llm=LLMFalso(resposta="Não localizei.")))
    eventos = perguntar_stream(cliente)
    assert any(e["tipo"] == "parcial" for e in eventos)
    final = eventos[-1]
    assert final["modo"] == "sem_resultado"
    assert final["fontes"] == []
    assert final["resposta"] != "Não localizei."


def test_chat_stream_rejeita_pergunta_invalida(cliente):
    assert cliente.post("/chat/stream", json={"nome": "Ana", "pergunta": " "}).status_code == 422


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
    assert cliente.post("/admin/documentos", headers=cabecalhos).status_code == 401
    assert cliente.get("/admin/logs", headers=cabecalhos).status_code == 401
    assert cliente.get("/admin/config", headers=cabecalhos).status_code == 401
    assert cliente.put("/admin/config", json={"top_k": 2}, headers=cabecalhos).status_code == 401


def test_rotas_administrativas_desativadas_sem_admin_key(ambiente):
    cliente = TestClient(criar_app(replace(ambiente, admin_key="", admin_email="", admin_pass=""), embedder=EmbedderFalso(), llm=LLMFalso()))
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


def test_admin_upload_pdf_converte_com_docling_persiste_e_reindexa(cliente, ambiente, monkeypatch):
    markdown = "# Manual enviado\n\n## Matrícula\nO prazo de matrícula termina em 20 de agosto."
    estruturado = {"name": "manual", "texts": [{"text": "O prazo de matrícula termina em 20 de agosto."}]}
    chamadas = []

    def converter(caminho):
        chamadas.append(caminho)
        return markdown, estruturado

    monkeypatch.setattr("app.servico.converter_pdf_docling", converter)
    resposta = cliente.post(
        "/admin/documentos",
        headers=ADMIN,
        files={"arquivo": ("Manual Acadêmico.pdf", b"%PDF-1.7\nconteudo", "application/pdf")},
        data={
            "titulo": "Manual Acadêmico",
            "url": "https://universo.edu.br/manual.pdf",
            "categoria": "manual",
            "atualizado_em": "2026-11-07",
        },
    )

    assert resposta.status_code == 200
    dados = resposta.json()
    assert dados["extrator"] == "docling"
    assert dados["arquivo_markdown"] == "manual_academico.md"
    assert dados["arquivo_json"] == "manual_academico.docling.json"
    assert dados["n_documentos"] == 4
    assert chamadas
    assert (ambiente.docs_dir / "manual_academico.md").read_text(encoding="utf-8").startswith("# Manual")
    assert (ambiente.originais_dir / "manual_academico.pdf").read_bytes().startswith(b"%PDF-")
    assert json.loads((ambiente.docling_dir / "manual_academico.docling.json").read_text(encoding="utf-8")) == estruturado
    manifesto = json.loads(ambiente.manifesto.read_text(encoding="utf-8"))
    item = next(d for d in manifesto["documentos"] if d["arquivo"] == "manual_academico.md")
    assert item["extrator"] == "docling"


def test_admin_upload_rejeita_arquivo_que_nao_e_pdf(cliente):
    resposta = cliente.post(
        "/admin/documentos",
        headers=ADMIN,
        files={"arquivo": ("notas.txt", b"texto", "text/plain")},
    )
    assert resposta.status_code == 422


def test_admin_upload_remove_artefatos_se_reindexacao_falhar(
    cliente, ambiente, monkeypatch
):
    manifesto_anterior = ambiente.manifesto.read_bytes()
    monkeypatch.setattr(
        "app.servico.converter_pdf_docling",
        lambda caminho: ("# Documento\n\nConteúdo", {"name": "documento"}),
    )
    monkeypatch.setattr(
        "app.servico.construir_indice",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("falha simulada")),
    )

    try:
        cliente.post(
            "/admin/documentos",
            headers=ADMIN,
            files={"arquivo": ("Falha.pdf", b"%PDF-1.7\nconteudo", "application/pdf")},
        )
    except RuntimeError as erro:
        assert str(erro) == "falha simulada"
    else:
        raise AssertionError("A falha de reindexação deveria ser propagada.")

    assert ambiente.manifesto.read_bytes() == manifesto_anterior
    assert not (ambiente.docs_dir / "falha.md").exists()
    assert not (ambiente.originais_dir / "falha.pdf").exists()
    assert not (ambiente.docling_dir / "falha.docling.json").exists()


def test_interface_web_e_servida_na_raiz(cliente):
    resposta = cliente.get("/")
    assert resposta.status_code == 200
    assert "Chatbot Institucional UNIVERSO" in resposta.text
    assert cliente.get("/app.js").status_code == 200


# ---------------------------------------------------------------- painel /admin
def entrar(cliente):
    r = cliente.post("/admin/login", json={"email": "admin@teste.com", "senha": "senha-teste"})
    assert r.status_code == 200
    return r


def test_pagina_admin_e_servida(cliente):
    r = cliente.get("/admin")
    assert r.status_code == 200
    assert "Administração" in r.text


@pytest.mark.parametrize("caminho", ["/", "/app.js", "/style.css", "/admin.js", "/admin.css"])
def test_interface_web_exige_revalidacao_do_navegador(cliente, caminho):
    resposta = cliente.get(caminho)
    assert resposta.status_code == 200
    assert resposta.headers["cache-control"] == "no-cache"


def test_login_admin_rejeita_credenciais_invalidas(cliente):
    r = cliente.post("/admin/login", json={"email": "admin@teste.com", "senha": "errada"})
    assert r.status_code == 401
    assert cliente.get("/admin/logs").status_code == 401


def test_login_admin_bloqueia_apos_muitas_falhas(cliente):
    for _ in range(5):
        cliente.post("/admin/login", json={"email": "x@y.com", "senha": "errada"})
    r = cliente.post("/admin/login", json={"email": "admin@teste.com", "senha": "senha-teste"})
    assert r.status_code == 429


def test_login_admin_libera_rotas_e_logout_encerra(cliente):
    assert cliente.get("/admin/sessao").status_code == 401
    entrar(cliente)
    assert cliente.get("/admin/sessao").status_code == 200
    assert cliente.get("/admin/logs").status_code == 200
    cliente.post("/admin/logout")
    assert cliente.get("/admin/logs").status_code == 401


def test_login_admin_desativado_sem_email_e_senha(ambiente):
    cliente = TestClient(criar_app(replace(ambiente, admin_email="", admin_pass=""), embedder=EmbedderFalso(), llm=LLMFalso()))
    r = cliente.post("/admin/login", json={"email": "a@b.com", "senha": "x"})
    assert r.status_code == 503


def test_logs_registram_nome_do_aluno(cliente):
    perguntar(cliente)
    entrar(cliente)
    interacoes = cliente.get("/admin/logs").json()["interacoes"]
    assert interacoes[0]["nome"] == "Maria"


def test_admin_lista_e_remove_documento(cliente, ambiente):
    entrar(cliente)
    docs = cliente.get("/admin/documentos").json()["documentos"]
    assert {d["arquivo"] for d in docs} >= {"calendario_teste.md"}
    r = cliente.delete("/admin/documentos/calendario_teste.md")
    assert r.status_code == 200
    assert r.json()["n_documentos"] == 2
    assert not (ambiente.docs_dir / "calendario_teste.md").exists()
    assert cliente.get("/health").json()["n_documentos"] == 2
    assert cliente.delete("/admin/documentos/calendario_teste.md").status_code == 404


def test_admin_remover_documento_nao_sai_da_pasta(cliente, ambiente):
    entrar(cliente)
    (ambiente.docs_dir.parent / "segredo.md").write_text("x", encoding="utf-8")
    assert cliente.delete("/admin/documentos/..%2Fsegredo.md").status_code in (404, 405)
    assert (ambiente.docs_dir.parent / "segredo.md").exists()


def test_admin_remover_documento_exige_autenticacao(cliente):
    assert cliente.delete("/admin/documentos/calendario_teste.md").status_code == 401
    assert cliente.get("/admin/documentos").status_code == 401


def test_servico_recusa_nome_de_documento_com_caminho(servico, ambiente):
    (ambiente.docs_dir.parent / "segredo.md").write_text("x", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        servico.remover_documento("../segredo.md")
    assert (ambiente.docs_dir.parent / "segredo.md").exists()


def test_admin_relatorio_conteudo_download_e_teste_de_busca(cliente, ambiente, monkeypatch):
    markdown = "# Manual enviado\n\n## Matrícula\nO prazo de matrícula termina em 20 de agosto."
    estruturado = {"name": "manual", "pages": {"1": {}}, "texts": []}
    monkeypatch.setattr("app.servico.converter_pdf_docling", lambda caminho: (markdown, estruturado))
    envio = cliente.post(
        "/admin/documentos", headers=ADMIN,
        files={"arquivo": ("Manual Novo.pdf", b"%PDF-1.7\nconteudo", "application/pdf")},
    ).json()

    relatorio = envio["relatorio"]
    assert relatorio["paginas"] == 1 and relatorio["blocos"] >= 1 and relatorio["secoes"] >= 1
    assert any("digitalizado" in aviso for aviso in relatorio["avisos"])  # pouco texto por página
    manifesto = json.loads(ambiente.manifesto.read_text(encoding="utf-8"))
    assert next(d for d in manifesto["documentos"] if d["arquivo"] == "manual_novo.md")["ingestao"] == relatorio

    conteudo = cliente.get("/admin/documentos/manual_novo.md/conteudo", headers=ADMIN).json()
    assert set(conteudo["formatos"]) == {"markdown", "blocos", "json", "pdf"}
    assert "20 de agosto" in conteudo["blocos"][0]["texto"]

    linhas = cliente.get("/admin/documentos/manual_novo.md/baixar?formato=blocos", headers=ADMIN).text.splitlines()
    assert json.loads(linhas[0])["arquivo"] == "manual_novo.md"
    assert cliente.get("/admin/documentos/manual_novo.md/baixar?formato=pdf", headers=ADMIN).content.startswith(b"%PDF-")
    assert cliente.get("/admin/documentos/nao_existe.md/conteudo", headers=ADMIN).status_code == 404
    assert cliente.get("/admin/documentos/manual_novo.md/conteudo").status_code in (401, 403)

    busca = cliente.post("/admin/busca", headers=ADMIN, json={"pergunta": "prazo de matrícula agosto"}).json()
    assert busca["trechos"][0]["arquivo"] == "manual_novo.md"

    documento = next(d for d in cliente.get("/admin/documentos", headers=ADMIN).json()["documentos"]
                     if d["arquivo"] == "manual_novo.md")
    assert documento["blocos"] == relatorio["blocos"] and documento["avisos"] == len(relatorio["avisos"])

    cliente.delete("/admin/documentos/manual_novo.md", headers=ADMIN)
    assert not (ambiente.indice_dir / "blocos" / "manual_novo.jsonl").exists()


# ---------------------------------------------------------------- dashboard e validação humana
def test_dashboard_exige_autenticacao(cliente):
    assert cliente.get("/admin/dashboard").status_code == 401
    assert cliente.post("/admin/revisoes", json={"interacao_id": "x", "veredito": "correta"}).status_code == 401


def test_dashboard_resume_respostas_avaliacoes_e_modelo(cliente):
    interacao = perguntar(cliente)
    cliente.post("/feedback", json={"interacao_id": interacao["id"], "util": False})
    dados = cliente.get("/admin/dashboard?periodo=7d", headers=ADMIN).json()
    assert dados["totais"]["perguntas"] == 1
    assert dados["alunos"]["nao_uteis"] == 1
    assert dados["modelo"]["disponivel"] is True
    assert dados["para_revisar"][0]["id"] == interacao["id"]
    assert len(dados["por_dia"]) == 7 and len(dados["por_hora"]) == 24


def test_dashboard_rejeita_periodo_invalido(cliente):
    assert cliente.get("/admin/dashboard?periodo=ano", headers=ADMIN).status_code == 422


def test_validacao_humana_e_registrada_e_aparece_nos_logs_e_no_dashboard(cliente):
    interacao = perguntar(cliente)
    r = cliente.post("/admin/revisoes", headers=ADMIN, json={
        "interacao_id": interacao["id"], "veredito": "parcial", "revisor": "Secretaria", "comentario": "Faltou o prazo.",
    })
    assert r.status_code == 200 and r.json()["revisao"]["revisor"] == "Secretaria"
    log = cliente.get("/admin/logs", headers=ADMIN).json()["interacoes"][0]
    assert log["revisao"]["veredito"] == "parcial"
    validacao = cliente.get("/admin/dashboard", headers=ADMIN).json()["validacao"]
    assert validacao["parcial"] == 1 and validacao["pendentes"] == 0 and validacao["escore"] == 0.5


@pytest.mark.parametrize("corpo, status", [
    ({"interacao_id": "inexistente", "veredito": "correta"}, 404),
    ({"interacao_id": "x", "veredito": "talvez"}, 422),
])
def test_validacao_humana_rejeita_interacao_ou_veredito_invalido(cliente, corpo, status):
    assert cliente.post("/admin/revisoes", headers=ADMIN, json=corpo).status_code == status
