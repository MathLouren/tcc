"""Testes do fluxo RAG: prompt, histórico, fontes, recusa e modo extrativo (seção 3.6 do TCC)."""
from app.rag import MENSAGEM_NAO_ENCONTRADA, montar_fontes
from app.servico import Servico

from conftest import PERGUNTA_FORA, PERGUNTA_TRANCAMENTO, EmbedderFalso, LLMFalso


def test_sem_trechos_relevantes_responde_mensagem_padrao_sem_chamar_o_llm(servico, llm):
    resposta = servico.motor.responder(PERGUNTA_FORA)
    assert resposta.modo == "sem_resultado"
    assert resposta.resposta == MENSAGEM_NAO_ENCONTRADA
    assert resposta.fontes == []
    assert llm.chamadas == []


def test_prompt_contem_instrucoes_trechos_e_pergunta(servico, llm):
    resposta = servico.motor.responder(PERGUNTA_TRANCAMENTO)
    assert resposta.modo == "llm"
    assert resposta.resposta == llm.resposta

    chamada = llm.chamadas[0]
    mensagens = chamada["mensagens"]
    assert chamada["temperatura"] == 0.1
    assert mensagens[0]["role"] == "system"
    assert servico.parametros.prompt_sistema in mensagens[0]["content"]
    assert "Fonte: Calendário Letivo de Teste – Setembro" in mensagens[0]["content"]
    assert "trancamento de matrícula" in mensagens[0]["content"]
    assert mensagens[-1] == {"role": "user", "content": PERGUNTA_TRANCAMENTO}


def test_resposta_indica_as_fontes_com_metadados(servico):
    resposta = servico.motor.responder(PERGUNTA_TRANCAMENTO)
    fonte = resposta.fontes[0]
    assert fonte["documento"] == "Calendário Letivo de Teste"
    assert fonte["secao"] == "Setembro"
    assert fonte["url"] == "https://universo.edu.br/calendarios/"
    assert fonte["atualizado_em"] == "2026-07-22"
    assert 0 < fonte["similaridade"] <= 1


def test_historico_limitado_as_ultimas_duas_trocas(servico, llm):
    perguntas = [
        PERGUNTA_TRANCAMENTO,
        "segunda chamada requerimento justificativa",
        "aplicação da V1 disciplinas presenciais outubro",
        "Rua Lambari endereço da unidade",
    ]
    for pergunta in perguntas:
        servico.motor.responder(pergunta, sessao_id="sessao-1")

    mensagens = llm.chamadas[-1]["mensagens"]
    # sistema + 2 trocas (pergunta e resposta) + pergunta atual
    assert len(mensagens) == 6
    conteudos = [m["content"] for m in mensagens[1:]]
    assert perguntas[0] not in conteudos
    assert conteudos[0] == perguntas[1]
    assert conteudos[2] == perguntas[2]


def test_sessoes_diferentes_nao_compartilham_historico(servico, llm):
    servico.motor.responder(PERGUNTA_TRANCAMENTO, sessao_id="a")
    servico.motor.responder("segunda chamada requerimento justificativa", sessao_id="b")
    assert len(llm.chamadas[-1]["mensagens"]) == 2  # sistema + pergunta atual


def test_pergunta_de_acompanhamento_reutiliza_o_contexto_da_conversa(servico, llm):
    servico.motor.responder(PERGUNTA_TRANCAMENTO, sessao_id="sessao-2")
    resposta = servico.motor.responder(PERGUNTA_FORA, sessao_id="sessao-2")
    assert resposta.modo == "llm"
    assert resposta.fontes[0]["secao"] == "Setembro"
    assert llm.chamadas[-1]["mensagens"][1]["content"] == PERGUNTA_TRANCAMENTO


def test_modo_extrativo_quando_o_llm_esta_indisponivel(ambiente):
    servico = Servico(ambiente, embedder=EmbedderFalso(), llm=LLMFalso(falhar=True))
    resposta = servico.motor.responder(PERGUNTA_TRANCAMENTO)
    assert resposta.modo == "extrativo"
    assert "trancamento de matrícula" in resposta.resposta
    assert "modo extrativo" in resposta.resposta
    assert resposta.fontes


def test_fontes_sem_repeticao_e_ordenadas_por_similaridade():
    trechos = [
        {"documento": "Calendário", "secao": "Setembro", "similaridade": 0.70},
        {"documento": "Calendário", "secao": "Setembro", "similaridade": 0.82},
        {"documento": "Critérios", "secao": "Art. 80", "similaridade": 0.75},
    ]
    fontes = montar_fontes(trechos)
    assert [(f["documento"], f["similaridade"]) for f in fontes] == [("Calendário", 0.82), ("Critérios", 0.75)]
