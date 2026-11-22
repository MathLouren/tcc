"""Cliente do LLM na API compatível com OpenAI: stream, modelos reserva, teto de tokens e métricas."""
import json
import time
from dataclasses import replace

import httpx
import pytest

from app import llm as modulo_llm
from app.llm import ClienteLLM, LLMIndisponivel
from app.rag import resumir_metricas

MENSAGENS = [{"role": "user", "content": "Qual o prazo de trancamento?"}]


def sse(*pedacos: str, provedor: str = "ProvedorA", comentarios: int = 0) -> bytes:
    linhas = [": OPENROUTER PROCESSING"] * comentarios
    for pedaco in pedacos:
        linhas.append("data: " + json.dumps({"provider": provedor, "choices": [{"delta": {"content": pedaco}}]}))
    linhas.append("data: " + json.dumps({"provider": provedor, "choices": [], "usage": {"completion_tokens": 7}}))
    linhas.append("data: [DONE]")
    return ("\n\n".join(linhas) + "\n\n").encode()


@pytest.fixture
def servidor(monkeypatch):
    """Simula /chat/completions: cada modelo tem uma resposta (função que recebe o corpo e devolve httpx.Response)."""
    respostas: dict = {}
    corpos: list[dict] = []

    def tratar(requisicao: httpx.Request) -> httpx.Response:
        corpo = json.loads(requisicao.content)
        corpos.append(corpo)
        return respostas[corpo["model"]](corpo)

    cliente = httpx.Client(transport=httpx.MockTransport(tratar))

    def stream(metodo, url, headers=None, json=None, timeout=None):  # noqa: A002 - mesma assinatura do httpx
        return cliente.stream(metodo, url, headers=headers, json=json)

    monkeypatch.setattr(modulo_llm.httpx, "stream", stream)
    return respostas, corpos


def cliente(**opcoes) -> ClienteLLM:
    padrao = dict(api="openai", base_url="http://llm.teste/v1", modelo="principal", api_key="k", timeout=5.0)
    return ClienteLLM(**{**padrao, **opcoes})


def test_stream_repassa_pedacos_e_registra_metricas(servidor):
    respostas, corpos = servidor
    respostas["principal"] = lambda corpo: httpx.Response(200, content=sse("O prazo ", "é 30/09.", comentarios=2))
    llm = cliente(max_tokens=321)
    recebidos = []

    llm.iniciar_metricas()
    texto = llm.gerar(MENSAGENS, 0.1, ao_token=recebidos.append)

    assert texto == "O prazo é 30/09."
    assert recebidos == ["O prazo ", "é 30/09."]
    assert corpos[0]["stream"] is True and corpos[0]["max_tokens"] == 321
    [chamada] = llm.consumir_metricas()
    assert chamada["modelo"] == "principal" and chamada["provedor"] == "ProvedorA"
    assert chamada["tokens"] == 7 and chamada["reserva"] is False
    assert chamada["primeiro_token_ms"] >= 0
    assert llm.consumir_metricas() == []  # consumir esvazia


def test_sem_ao_token_tambem_funciona(servidor):
    respostas, _ = servidor
    respostas["principal"] = lambda corpo: httpx.Response(200, content=sse("Resposta completa."))
    assert cliente().gerar(MENSAGENS, 0.1) == "Resposta completa."


def test_erro_do_principal_usa_o_modelo_reserva(servidor):
    respostas, corpos = servidor
    respostas["principal"] = lambda corpo: httpx.Response(503, json={"error": "sobrecarregado"})
    respostas["reserva"] = lambda corpo: httpx.Response(200, content=sse("Resposta da reserva.", provedor="B"))
    llm = cliente(modelos_reserva=("reserva",))

    llm.iniciar_metricas()
    assert llm.gerar(MENSAGENS, 0.1) == "Resposta da reserva."
    falha, ok = llm.consumir_metricas()
    assert falha["modelo"] == "principal" and falha["erro"]
    assert ok["modelo"] == "reserva" and ok["reserva"] is True
    assert [c["model"] for c in corpos] == ["principal", "reserva"]
    resumo = resumir_metricas([falha, ok], "llm")
    assert resumo["chamadas_llm"] == 2 and resumo["falhas_llm"] == 1 and resumo["reserva"] is True
    assert resumo["modelo"] == "reserva" and resumo["provedor"] == "B"


def test_principal_sem_primeiro_token_no_prazo_troca_de_modelo(servidor):
    respostas, _ = servidor

    def fila_do_provedor(corpo):
        def corpo_lento():
            for _ in range(5):  # só comentários de keep-alive, nenhum texto
                time.sleep(0.05)
                yield b": OPENROUTER PROCESSING\n\n"
            yield sse("tarde demais")
        return httpx.Response(200, content=corpo_lento())

    respostas["principal"] = fila_do_provedor
    respostas["reserva"] = lambda corpo: httpx.Response(200, content=sse("Rápida."))
    llm = cliente(modelos_reserva=("reserva",), timeout_primeiro_token=0.1)

    inicio = time.perf_counter()
    assert llm.gerar(MENSAGENS, 0.1) == "Rápida."
    assert time.perf_counter() - inicio < 1.0


def test_nao_troca_de_modelo_depois_de_mostrar_texto(servidor):
    """O aluno já viu parte da resposta: misturar com o texto de outro modelo seria pior que falhar."""
    respostas, corpos = servidor
    quebrada = (
        "data: " + json.dumps({"choices": [{"delta": {"content": "Começo "}}]}) + "\n\n"
        + "data: " + json.dumps({"error": {"message": "provedor caiu"}}) + "\n\n"
    ).encode()
    respostas["principal"] = lambda corpo: httpx.Response(200, content=quebrada)
    respostas["reserva"] = lambda corpo: httpx.Response(200, content=sse("outra resposta"))
    llm = cliente(modelos_reserva=("reserva",))

    with pytest.raises(LLMIndisponivel):
        llm.gerar(MENSAGENS, 0.1, ao_token=lambda _: None)
    assert [c["model"] for c in corpos] == ["principal"]


def test_todos_os_modelos_falham(servidor):
    respostas, _ = servidor
    respostas["principal"] = lambda corpo: httpx.Response(500)
    respostas["reserva"] = lambda corpo: httpx.Response(429)
    with pytest.raises(LLMIndisponivel, match=r"(?s)principal.*reserva"):
        cliente(modelos_reserva=("reserva",)).gerar(MENSAGENS, 0.1)


def test_resposta_vazia_e_indisponivel(servidor):
    respostas, _ = servidor
    respostas["principal"] = lambda corpo: httpx.Response(200, content=sse())
    with pytest.raises(LLMIndisponivel, match="vazia"):
        cliente().gerar(MENSAGENS, 0.1)


def test_config_le_reservas_do_ambiente(monkeypatch):
    from app.config import carregar_config

    monkeypatch.setenv("LLM_MODELOS_RESERVA", " a/b , c/d ,")
    monkeypatch.setenv("LLM_TIMEOUT_PRIMEIRO_TOKEN", "12")
    monkeypatch.setenv("LLM_MAX_TOKENS", "500")
    config = carregar_config()
    assert config.llm_modelos_reserva == ("a/b", "c/d")
    assert config.llm_timeout_primeiro_token == 12.0 and config.llm_max_tokens == 500
    criado = modulo_llm.criar_cliente_llm(replace(config, llm_modelo="a/b"))
    assert criado.modelos_reserva == ("c/d",)  # o principal não se repete como reserva


# ------------------------------------------------------------ métricas no registro e no painel
def test_resposta_registra_metricas_e_cache(servico):
    primeira = servico.motor.responder("Qual o prazo para trancamento de matrícula?")
    assert primeira.modo == "llm"
    servico.registro.registrar_interacao("s1", "Qual o prazo para trancamento de matrícula?", primeira)
    repetida = servico.motor.responder("Qual o prazo para trancamento de matrícula?")
    assert repetida.metricas == {"chamadas_llm": 0, "cache": True}

    [registro] = servico.registro._ler(servico.registro.arq_interacoes)
    assert "metricas" not in registro or registro["metricas"].get("chamadas_llm", 0) >= 0


def test_painel_mostra_primeiro_token_e_reserva():
    from app.dashboard import montar_dashboard
    from app.registros import agora

    quando = agora().isoformat(timespec="seconds")
    interacoes = [
        {"id": "a", "data_hora": quando, "modo": "llm", "pergunta": "p1", "tempo_ms": 3000,
         "metricas": {"chamadas_llm": 1, "primeiro_token_ms": 800, "reserva": False}},
        {"id": "b", "data_hora": quando, "modo": "llm", "pergunta": "p2", "tempo_ms": 5000,
         "metricas": {"chamadas_llm": 2, "primeiro_token_ms": 1200, "reserva": True}},
        {"id": "c", "data_hora": quando, "modo": "llm", "pergunta": "p3", "tempo_ms": 5,
         "metricas": {"chamadas_llm": 0, "cache": True}},
    ]
    tempo = montar_dashboard(interacoes, {}, {}, "hoje")["tempo"]
    assert tempo["primeiro_token_mediano_ms"] == 1000
    assert tempo["respostas_pela_reserva"] == 1 and tempo["respostas_do_cache"] == 1


# ------------------------------------------------------------ pré-aquecimento do cache
def _registrar(servico, pergunta, modo="llm"):
    from app.rag import Resposta

    servico.registro.registrar_interacao("s", pergunta, Resposta("r", [], modo, [], 10))


def test_perguntas_frequentes_ordem_e_filtros(servico):
    for pergunta, modo in [
        ("Qual o prazo de trancamento?", "llm"),
        ("qual o prazo de trancamento", "llm"),        # mesma pergunta, outra grafia
        ("Onde fica o campus?", "llm"),
        ("presencial", "esclarecimento"),              # não é resposta do modelo
        ("Qual a senha do wifi?", "sem_resultado"),
        ("oi", "llm"),                                 # curta demais
        ("Quando começam as aulas?", "citacao"),
    ]:
        _registrar(servico, pergunta, modo)
    frequentes = servico.perguntas_frequentes(10)
    assert frequentes[0] == "qual o prazo de trancamento"  # a mais repetida, na grafia mais recente
    assert set(frequentes[1:]) == {"Onde fica o campus?", "Quando começam as aulas?"}
    assert frequentes[1] == "Quando começam as aulas?"   # empate: a mais recente primeiro
    assert servico.perguntas_frequentes(1) == ["qual o prazo de trancamento"]


def test_aquecer_cache_deixa_a_resposta_pronta(ambiente, llm):
    from app.servico import Servico
    from tests.conftest import EmbedderFalso

    servico = Servico(replace(ambiente, cache_aquecer=5), embedder=EmbedderFalso(), llm=llm)
    _registrar(servico, "Qual o prazo para trancamento de matrícula?")
    assert servico.aquecer_cache() == 1
    chamadas = len(llm.chamadas)

    resposta = servico.motor.responder("Qual o prazo para trancamento de matrícula?", "sessao-aluno")
    assert len(llm.chamadas) == chamadas  # veio do cache
    assert resposta.modo == "llm" and resposta.metricas.get("cache") is True
    assert len(servico.registro._ler(servico.registro.arq_interacoes)) == 1  # o aquecimento não registra


def test_aquecer_cache_para_quando_o_modelo_cai(ambiente):
    from app.servico import Servico
    from tests.conftest import EmbedderFalso, LLMFalso

    servico = Servico(replace(ambiente, cache_aquecer=5), embedder=EmbedderFalso(), llm=LLMFalso(falhar=True))
    _registrar(servico, "Qual o prazo para trancamento de matrícula?")
    _registrar(servico, "Onde fica o campus de São Gonçalo?")
    assert servico.aquecer_cache() == 0


def test_aquecimento_desligado_nao_cria_thread(servico, monkeypatch):
    criadas = []
    monkeypatch.setattr("app.servico.threading.Thread", lambda *a, **k: criadas.append(k))
    servico.aquecer_cache_em_segundo_plano()
    assert criadas == []
