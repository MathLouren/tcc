"""Respostas exatas da matriz curricular e proteção contra pedidos de dado feitos pelo modelo."""
import pytest

from app.config import REGRAS_RESPOSTA
from app.llm import LLMIndisponivel
from app.matriz import responder_consulta_matriz
from app.servico import Servico

from conftest import EmbedderFalso, LLMFalso

MATRIZ = """# Fluxograma

## CURSO: Bacharelado em Sistemas de Informação

CURRÍCULO: 109AN

## Disciplinas por período

### 1º período

| Código | Disciplina | Carga horária | Créditos | Pré-requisitos | Marcação |
| --- | --- | --- | --- | --- | --- |
| N004 | Ciências Exatas e Tecnologias | 80 h | 4 | Não indicado | OL |
| N014 | Lógica de Programação e Algoritmos I | 80 h | 4 | Não indicado | — |

### 2º período

| Código | Disciplina | Carga horária | Créditos | Pré-requisitos | Marcação |
| --- | --- | --- | --- | --- | --- |
| N101 | Lógica de Programação de Algoritmos II | 80 h | 4 | N014 | — |
| N134 | Banco de Dados | 80 h | 4 | Não indicado | — |

### 3º período

| Código | Disciplina | Carga horária | Créditos | Pré-requisitos | Marcação |
| --- | --- | --- | --- | --- | --- |
| N476 | Estágio Supervisionado em Sistemas de Informação I | 180 h | 9 | Não indicado | — |
| N535 | Trabalho de Conclusão de Curso em Sistemas de Informação I | 80 h | 4 | N101, N134 | — |

### 4º período

| Código | Disciplina | Carga horária | Créditos | Pré-requisitos | Marcação |
| --- | --- | --- | --- | --- | --- |
| N588 | Trabalho de Conclusão de Curso em Sistemas de Informação II | 80 h | 4 | N535 | — |
| N239 | Estágio Supervisionado em Sistemas de Informação II | 180 h | 9 | Não indicado | — |
| OPT | Optativa em Sistemas de Informação | 80 h | 4 | Não indicado | — |

## Resumo da carga horária por período

| Período | Carga Horária | Aulas Semanais | Ativ. Complem. | Total |
| --- | --- | --- | --- | --- |
| 1º período | 320 | 16 | 30 | 350 horas |
| 4º período | 500 | 25 | 30 | 530 horas |
"""


class LLMRoteiro(LLMFalso):
    """Devolve as respostas na ordem indicada; repete a última se faltar roteiro."""

    def __init__(self, respostas):
        super().__init__()
        self.respostas = list(respostas)

    def gerar(self, mensagens, temperatura):
        self.chamadas.append({"mensagens": mensagens, "temperatura": temperatura})
        return self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]


@pytest.fixture
def motor_com_matriz(ambiente):
    (ambiente.docs_dir / "grade_curricular.md").write_text(MATRIZ, encoding="utf-8")

    def criar(llm=None):
        return Servico(ambiente, embedder=EmbedderFalso(), llm=llm or LLMFalso()).motor

    return criar


@pytest.fixture
def blocos(motor_com_matriz):
    return motor_com_matriz().indice.blocos


@pytest.mark.parametrize("pergunta", [
    "Quais disciplinas do 2º período de Sistemas de Informação?",
    "Quais são as matérias do 2º período?",
    "Quais são as matérias do segundo período?",
    "O que eu estudo no segundo semestre de sistemas de informação?",
    "disciplinas do periodo 2",
    "Quais materias do 2o periodo?",
])
def test_lista_as_disciplinas_somente_do_periodo_pedido(blocos, pergunta):
    texto, usados = responder_consulta_matriz(pergunta, blocos)
    assert "Banco de Dados" in texto and "N101" in texto
    assert "Ciências Exatas" not in texto and "N476" not in texto
    assert [b["secao"] for b in usados] == ["Disciplinas por período > 2º período"]
    assert usados[0]["similaridade"] == 1.0


def test_contagem_do_periodo_nao_usa_o_total_do_curso(motor_com_matriz):
    resposta = motor_com_matriz().responder("Quantas disciplinas tem no 4º período de Sistemas de Informação?")
    assert resposta.modo == "validado"
    assert "tem 3 componentes" in resposta.resposta


def test_horas_do_periodo_vem_do_resumo(blocos):
    texto, usados = responder_consulta_matriz("Quantas horas tem o 4º período de Sistemas de Informação?", blocos)
    assert "530 horas" in texto and "25 aulas semanais" in texto
    assert usados[0]["secao"] == "Resumo da carga horária por período"


@pytest.mark.parametrize("pergunta,esperado,ausente", [
    ("Qual é o pré-requisito de Lógica de Programação de Algoritmos II?", "N014", "OL"),
    ("Qual é o pré-requisito de Lógica de Programação e Algoritmos II?", "N014", "OL"),
    ("Qual o pré-requisito do TCC I?", "N101", "N588"),
    ("Quais disciplinas preciso ter feito para cursar o TCC 2?", "N535", "N101"),
    ("Qual é o pré-requisito de Banco de Dados?", "não indica pré-requisito", "OL"),
    ("Quais disciplinas dependem de Lógica de Programação e Algoritmos I?", "N101", "N476"),
    ("Em qual período é a disciplina Banco de Dados?", "2º período", "1º período"),
    ("Em qual semestre é Banco de Dados?", "2º período", "1º período"),
    ("Qual a carga horária de Estágio Supervisionado em Sistemas de Informação I?", "180 h", "80 h "),
    ("Quantos créditos vale o estágio supervisionado I?", "9 créditos", "4 créditos"),
    ("Qual disciplina tem o código N134?", "Banco de Dados", "N101"),
    ("O que é a disciplina n134?", "Banco de Dados", "N101"),
    ("Quantos estágios tem Sistemas de Informação?", "2 componentes", "N535"),
])
def test_consultas_exatas_por_disciplina(blocos, pergunta, esperado, ausente):
    texto, _ = responder_consulta_matriz(pergunta, blocos)
    assert esperado in texto
    assert ausente not in texto


def test_nome_mais_especifico_vence_e_nao_confunde_i_com_ii(blocos):
    texto, _ = responder_consulta_matriz("Qual é o pré-requisito do Trabalho de Conclusão de Curso em Sistemas de Informação II?", blocos)
    assert "N535" in texto and "N101" not in texto


@pytest.mark.parametrize("pergunta", [
    "Qual é o pré-requisito de Lógica de Programação?",  # ambíguo: I ou II
    "Quando são as provas do 2º período?",
    "Quais disciplinas tenho no 2º semestre de 2026?",
    "Como funciona o trancamento de matrícula?",
    "O que é Banco de Dados?",
    "Quais disciplinas do 9º período?",
])
def test_perguntas_fora_do_padrao_seguem_para_o_rag(blocos, pergunta):
    assert responder_consulta_matriz(pergunta, blocos) is None


def test_sem_matriz_na_base_nada_e_respondido_pelo_codigo(servico):
    assert responder_consulta_matriz("Quais disciplinas do 2º período?", servico.motor.indice.blocos) is None


def test_consulta_exata_nao_chama_o_modelo_nem_pede_esclarecimento(motor_com_matriz):
    llm = LLMFalso()
    motor = motor_com_matriz(llm)
    resposta = motor.responder("Em qual semestre é Banco de Dados?", "sessao-1")
    assert resposta.modo == "validado"
    assert resposta.fontes[0]["secao"] == "Disciplinas por período > 2º período"
    assert llm.chamadas == []


def test_prompt_proibe_o_modelo_de_pedir_dados_mesmo_com_prompt_personalizado(servico):
    servico.atualizar_parametros({"prompt_sistema": "Você é um assistente da universidade. Seja cordial."})
    mensagens = servico.motor.montar_mensagens("Pergunta", [], [])
    assert "Seja cordial" in mensagens[0]["content"]
    assert REGRAS_RESPOSTA in mensagens[0]["content"]
    assert "Nunca faça perguntas" in mensagens[0]["content"]
    assert "Não invente" in mensagens[0]["content"]


@pytest.mark.parametrize("pedido", [
    "Qual é o seu curso?",
    "Para responder, preciso saber qual o seu curso.",
    "Para fornecer a lista, por favor, informe sua modalidade de ensino (presencial ou EAD).",
    "Quais são os códigos das disciplinas?",
    "Quais são o seu curso e o semestre atual?",
])
def test_pedido_do_modelo_gera_nova_tentativa_sem_perguntas(ambiente, pedido):
    llm = LLMRoteiro([pedido, "Resposta direta com base nos trechos."])
    motor = Servico(ambiente, embedder=EmbedderFalso(), llm=llm).motor
    resposta = motor.responder("Como faço o trancamento de matrícula?", "s")
    assert resposta.resposta == "Resposta direta com base nos trechos."
    assert resposta.modo == "llm"
    assert len(llm.chamadas) == 2
    assert "sem fazer perguntas" in llm.chamadas[1]["mensagens"][-1]["content"]


def test_modelo_que_insiste_em_perguntar_nao_chega_ao_aluno(ambiente):
    llm = LLMRoteiro(["Qual é o seu curso?"])
    motor = Servico(ambiente, embedder=EmbedderFalso(), llm=llm).motor
    resposta = motor.responder("Como faço o trancamento de matrícula?", "s")
    assert "Não localizei" in resposta.resposta
    assert resposta.modo == "sem_resultado"
    assert resposta.fontes == []
    assert len(llm.chamadas) == 2


def test_resposta_normal_nao_gera_segunda_chamada(ambiente):
    llm = LLMRoteiro(["O trancamento é feito na secretaria. Veja o seu portal para detalhes."])
    motor = Servico(ambiente, embedder=EmbedderFalso(), llm=llm).motor
    motor.responder("Como faço o trancamento de matrícula?", "s")
    assert len(llm.chamadas) == 1


@pytest.mark.parametrize("pergunta,campo_ausente", [
    ("Quando se inicia a avaliação AF no EaD?", "avaliacao"),
    ("Quando se encerram as avaliações AF e AT?", "modalidade"),
    ("Qual é o período da avaliação AP no EaD?", "avaliacao"),
])
def test_sigla_na_pergunta_dispensa_esclarecimento(servico, pergunta, campo_ausente):
    assert campo_ausente not in servico.motor._campos_para_esclarecer(pergunta)


@pytest.mark.parametrize("pergunta,esperadas", [
    ("Qual o prazo para entregar as ats?", {"AT"}),
    ("quanto preciso tirar na vs?", {"VS"}),
    ("quando é a v1 e a v2?", {"V1", "V2"}),
    ("o que é af e ap no ead?", {"AF", "AP"}),
    ("Quais são as regras?", set()),
    ("Quando são AS provas?", {"AS"}),
])
def test_siglas_em_minusculas_sao_reconhecidas_sem_confundir_o_artigo_as(pergunta, esperadas):
    from app.rag import _siglas_da_consulta

    assert _siglas_da_consulta(pergunta) == esperadas


def test_pergunta_ambigua_continua_pedindo_esclarecimento(servico):
    campos = servico.motor._campos_para_esclarecer("Quando são as provas?")
    assert campos[:2] == ["modalidade", "avaliacao"]


def test_llm_indisponivel_na_segunda_tentativa_usa_modo_extrativo(ambiente):
    class Falha(LLMRoteiro):
        def gerar(self, mensagens, temperatura):
            if self.chamadas:
                raise LLMIndisponivel("fora do ar")
            return super().gerar(mensagens, temperatura)

    motor = Servico(ambiente, embedder=EmbedderFalso(), llm=Falha(["Qual é o seu curso?"])).motor
    assert motor.responder("Como faço o trancamento de matrícula?", "s").modo == "extrativo"


@pytest.mark.parametrize("pergunta", [
    "Quais são as matérias de Sistemas de Informação?",
    "liste as disciplinas do curso de sistemas de informação",
    "Quantas e quais são as disciplinas de Sistemas de Informação?",
])
def test_pedido_de_lista_do_curso_traz_todos_os_periodos_com_o_total(blocos, pergunta):
    texto, usados = responder_consulta_matriz(pergunta, blocos)
    assert "tem 9 componentes curriculares" in texto
    assert "N004" in texto and "N588" in texto and "OPT" in texto
    assert len(usados) == 4


def test_contagem_do_curso_sem_pedido_de_lista_nao_lista(blocos):
    assert responder_consulta_matriz("Quantas matérias tem Sistemas de Informação?", blocos) is None


@pytest.mark.parametrize("continuacao", ["quais são as 9?", "quais são elas?"])
def test_continuacao_com_numero_da_resposta_anterior_lista_o_curso(motor_com_matriz, continuacao):
    llm = LLMFalso()
    motor = motor_com_matriz(llm)
    primeira = motor.responder("quantas matérias tem no total no curso de SI", "sessao-lista")
    assert "9 componentes" in primeira.resposta
    resposta = motor.responder(continuacao, "sessao-lista")
    assert resposta.modo == "validado"
    assert "N004" in resposta.resposta and "N588" in resposta.resposta
    assert llm.chamadas == []


def test_numero_solto_nao_conta_como_assunto_proprio(motor_com_matriz):
    """No manual real, "32" é raro (numera a seção 32): sem a regra, "quais são as 32?" virava pergunta nova."""
    motor = motor_com_matriz()

    class LexicoComNumeroRaro:
        def tem_termo_especifico(self, consulta):
            return "32" in consulta

    motor.indice.lexico = LexicoComNumeroRaro()
    assert motor._parece_continuacao("quais são as 32?")


# --- a matriz só responde o que entendeu ---------------------------------------------------------

@pytest.mark.parametrize("pergunta", [
    "quem dá aula de Banco de Dados?",
    "qual a ementa de N134?",
    "qual o horário de Lógica de Programação e Algoritmos I?",
    "o que significa a marcação OL?",
    "N101 e N134 são do mesmo período?",
    "posso cursar N014 e N004 juntas?",
])
def test_pergunta_que_a_tabela_nao_responde_segue_para_o_rag(blocos, pergunta):
    assert responder_consulta_matriz(pergunta, blocos) is None


@pytest.mark.parametrize("pergunta, presente, ausente", [
    ("quais disciplinas exigem a N014?", "N101 — Lógica de Programação de Algoritmos II", "Não indicado"),
    ("quais matérias têm N101 como pré-requisito?", "N535", "N014 —"),
    ("o que a N535 exige?", "N134 — Banco de Dados", "N588"),
])
def test_direcao_do_pre_requisito_vem_da_ordem_da_frase(blocos, pergunta, presente, ausente):
    texto, _ = responder_consulta_matriz(pergunta, blocos)
    assert presente in texto and ausente not in texto


def test_aulas_semanais_nao_viram_contagem_de_disciplinas(blocos):
    texto, _ = responder_consulta_matriz("quantas aulas por semana tem o primeiro período?", blocos)
    assert "16 aulas semanais" in texto and "componentes" not in texto


# --- consultas novas na tabela ------------------------------------------------------------------

def test_diferenca_de_carga_entre_dois_periodos(blocos):
    texto, _ = responder_consulta_matriz("o 4º período tem quantas horas a mais que o 1º?", blocos)
    assert "500 − 320 = 180" in texto


def test_soma_de_carga_de_periodos_nao_contiguos(blocos):
    texto, _ = responder_consulta_matriz("somando o 1º e o 4º período, quantas horas de disciplinas dá?", blocos)
    assert "320 + 500 = 820" in texto and "dos períodos 1º, 4º" in texto


def test_cada_periodo_lista_os_valores_sem_somar(blocos):
    texto, _ = responder_consulta_matriz("quantas aulas semanais tem cada período?", blocos)
    assert "1º: 16" in texto and "4º: 25" in texto and "=" not in texto


def test_menor_carga_entre_os_periodos(blocos):
    texto, _ = responder_consulta_matriz("qual semestre tem a menor carga horária?", blocos)
    assert "menor carga é a do 1º período, com 320" in texto


def test_disciplinas_agrupadas_pela_carga_horaria(blocos):
    texto, _ = responder_consulta_matriz("que disciplinas têm 180 horas?", blocos)
    assert "N476" in texto and "N239" in texto and "N134" not in texto


def test_marcacao_ol_lista_e_presenca_por_periodo(blocos):
    lista, _ = responder_consulta_matriz("quais disciplinas têm a marcação (OL)?", blocos)
    assert "1 disciplina tem a marcação (OL)" in lista and "N004" in lista and "legenda" in lista
    nenhuma, _ = responder_consulta_matriz("o 2º período tem alguma disciplina marcada com (OL)?", blocos)
    assert nenhuma.startswith("Não.")


def test_codigo_do_curriculo_e_codigo_pelo_nome_da_disciplina(blocos):
    curriculo, _ = responder_consulta_matriz("qual é o código do currículo?", blocos)
    assert "109AN" in curriculo
    disciplina, _ = responder_consulta_matriz("qual o código de Banco de Dados?", blocos)
    assert "N134" in disciplina


@pytest.mark.parametrize("pergunta, esperado", [
    ("preciso ter feito N101 pra fazer N535?", "N101 — Lógica de Programação de Algoritmos II (2º período) é pré-requisito de N535"),
    ("posso cursar o TCC 2 antes do TCC 1?", "é preciso cursar N535 antes de N588"),
    ("é obrigatório concluir N476 antes de N239?", "não registra pré-requisito entre"),
])
def test_relacao_entre_duas_disciplinas_vem_da_coluna_de_pre_requisitos(blocos, pergunta, esperado):
    texto, _ = responder_consulta_matriz(pergunta, blocos)
    assert esperado in texto


def test_categoria_da_totalizacao_nao_recebe_o_total_do_curso(blocos):
    assert responder_consulta_matriz("quantos créditos valem as horas teóricas do currículo?", blocos) is None


# --- cadeias, relações entre períodos, somas por tipo e composição do total ----------------------

TOTALIZACAO = """
## Totalização de carga horária

- Horas Teóricas: 460 h/23 créditos
- Mínimo de Horas de Estágio: 360 h / 18 créditos
- Mínimo de Atividades Complementares: 60 h / 3 créditos
- Horas Práticas: 0 h / 0 créditos
- MINIMO DE HORAS CUMPRIDAS = 880 horas / 44 créditos
"""


@pytest.fixture
def blocos_com_totais(ambiente):
    (ambiente.docs_dir / "grade_curricular.md").write_text(MATRIZ + TOTALIZACAO, encoding="utf-8")
    return Servico(ambiente, embedder=EmbedderFalso(), llm=LLMFalso()).motor.indice.blocos


def test_pre_requisitos_vem_com_o_periodo_de_cada_um(blocos):
    texto, _ = responder_consulta_matriz("de que períodos são os pré-requisitos de N535?", blocos)
    assert "N101 — Lógica de Programação de Algoritmos II (2º período)" in texto
    assert "N134 — Banco de Dados (2º período)" in texto


def test_cadeia_de_pre_requisitos_inclui_os_pre_requisitos_dos_pre_requisitos(blocos):
    texto, _ = responder_consulta_matriz("qual o caminho de pré-requisitos até o TCC II?", blocos)
    assert "Para cursar N588" in texto and "N535 (3º)" in texto
    assert "Para cursar N535" in texto and "N101 (2º)" in texto
    assert "Para cursar N101" in texto and "N014 (1º)" in texto


@pytest.mark.parametrize("pergunta, inicio, presente", [
    ("tem disciplina do 1º período que é pré-requisito de alguma do 2º?", "Sim.", "N014"),
    ("alguma do 1º prende disciplina do 4º?", "Não.", "não registra"),
])
def test_pre_requisitos_entre_periodos(blocos, pergunta, inicio, presente):
    texto, _ = responder_consulta_matriz(pergunta, blocos)
    assert texto.startswith(inicio) and presente in texto


def test_tanto_quanto_exige_pre_requisito_em_todos_os_periodos_citados(blocos):
    # N101 (2º) prende só N535 (3º); nenhuma do 2º prende disciplinas do 3º e do 4º ao mesmo tempo
    texto, _ = responder_consulta_matriz("qual disciplina do 2º é pré-requisito tanto do 3º quanto do 4º?", blocos)
    assert texto.startswith("Não.")


def test_soma_das_horas_por_tipo_de_componente_confere_com_a_totalizacao(blocos_com_totais):
    texto, _ = responder_consulta_matriz("somadas, as disciplinas de estágio dão quantas horas no total?", blocos_com_totais)
    assert "180 (N476) + 180 (N239) = 360 horas" in texto and "mínimo indicado no quadro de totalização" in texto


def test_composicao_do_total_pelos_periodos_e_pela_totalizacao(blocos_com_totais):
    texto, _ = responder_consulta_matriz("como são compostas as horas mínimas da grade?", blocos_com_totais)
    assert "820 h de disciplinas" in texto and "60 h de atividades complementares = 880 h" in texto
    assert "Horas Teóricas 460 + Mínimo de Horas de Estágio 360" in texto and "Com 0 h: Horas Práticas" in texto


def test_quantas_horas_de_estagio_nao_vira_contagem_de_estagios(blocos):
    resposta = responder_consulta_matriz("quantas horas de estágio preciso cumprir?", blocos)
    assert resposta is None or "componentes desse tipo" not in resposta[0]
