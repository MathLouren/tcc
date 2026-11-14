"""Testes do fluxo RAG: prompt, histórico, fontes, recusa e modo extrativo (seção 3.6 do TCC)."""
from app.rag import (
    MENSAGEM_NAO_ENCONTRADA,
    _resposta_factual_de_notas_ead,
    _resposta_factual_de_notas_presenciais,
    montar_fontes,
)
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
    # A parte fixa abre o sistema, para o Ollama reaproveitar esse prefixo entre perguntas.
    assert mensagens[0]["content"].startswith(servico.parametros.prompt_sistema.strip())
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
        "e a Rua Lambari, endereço da unidade?",  # continuação: leva a conversa ao modelo
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


def test_pergunta_independente_nao_leva_a_conversa_ao_modelo(servico, llm):
    servico.motor.responder(PERGUNTA_TRANCAMENTO, sessao_id="sessao-3")
    servico.motor.responder("Rua Lambari endereço da unidade", sessao_id="sessao-3")
    assert len(llm.chamadas[-1]["mensagens"]) == 2  # sistema + pergunta atual


def test_pergunta_repetida_vem_do_cache_e_nao_util_esquece(servico, llm):
    primeira = servico.motor.responder(PERGUNTA_TRANCAMENTO, sessao_id="a")
    segunda = servico.motor.responder(PERGUNTA_TRANCAMENTO.upper(), sessao_id="b")
    assert len(llm.chamadas) == 1
    assert segunda.resposta == primeira.resposta and segunda.fontes == primeira.fontes
    servico.motor.esquecer_resposta(PERGUNTA_TRANCAMENTO)
    servico.motor.responder(PERGUNTA_TRANCAMENTO, sessao_id="c")
    assert len(llm.chamadas) == 2


def test_cache_e_esvaziado_quando_os_parametros_mudam(servico, llm):
    servico.motor.responder(PERGUNTA_TRANCAMENTO)
    servico.atualizar_parametros({"top_k": 2})
    servico.motor.responder(PERGUNTA_TRANCAMENTO)
    assert len(llm.chamadas) == 2


def test_numero_que_nao_esta_nos_trechos_gera_nova_tentativa_e_depois_recusa(servico, llm):
    llm.resposta = "O prazo final é 45 de setembro."
    resposta = servico.motor.responder("prazo final de trancamento de matrícula")
    assert len(llm.chamadas) == 2
    assert "datas e valores" in llm.chamadas[-1]["mensagens"][-1]["content"]
    assert resposta.modo == "sem_resultado" and "45" not in resposta.resposta


def test_numero_presente_nos_trechos_e_aceito(servico, llm):
    llm.resposta = "O prazo final é 30 de setembro."
    resposta = servico.motor.responder("prazo final de trancamento de matrícula")
    assert len(llm.chamadas) == 1 and resposta.modo == "llm"


def test_pergunta_sem_nenhuma_palavra_da_base_nao_chama_o_modelo(servico, llm):
    servico.atualizar_parametros({"limiar": 0.0})
    resposta = servico.motor.responder(PERGUNTA_FORA)
    assert resposta.modo == "sem_resultado" and not llm.chamadas


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


def test_pergunta_ambigua_solicita_esclarecimentos_em_etapas(servico, llm):
    primeira = servico.motor.responder("Quando começam as minhas provas?", sessao_id="duvida-provas")
    assert primeira.modo == "esclarecimento"
    assert "presenciais" in primeira.resposta
    assert primeira.fontes == []
    assert llm.chamadas == []

    segunda = servico.motor.responder("Presenciais.", sessao_id="duvida-provas")
    assert segunda.modo == "esclarecimento"
    assert "V1" in segunda.resposta
    assert llm.chamadas == []


def test_resposta_ao_esclarecimento_refaz_a_recuperacao(servico, llm):
    servico.motor.responder("Quando começam as minhas provas?", sessao_id="duvida-refinada")
    servico.motor.responder("Presenciais.", sessao_id="duvida-refinada")
    resposta = servico.motor.responder("V1.", sessao_id="duvida-refinada")

    # A data da V1 é lida direto da linha do calendário, sem o modelo.
    assert resposta.modo == "validado"
    assert resposta.fontes[0]["secao"] == "Outubro"
    assert "05 a 10 de outubro" in resposta.resposta and "V1" in resposta.resposta
    assert llm.chamadas == []


def test_esclarecimento_invalido_repete_apenas_o_dado_pendente(servico, llm):
    servico.motor.responder("Quando começam as minhas provas?", sessao_id="duvida-invalida")
    resposta = servico.motor.responder("Não sei.", sessao_id="duvida-invalida")
    assert resposta.modo == "esclarecimento"
    assert "Não consegui identificar" in resposta.resposta
    assert "presenciais" in resposta.resposta
    assert llm.chamadas == []


def test_pergunta_clara_sobre_avaliacao_nao_pede_confirmacao(servico):
    resposta = servico.motor.responder(
        "Quando começa a V1 das disciplinas presenciais?",
        sessao_id="duvida-clara",
    )
    assert resposta.modo == "validado"
    assert "05 a 10 de outubro" in resposta.resposta
    assert resposta.fontes[0]["secao"] == "Outubro"


def test_plural_informal_de_siglas_prioriza_bloco_que_contem_af_e_at(servico):
    resposta = servico.motor.responder("Quando termina o prazo para execução das ATS E AFS?")
    assert resposta.modo == "validado"
    assert "14 de novembro" in resposta.resposta
    assert "Avaliações AF e AT" in resposta.resposta
    assert resposta.fontes[0]["secao"] == "Novembro"
    assert len(resposta.fontes) == 1


def test_notas_presenciais_usam_formula_e_substituicao_da_r1_documentadas(ambiente):
    (ambiente.docs_dir / "manual_notas.md").write_text(
        "# Manual do Aluno\n\n## Avaliação do rendimento escolar\n"
        "§ 1º Nos cursos de Graduação Presenciais serão observados os seguintes critérios:\n"
        "a. A R1 será oferecida aos alunos que obtiverem nota igual ou inferior a 6,0 na V1.\n"
        "b. A nota obtida na R1 substituirá a nota da V1, desde que seja superior a esta.\n"
        "c. O valor máximo da nota da avaliação R1 será de 7,0.\n"
        "II. A Média Semestral (MS) será calculada da seguinte maneira:\n"
        "a. MS = [(V1 × 2) + VT + (V2 × 2)]/5;\n"
        "b. Se a Média Semestral for igual ou superior a 7,0, o aluno será aprovado diretamente.\n"
        "c. Caso MS seja inferior a 4,0, o aluno estará reprovado por média.\n"
        "d. Se MS for igual ou superior a 4,0 e inferior a 7,0, deverá prestar a VS.\n"
        "É exigida frequência mínima de 75%.\n",
        encoding="utf-8",
    )
    llm = LLMFalso(resposta="Não localizei essa informação.")
    servico = Servico(ambiente, embedder=EmbedderFalso(), llm=llm)

    simples = servico.motor.responder(
        "Tirei 6 na V1, 8 na VT e 7 na V2. Passei direto na graduação presencial?"
    )
    recuperacao = servico.motor.responder(
        "Tirei 5 na V1 e 7 na R1; depois tirei 8 na VT e 6 na V2. Qual minha média e preciso de VS?"
    )
    assert simples.modo == recuperacao.modo == "validado"
    assert "6,8" in simples.resposta and "necessária a VS" in simples.resposta
    assert "R1 substitui a V1" in recuperacao.resposta
    assert "[(7 × 2) + 8 + (6 × 2)] / 5 = 6,8" in recuperacao.resposta
    assert simples.fontes and recuperacao.fontes and llm.chamadas == []

    direta = servico.motor.responder("Tirei 7 na V1, 8 na VT e 7 na V2. Passei direto?")
    assert direta.modo == "validado" and "7,2" in direta.resposta and "aprovação direta" in direta.resposta
    assert _resposta_factual_de_notas_presenciais(
        "Tirei 6 na V1, 8 na VT e 7 na V2. Passei direto?", []
    ) is None


def test_as_considera_notas_anteriores_e_nao_confunde_quanto_com_data(ambiente):
    (ambiente.docs_dir / "manual_ead.md").write_text(
        "# Manual do Aluno\n\n## Avaliação do rendimento escolar\n"
        "Independente dos resultados, é exigida frequência mínima de 75%.\n"
        "§ 2º Nos cursos de Graduação na modalidade de Educação a Distância serão observados os critérios:\n"
        "a. Avaliação Presencial (AP): valor de 0,0 a 6,0 pontos;\n"
        "b. Avaliação Formativa (AF): valor de 0,0 a 2,0 pontos;\n"
        "c. Avaliação por Trabalho (AT): valor de 0,0 a 2,0 pontos;\n"
        "II. A Média Parcial (MP) será calculada como soma: MP = (AP+AF+AT):\n"
        "a. Se a Média Parcial for igual ou superior a 6,0, há aprovação direta;\n"
        "b. Caso MP seja inferior a 4,0, há reprovação por média;\n"
        "c. Se MP for igual ou superior a 4,5 e inferior a 6,0, deverá prestar a AS;\n"
        "e. A Média Final (MF) na AS será calculada como (MP + AS)/2 e deverá ser igual ou superior a 6,0.\n",
        encoding="utf-8",
    )
    llm = LLMFalso(resposta="Não localizei essa informação.")
    servico = Servico(ambiente, embedder=EmbedderFalso(), llm=llm)
    sessao = "notas-ead"
    primeira = servico.motor.responder(
        "tirei 2 na at, e tirei 2 na af, e tirei 0,5 na ap, qual a minha situação?", sessao_id=sessao
    )
    segunda = servico.motor.responder("eu preciso tirar quando na AS para ser aprovado?", sessao_id=sessao)
    terceira = servico.motor.responder(
        "com base nas minhas informações anteriores quanto eu preciso tirar na AS para ser aprovado",
        sessao_id=sessao,
    )
    assert primeira.modo == segunda.modo == terceira.modo == "validado"
    assert "4,5" in primeira.resposta and "necessária a Avaliação Suplementar" in primeira.resposta
    assert "7,5 na AS" in segunda.resposta and "7,5 na AS" in terceira.resposta
    assert "(4,5 + 7,5) / 2 = 6,0" in terceira.resposta
    assert primeira.fontes and segunda.fontes and terceira.fontes and llm.chamadas == []
    sem_historico = servico.motor.responder("Quanto preciso tirar na AS para ser aprovado?", sessao_id="nova-sessao")
    assert sem_historico.modo == "validado"
    assert "Informe suas notas de AP, AF e AT" in sem_historico.resposta
    assert "7,5" not in sem_historico.resposta
    assert _resposta_factual_de_notas_ead(
        "quando é a AS?", servico.motor.historico(sessao), servico.indice.blocos
    ) is None


def test_contagem_da_matriz_considera_todos_os_periodos_e_nao_interpreta_ol(ambiente):
    matriz = """# Matriz curricular de Sistemas de Informação

## Disciplinas por período
### 1º período
| Código | Disciplina | Marcação |
| --- | --- | --- |
| N001 | Disciplina A | OL |
| N002 | Disciplina B | — |

### 2º período
| Código | Disciplina | Marcação |
| --- | --- | --- |
| OPT | Optativa em Sistemas de Informação | — |

## Contagem dos componentes curriculares
A matriz apresenta 3 componentes: 2 não identificados como optativos e 1 optativo.
"""
    (ambiente.docs_dir / "matriz_curricular_sistemas_informacao_109AN.md").write_text(
        matriz, encoding="utf-8"
    )
    llm = LLMFalso()
    servico = Servico(ambiente, embedder=EmbedderFalso(), llm=llm)

    total = servico.motor.responder("Quantas matérias tem Sistemas de Informação?")
    obrigatorias = servico.motor.responder("Quantas matérias obrigatórias tem Sistemas de Informação?")
    sigla = servico.motor.responder("quantas matérias tem no total no curso de SI")
    singular = servico.motor.responder(
        "Qual é o total de disciplina no curso de sistemas de informação contando todos os periodos?"
    )

    assert total.modo == "validado"
    assert "3 componentes curriculares" in total.resposta
    assert "2 não identificados como optativos" in total.resposta
    assert obrigatorias.modo == "validado"
    assert "são 2" in obrigatorias.resposta
    assert "(OL) não foi usada" in obrigatorias.resposta
    assert obrigatorias.fontes[0]["secao"] == "Contagem dos componentes curriculares"
    assert sigla.modo == singular.modo == "validado"
    assert "3 componentes curriculares" in sigla.resposta
    assert "3 componentes curriculares" in singular.resposta
    assert sigla.fontes[0]["secao"] == "Contagem dos componentes curriculares"
    assert servico.motor._resposta_factual_da_matriz("Quantas matérias tem no curso de Psicologia?") is None
    assert llm.chamadas == []


def test_contagem_da_matriz_funciona_com_arquivo_enviado_com_outro_nome(ambiente):
    (ambiente.docs_dir / "grade_curricular.md").write_text(
        """# Fluxograma

CURRÍCULO: 109AN

## Disciplinas por período

### 1º período

| Código | Disciplina | Marcação |
| --- | --- | --- |
| N001 | Disciplina A | OL |
| OPT | Optativa em Sistemas de Informação | — |
""",
        encoding="utf-8",
    )
    llm = LLMFalso()
    servico = Servico(ambiente, embedder=EmbedderFalso(), llm=llm)

    resposta = servico.motor.responder("Quantas matérias tem Sistemas de Informação?")

    assert resposta.modo == "validado"
    assert "2 componentes curriculares" in resposta.resposta
    assert llm.chamadas == []


def test_contagem_nao_usa_matriz_de_outro_curriculo(ambiente):
    (ambiente.docs_dir / "outra_grade.md").write_text(
        """# Fluxograma

CURRÍCULO: 200XX

## Disciplinas por período

### 1º período

| Código | Disciplina | Marcação |
| --- | --- | --- |
| N001 | Disciplina A | OL |
""",
        encoding="utf-8",
    )
    servico = Servico(ambiente, embedder=EmbedderFalso(), llm=LLMFalso())

    assert servico.motor._resposta_factual_da_matriz("Quantas matérias tem Sistemas de Informação?") is None


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


def test_datas_de_avaliacao_listam_abertura_e_fechamento_e_ignoram_linha_sem_data(ambiente):
    (ambiente.docs_dir / "calendario_teste.md").write_text(
        "# Calendário EaD\n\n## Agosto de 2026\n- 17 de agosto de 2026 – Abertura do período de realização da Avaliação AF\n\n"
        "## Setembro de 2026\n- 14 de setembro de 2026 – Abertura do período de realização da Avaliação AT\n\n"
        "## Novembro de 2026\n- 14 de novembro de 2026 – Fechamento do período de realização das Avaliações AF e AT\n\n"
        "## Legenda\nInício e término do semestre letivo | Verificações (AT, AF, AP, AS) | Domingos e Feriados\n",
        encoding="utf-8",
    )
    llm = LLMFalso()
    servico = Servico(ambiente, embedder=EmbedderFalso(), llm=llm)

    ambas = servico.motor.responder("Qual o prazo para inicio e final das at e af?")
    assert ambas.modo == "validado" and not llm.chamadas
    for esperado in ("17 de agosto de 2026", "14 de setembro de 2026", "14 de novembro de 2026"):
        assert esperado in ambas.resposta
    assert "Verificações" not in ambas.resposta

    inicio = servico.motor.responder("quando começa a at?")
    assert "14 de setembro de 2026" in inicio.resposta and "novembro" not in inicio.resposta
    fim = servico.motor.responder("até quando posso fazer a AF?")
    assert "14 de novembro de 2026" in fim.resposta and "agosto" not in fim.resposta

    entrega = servico.motor.responder("Qual prazo final para entrega das AT?")
    assert entrega.modo == "validado"
    assert "14 de novembro de 2026" in entrega.resposta
    assert entrega.fontes[0]["secao"] == "Novembro de 2026"


def test_verificacao_de_numeros_so_olha_datas_e_valores_em_reais():
    from app.rag import _numeros_sem_apoio

    apoio = "frequência mínima de 75% ... 14 de setembro de 2026 – Abertura ... 09 a 28 de novembro ... multa de R$ 3,00 ... 10/09/2019"
    assert _numeros_sem_apoio("Você pode faltar até 25% das aulas. A média seria 5,9.", apoio) == set()
    assert _numeros_sem_apoio("Abre em 14 de setembro, de 09 de novembro até 28, multa de R$ 3,00, portaria de 10/09.", apoio) == set()
    assert _numeros_sem_apoio("Abre em 14 de outubro e a multa é R$ 5,00.", apoio) == {"14/10", "R$5"}
