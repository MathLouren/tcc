# Protocolo de avaliacao - UNIVERSO Sao Goncalo

## Proveniencia e corpus

O arquivo `perguntas.json` contem 45 perguntas em sete categorias e gabaritos por termos. O historico do arquivo registra sua inclusao em 24/10/2026, mas nao registra autores, procedimento de elaboracao, revisao independente ou uso de LLM. Portanto, nao e possivel afirmar como foram criadas. Os resultados antigos ficam em `avaliacao/resultados/` e nao devem ser comparados diretamente com o novo conjunto porque os documentos e a indexacao mudaram.

O benchmark `perguntas_sao_goncalo.psv` foi redigido com auxilio de LLM (Codex) a partir da pagina oficial da unidade, consultada em 06/11/2026. Os enunciados e gabaritos exigem revisao humana antes da publicacao academica; a checagem automatica apenas confirma cotas, unicidade textual e presenca dos trechos citados no indice. Seu gabarito usa a transcricao verificada `data/documentos/universidade_sao_goncalo.md`. O avaliador registra SHA-256 do documento e configuracao da execucao. A pagina repete alguns blocos e apresenta duas informacoes diferentes para a taxa do Exame de Bolsas. O benchmark nao define uma taxa correta; pergunta apenas se o sistema reconhece a falta de certeza.

## Desenho das 120 perguntas

Sao 100 perguntas respondiveis e 20 que requerem abstencao; 40 faceis, 40 medias e 40 dificeis. As categorias cobrem unidade e contato, infraestrutura, ingresso, cursos, comunidade e fora do escopo. Facil exige localizacao de um fato; medio exige dois fatos ou interpretacao de um limite; dificil exige combinar evidencias, aplicar condicoes ou reconhecer informacao insuficiente. Cada linha registra resposta esperada, evidencias textuais e familia tematica. Variantes na mesma familia nao sao observacoes independentes; por isso o relatorio inclui macro medias por familia. A presenca de 20 perguntas de abstencao, sobretudo entre as dificeis, deve ser considerada ao interpretar medias por nivel.

Uma rodada piloto apontou que o CEP da unidade, ausente da pagina escolhida, estava presente em outro documento indexado. A pergunta SG108 foi substituida por uma informacao ausente do corpus antes da rodada final. O piloto tambem revelou uma variante de recusa ("Nao a localizou") nao reconhecida pelo detector; a regra foi corrigida antes de recalcular. Os arquivos piloto nao sao o resultado final.

## Validacao automatica

Reindexar o corpus com `python -m app.indexacao` e executar `python avaliacao/avaliar_sao_goncalo.py --sem-juiz`. Depois julgar o JSON completo com `python avaliacao/avaliar_sao_goncalo.py --julgar avaliacao/resultados/ARQUIVO.json`. Duas passagens evitam trocar de modelo a cada pergunta. Para cada evidencia, o script identifica blocos indexados que a contem. Hit@k e MRR usam o primeiro bloco relevante; Precision@k e nDCG@k usam relevancia binaria por bloco; Recall@k mede a proporcao de evidencias distintas cobertas pelos k blocos. Perguntas sem resposta ficam fora das metricas de recuperacao e entram na taxa de abstencao. A coincidencia de todos os termos esperados e uma medida automatica lexical, nao equivale a correcao semantica. O tempo da resposta e registrado por pergunta.

O script grava resultados por pergunta em JSONL durante a execucao, alem de JSON, CSV e relatorio Markdown no final. O comando `--sem-llm --sem-juiz` testa somente recuperacao e modo extrativo; seus resultados nao devem ser publicados como avaliacao da arquitetura RAG completa. `--max-perguntas N` serve apenas para teste rapido.

## LLM juiz

O juiz local `llama3.2:latest` e diferente do `gemma3:4b` usado para responder. Ele recebe pergunta, gabarito, trechos recuperados e resposta, e atribui notas de 0 a 4 para fidelidade, pertinencia, completude e abstencao, com justificativa. Respostas invalidas do juiz sao registradas como erro e excluidas das medias. Uma calibracao piloto mostrou pontuacao incoerente na dimensao de abstencao; o relatorio inclui concordancia dessa nota com a decisao observavel de responder ou recusar. Essa dimensao nao deve ser interpretada como validada sem revisao humana. Separar modelo e fixar rubrica reduz acoplamento, mas nao elimina vies; revisar manualmente uma amostra estratificada de pelo menos 20% dos julgamentos, incluindo notas altas e baixas, antes de interpretar os resultados.

## Avaliacao com usuarios

Desenho proposto, ainda sem participantes: 20 voluntarios, 10 alunos e 10 candidatos. Cada pessoa executa seis tarefas, duas por dificuldade, com ordem alternada: SG002, SG018, SG043, SG054, SG079 e SG094. Registrar em `avaliacao/usuarios_tarefas_modelo.csv` se encontrou uma resposta util, clareza (1-5), confianca (1-5), utilidade das fontes (1-5) e comentario livre. Ao final aplicar os dez itens SUS com `avaliacao/sus_respostas_modelo.csv` e calcular por `python avaliacao/sus.py avaliacao/sus_respostas.csv`. Identificar pessoas apenas por codigo; nao registrar dados academicos ou credenciais.

Antes da coleta, explicar objetivo, duracao, participacao voluntaria, possibilidade de desistir e uso anonimo dos resultados; obter consentimento e aprovacao institucional quando exigida. Nao usar respostas de exemplo como dados reais. Apos a coleta, executar `python avaliacao/avaliar_usuarios.py avaliacao/usuarios_tarefas.csv` e o calculo SUS. Relatar n por grupo, tarefas concluidas, medias e dispersao das escalas, SUS e comentarios tematicos. O estudo permanece pendente ate haver coleta efetiva.

## Criterios para publicacao

Publicar sempre o numero de perguntas validas, o numero de respostas geradas pelo LLM (versus modo extrativo), o numero de notas validas do juiz e a versao do corpus. Comparar apenas execucoes com o mesmo conjunto e o mesmo corpus, ou explicitar as diferencas. Nenhuma avaliacao com usuarios pode ser declarada concluida sem participantes reais.
