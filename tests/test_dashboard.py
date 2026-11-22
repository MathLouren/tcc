"""Indicadores do painel administrativo: desfechos, avaliação dos alunos, validação e séries no tempo."""
from datetime import datetime, timedelta, timezone

import pytest

from app.dashboard import montar_dashboard
from app.registros import Registro, ler_data_hora

BRT = timezone(timedelta(hours=-3))
REFERENCIA = datetime(2026, 11, 22, 15, 0, tzinfo=BRT)


def interacao(id_, quando, modo, pergunta="pergunta", nome="Ana", tempo_ms=1000):
    return {"id": id_, "data_hora": quando.isoformat(timespec="seconds"), "modo": modo, "pergunta": pergunta,
            "nome": nome, "sessao_id": f"s-{nome}", "tempo_ms": tempo_ms}


@pytest.fixture
def registros():
    ontem = REFERENCIA - timedelta(days=1)
    return [
        interacao("a", ontem.replace(hour=9), "llm", tempo_ms=6000),
        interacao("b", ontem.replace(hour=9, minute=30), "validado", tempo_ms=5),
        interacao("c", REFERENCIA.replace(hour=10), "sem_resultado", "qual a senha do wifi?", nome="Bia"),
        interacao("d", REFERENCIA.replace(hour=11), "sem_resultado", "Qual a senha do Wi-Fi?", nome="Bia"),
        interacao("e", REFERENCIA.replace(hour=14), "extrativo", nome="Caio"),
        interacao("f", REFERENCIA.replace(hour=14, minute=5), "conversa", "oi", nome="Caio"),
        interacao("g", REFERENCIA - timedelta(days=60), "llm"),  # fora dos últimos 30 dias
    ]


def test_desfechos_separam_respostas_falta_de_documento_e_modelo_fora_do_ar(registros):
    d = montar_dashboard(registros, {}, {}, "30d", REFERENCIA)
    assert d["totais"]["interacoes"] == 6
    assert d["totais"]["perguntas"] == 5  # a saudação não é pergunta acadêmica
    assert d["desfechos"]["respondidas"] == 2
    assert d["desfechos"]["sem_resultado"] == 2
    assert d["desfechos"]["modelo_indisponivel"] == 1
    assert d["desfechos"]["taxa_resposta"] == 0.4
    assert d["ultima_falha_modelo"].startswith("2026-11-22T14:00")
    assert d["totais"]["alunos"] == 3


def test_periodo_hoje_considera_so_o_dia_corrente(registros):
    d = montar_dashboard(registros, {}, {}, "hoje", REFERENCIA)
    assert d["totais"]["perguntas"] == 3
    assert [dia["data"] for dia in d["por_dia"]] == ["2026-11-22"]


def test_series_por_dia_e_por_hora_preenchem_dias_sem_uso(registros):
    d = montar_dashboard(registros, {}, {}, "7d", REFERENCIA)
    assert len(d["por_dia"]) == 7 and d["por_dia"][0]["data"] == "2026-11-16"
    ontem = next(dia for dia in d["por_dia"] if dia["data"] == "2026-11-21")
    assert ontem["respondidas"] == 2
    assert d["por_hora"][9] == 2 and d["por_hora"][14] == 1  # a saudação das 14h05 não conta


def test_avaliacao_dos_alunos_e_validacao_da_equipe(registros):
    avaliacoes = {"a": {"util": True}, "c": {"util": False}}
    revisoes = {"a": {"veredito": "correta"}, "b": {"veredito": "parcial"}}
    d = montar_dashboard(registros, avaliacoes, revisoes, "30d", REFERENCIA)
    assert d["alunos"] == {"uteis": 1, "nao_uteis": 1, "sem_avaliacao": 4, "taxa_util": 0.5}
    v = d["validacao"]
    assert (v["correta"], v["parcial"], v["incorreta"], v["pendentes"]) == (1, 1, 0, 3)
    assert v["escore"] == 0.75


def test_fila_de_validacao_prioriza_resposta_reprovada_pelo_aluno(registros):
    d = montar_dashboard(registros, {"c": {"util": False}}, {}, "30d", REFERENCIA)
    fila = d["para_revisar"]
    assert fila[0]["id"] == "c" and "não útil" in fila[0]["motivo"]
    assert fila[1]["id"] == "e" and "Modelo indisponível" in fila[1]["motivo"]
    assert "f" not in {item["id"] for item in fila}


def test_perguntas_sem_resposta_iguais_sao_agrupadas(registros):
    d = montar_dashboard(registros, {}, {}, "30d", REFERENCIA)
    assert d["sem_resposta_frequentes"][0]["vezes"] == 2


def test_registro_grava_data_hora_com_fuso_e_le_registros_antigos_sem_fuso(tmp_path):
    registro = Registro(tmp_path)

    class Resposta:
        resposta, modo, tempo_ms, trechos = "texto", "llm", 10, []

    interacao_id = registro.registrar_interacao("s", "pergunta", Resposta(), nome="Ana")
    gravada = registro.dados()[0][0]["data_hora"]
    assert gravada[-6] in "+-" and gravada.endswith(":00")  # ex.: 2026-11-22T15:00:00-03:00
    assert ler_data_hora("2026-11-22T10:00:00").utcoffset() is not None
    assert registro.registrar_revisao(interacao_id, "correta", "Secretaria")["revisor"] == "Secretaria"
    assert registro.registrar_revisao(interacao_id, "talvez", "Secretaria") is None
    assert registro.registrar_revisao("inexistente", "correta", "Secretaria") is None
