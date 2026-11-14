"""Saudações e o pedido de esclarecimento feito pelo modelo, validado pelo código."""
import pytest

from app.rag import MotorRAG, _campo_do_pedido, _pedido_formal, _resposta_social
from app.config import Parametros

from conftest import EmbedderFalso, LLMFalso

PRESENCIAL = {"secao": "17. AVALIAÇÃO", "texto": "Disciplinas presenciais: média igual ou superior a 7,0 aprova."}
EAD = {"secao": "17. AVALIAÇÃO", "texto": "Disciplinas na modalidade EaD: Média Parcial igual ou superior a 6,0 aprova."}
POS = {"secao": "8.2. PÓS-GRADUAÇÃO", "texto": "Na pós-graduação lato sensu a média mínima é 7,0."}
GRAD = {"secao": "8.1. GRADUAÇÃO", "texto": "Na graduação a média é calculada pelas avaliações."}


class LLMRoteiro(LLMFalso):
    def __init__(self, respostas):
        super().__init__()
        self.respostas = list(respostas)

    def gerar(self, mensagens, temperatura):
        self.chamadas.append({"mensagens": mensagens, "temperatura": temperatura})
        return self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]


@pytest.mark.parametrize("mensagem,trecho", [
    ("oi", "Olá!"), ("Olá, tudo bem?", "Olá!"), ("bom dia!", "Olá!"), ("Oi, boa noite", "Olá!"),
    ("obrigado!", "Por nada"), ("Muito obrigada pela ajuda", "Por nada"), ("valeu", "Por nada"),
    ("tchau", "Até mais"),
])
def test_saudacoes_recebem_resposta_direta(mensagem, trecho):
    assert trecho in _resposta_social(mensagem)


@pytest.mark.parametrize("mensagem", [
    "oi, qual o prazo da AT?", "obrigado, e a VS?", "bom dia, como tranco a matrícula?", "tudo bem reprovar?",
])
def test_saudacao_com_pergunta_segue_para_o_rag(mensagem):
    assert _resposta_social(mensagem) is None


def test_saudacao_nao_chama_busca_nem_modelo(servico, llm):
    resposta = servico.motor.responder("oi, tudo bem?", "s")
    assert resposta.modo == "conversa"
    assert resposta.fontes == []
    assert llm.chamadas == []


def test_pedido_formal_e_reconhecido():
    assert _pedido_formal("ESCLARECIMENTO: Presencial ou EaD?") == "Presencial ou EaD?"
    assert _pedido_formal("**ESCLARECIMENTO:** Sua disciplina é presencial ou EaD?") == "Sua disciplina é presencial ou EaD?"
    assert _pedido_formal("A média é 7,0.") is None


@pytest.mark.parametrize("pergunta,pedido,trechos,campo", [
    ("qual a media pra passar direto?", "Presencial ou EaD?", [PRESENCIAL, EAD], "modalidade"),
    ("qual a media pra passar direto no ead?", "Presencial ou EaD?", [PRESENCIAL, EAD], None),
    ("qual a media da AP?", "Presencial ou EaD?", [PRESENCIAL, EAD], None),
    ("qual a media pra passar direto?", "Presencial ou EaD?", [PRESENCIAL], None),
    ("qual a media minima?", "É graduação ou pós-graduação?", [POS, GRAD], "nivel"),
    ("qual a media minima na graduação?", "É graduação ou pós-graduação?", [POS, GRAD], None),
    ("qual a media minima?", "É graduação ou pós-graduação?", [GRAD], None),
    ("qual a media pra passar direto?", "Qual é o seu curso?", [PRESENCIAL, EAD], None),
    ("qual a media pra passar direto?", "Em qual semestre você está?", [PRESENCIAL, EAD], None),
])
def test_codigo_so_aceita_pedido_necessario(pergunta, pedido, trechos, campo):
    assert _campo_do_pedido(pergunta, pedido, trechos) == campo


class _IndiceVazio:
    blocos: list = []


def _motor(llm, trechos):
    motor = MotorRAG(_IndiceVazio(), EmbedderFalso(), llm, Parametros())
    motor.recuperar = lambda consulta: [
        dict(t, id=n, documento="Manual", arquivo="manual.md", similaridade=0.9) for n, t in enumerate(trechos)
    ]
    motor._resposta_factual_da_matriz = lambda pergunta: None
    return motor


def test_pedido_valido_vira_esclarecimento_e_a_resposta_do_aluno_refaz_a_busca():
    llm = LLMRoteiro(["ESCLARECIMENTO: Suas disciplinas são presenciais ou EaD?", "Na modalidade presencial, a média é 7,0."])
    motor = _motor(llm, [PRESENCIAL, EAD])
    consultas = []
    recuperar = motor.recuperar
    motor.recuperar = lambda consulta: consultas.append(consulta) or recuperar(consulta)

    primeira = motor.responder("qual a media pra passar direto?", "s1")
    assert primeira.modo == "esclarecimento"
    assert "presenciais ou à modalidade EaD" in primeira.resposta

    segunda = motor.responder("presencial", "s1")
    assert segunda.modo == "llm"
    assert segunda.resposta == "Na modalidade presencial, a média é 7,0."
    assert "qual a media pra passar direto?" in consultas[-1]
    assert "modalidade presencial" in llm.chamadas[-1]["mensagens"][-1]["content"]


def test_depois_do_esclarecimento_o_modelo_nao_pergunta_de_novo():
    llm = LLMRoteiro(["ESCLARECIMENTO: Presencial ou EaD?", "ESCLARECIMENTO: Presencial ou EaD?", "A média é 7,0."])
    motor = _motor(llm, [PRESENCIAL, EAD])
    motor.responder("qual a media pra passar direto?", "s1")
    resposta = motor.responder("presencial", "s1")
    assert resposta.modo == "llm"
    assert resposta.resposta == "A média é 7,0."


def test_pedido_desnecessario_e_descartado_e_o_modelo_responde():
    llm = LLMRoteiro(["ESCLARECIMENTO: Presencial ou EaD?", "No EaD, a Média Parcial de 6,0 aprova."])
    motor = _motor(llm, [PRESENCIAL, EAD])
    resposta = motor.responder("qual a media pra passar direto no ead?", "s1")
    assert resposta.modo == "llm"
    assert resposta.resposta == "No EaD, a Média Parcial de 6,0 aprova."
    assert len(llm.chamadas) == 2


def test_sem_sessao_o_pedido_nao_pode_ser_continuado_e_e_descartado():
    llm = LLMRoteiro(["ESCLARECIMENTO: Presencial ou EaD?", "Presencial: 7,0. EaD: 6,0."])
    resposta = _motor(llm, [PRESENCIAL, EAD]).responder("qual a media pra passar direto?")
    assert resposta.resposta == "Presencial: 7,0. EaD: 6,0."


def test_resposta_de_nivel_e_interpretada():
    from app.rag import _interpretar_esclarecimento

    assert _interpretar_esclarecimento("nivel", "graduação") == "graduação"
    assert _interpretar_esclarecimento("nivel", "pós") == "pós-graduação"
    assert _interpretar_esclarecimento("nivel", "especialização") == "pós-graduação"
    assert _interpretar_esclarecimento("nivel", "não sei") is None


PRES_ESCOPO = {"secao": "17. AVALIAÇÃO", "contexto": "§ 1º Nos cursos de Graduação Presenciais serão observados os critérios",
               "texto": "Se a Média Semestral for igual ou superior a 7,0, o aluno será aprovado."}
EAD_ESCOPO = {"secao": "17. AVALIAÇÃO", "contexto": "§ 2º Nos cursos de Graduação na modalidade de Educação a Distância",
              "texto": "Se a Média Parcial for igual ou superior a 6,0, o aluno será aprovado."}
GERAL = {"secao": "26. ATIVIDADES", "texto": "As atividades complementares devem ser comprovadas."}


@pytest.mark.parametrize("pergunta,trechos,esperado", [
    ("qual a media pra passar direto?", [PRES_ESCOPO, EAD_ESCOPO], True),
    ("qual a media pra passar direto no ead?", [PRES_ESCOPO, EAD_ESCOPO], False),
    ("qual a nota da AP?", [PRES_ESCOPO, EAD_ESCOPO], False),
    ("quanto preciso tirar na vs pra passar?", [PRES_ESCOPO, EAD_ESCOPO], False),
    ("qual a nota minima da V2?", [PRES_ESCOPO, EAD_ESCOPO], False),
    ("e se eu ficar com 5 de media?", [PRES_ESCOPO, EAD_ESCOPO], True),
    ("com que media eu reprovo direto?", [PRES_ESCOPO, EAD_ESCOPO], True),
    ("posso trancar se ja estiver reprovado por falta?", [PRES_ESCOPO, EAD_ESCOPO], False),
    ("qual a media pra passar direto?", [PRES_ESCOPO, GERAL], False),
    ("como entrego as atividades complementares?", [PRES_ESCOPO, EAD_ESCOPO], False),
])
def test_ambiguidade_de_modalidade_decidida_pelo_codigo(pergunta, trechos, esperado):
    from app.rag import _modalidade_ambigua

    assert _modalidade_ambigua(pergunta, trechos) is esperado


def test_codigo_pergunta_a_modalidade_e_a_resposta_usa_a_pergunta_original():
    llm = LLMRoteiro(["Na modalidade presencial, a média é 7,0."])
    motor = _motor(llm, [PRES_ESCOPO, EAD_ESCOPO])
    consultas = []
    recuperar = motor.recuperar
    motor.recuperar = lambda consulta: consultas.append(consulta) or recuperar(consulta)

    primeira = motor.responder("qual a media pra passar direto?", "s1")
    assert primeira.modo == "esclarecimento"
    assert llm.chamadas == []

    segunda = motor.responder("presencial", "s1")
    assert segunda.resposta == "Na modalidade presencial, a média é 7,0."
    assert consultas[-1].startswith("qual a media pra passar direto?")
    assert "presenciais" in consultas[-1]


def test_sem_sessao_nao_ha_pergunta_de_volta():
    llm = LLMRoteiro(["Presencial: 7,0. EaD: 6,0."])
    resposta = _motor(llm, [PRES_ESCOPO, EAD_ESCOPO]).responder("qual a media pra passar direto?")
    assert resposta.modo == "llm"


def test_reordenacao_prefere_o_escopo_da_modalidade_perguntada():
    from app.rag import _ajuste_de_modalidade

    assert _ajuste_de_modalidade(EAD_ESCOPO, "ead") > 0
    assert _ajuste_de_modalidade(PRES_ESCOPO, "ead") < 0
    assert _ajuste_de_modalidade(GERAL, "ead") == 0


def test_busca_com_so_uma_modalidade_ainda_pergunta_se_o_indice_tem_a_outra():
    from app.rag import _modalidade_ambigua

    pres = dict(PRES_ESCOPO, arquivo="manual.md")
    ead = dict(EAD_ESCOPO, arquivo="manual.md")
    assert _modalidade_ambigua("qual a media pra passar direto?", [pres], [pres, ead]) is True
    assert _modalidade_ambigua("qual a media pra passar direto?", [pres], [pres]) is False
    assert _modalidade_ambigua("qual a media pra passar direto no ead?", [pres], [pres, ead]) is False


@pytest.mark.parametrize("modelo,pergunta,esperado", [
    ("Não localizei essa informação nos documentos oficiales.", "Qual o prazo para entregar as ats?", "Calendário Letivo"),
    ("Não localizei essa informação.", "quando começam as aulas?", "Calendário Letivo"),
    ("Não localizei essa informação nos documentos oficiais.", "qual o valor da mensalidade?", "0800 721 0251"),
])
def test_recusa_curta_vira_texto_padrao_com_orientacao(modelo, pergunta, esperado):
    llm = LLMRoteiro([modelo])
    resposta = _motor(llm, [GERAL]).responder(pergunta, "s1")
    assert resposta.modo == "sem_resultado"
    assert "oficiales" not in resposta.resposta
    assert esperado in resposta.resposta
    assert resposta.fontes == []


def test_recusa_sem_data_nao_ganha_dica_de_calendario():
    llm = LLMRoteiro(["Não localizei essa informação nos documentos oficiais."])
    resposta = _motor(llm, [GERAL]).responder("qual o valor da mensalidade?", "s1")
    assert "Calendário" not in resposta.resposta


def test_resposta_longa_que_cita_uma_lacuna_e_mantida():
    texto = ("O trancamento deve ser pedido pelo Espaço do Aluno, respeitando o contrato e as normas do regimento. "
             "Não localizei a data exata do prazo, que é publicada no calendário, mas as condições são estas: "
             "não estar reprovado por frequência e estar com a matrícula ativa no semestre.")
    resposta = _motor(LLMRoteiro([texto]), [GERAL]).responder("como tranco a matricula?", "s1")
    assert resposta.resposta == texto
    assert resposta.modo == "llm"
