# Avaliação do chatbot UNIVERSO São Gonçalo

## Resumo

Implementar a avaliação em `C:\Users\Matheus\Desktop\chatbot-universo`, onde já estão as 45 perguntas, o avaliador automático e o cálculo SUS. O conjunto atual tem sete categorias, mas o arquivo e seu histórico não registram **quem elaborou as perguntas, quais critérios usou ou se houve auxílio de LLM**. A metodologia do TCC deve declarar essa origem como não comprovada, sem atribuí-la retrospectivamente.

## Conjunto de perguntas

- Atualizar o documento indexado de São Gonçalo com base na [página oficial atual](https://universo.edu.br/a-universo/estrutura/sao-goncalo/), registrar data e versão da fonte e reindexar o conteúdo antes da avaliação.
- Criar **120 perguntas**: 100 respondíveis pela página e 20 que exigem abstenção. Distribuí-las em 40 fáceis, 40 médias e 40 difíceis, organizadas por unidade e contato, infraestrutura, ingresso, cursos, comunidade acadêmica e fora do escopo.
- Registrar para cada pergunta a dificuldade, categoria, resposta esperada, trecho de evidência e documento relevante. Revisar paráfrases para que perguntas semelhantes não inflem os resultados.
- Excluir do gabarito afirmações contraditórias da página, como as informações divergentes sobre a taxa do Exame de Bolsas, até que exista uma fonte oficial inequívoca. Preservar as 45 perguntas e seus relatórios anteriores como referência histórica, sem comparar diretamente execuções feitas sobre versões diferentes do corpus.

## Três validações

- **Automática:** adaptar o avaliador para executar as 120 perguntas e recalcular Hit@4, MRR, precisão, recall e nDCG da recuperação com relevância anotada por trecho. Medir também correção da resposta, abstenção e tempo, com resultados por categoria e dificuldade. Registrar modelo, parâmetros e versão do corpus em cada execução.
- **LLM juiz:** usar o `llama3.2:latest` já disponível no Ollama, separado do `gemma3:4b` configurado para responder. Aplicar rubrica fixa para fidelidade às fontes, pertinência, completude e abstenção, guardar justificativas estruturadas e revisar manualmente uma amostra dos julgamentos.
- **Usuários:** preparar tarefas, consentimento, formulário de avaliação das respostas e SUS. Como ainda não há desenho nem participantes definidos, adotar como proposta inicial **20 pessoas: 10 alunos e 10 candidatos, com seis tarefas por pessoa**, distribuídas entre os três níveis. Gerar resultados humanos somente após coleta real; o CSV modelo vazio não será apresentado como validação concluída.

## Verificação e entrega

Testar o gabarito e os cálculos com casos conhecidos, executar o benchmark contra o chatbot com Qdrant e Ollama ativos e entregar a lista das 120 perguntas, resultados por pergunta, métricas agregadas e texto metodológico para o TCC. O relatório distinguirá resultados efetivamente medidos de instrumentos de avaliação com usuários ainda aguardando participantes.
