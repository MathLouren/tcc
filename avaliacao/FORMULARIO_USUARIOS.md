# Roteiro de teste com usuarios

Codigo do participante: __________  Grupo: [ ] aluno  [ ] candidato

Apresente as seis tarefas em ordem alternada entre participantes para reduzir efeito de aprendizagem. O participante deve formular a pergunta com suas proprias palavras; os IDs abaixo identificam o objetivo, nao um texto a ser copiado obrigatoriamente.

| ID | Nivel | Objetivo da tarefa |
|---|---|---|
| SG002 | Facil | Encontrar a rua da unidade. |
| SG018 | Facil | Identificar desde quando uma nota do ENEM e aceita. |
| SG043 | Medio | Encontrar atributos dos laboratorios. |
| SG054 | Medio | Verificar se a data do vestibular on-line pode ser escolhida. |
| SG079 | Dificil | Combinar ano, nota e limite de desconto do ENEM. |
| SG094 | Dificil | Identificar duas estudantes de Biomedicina por seus relatos. |

Depois de cada tarefa, registre uma linha em `usuarios_tarefas.csv` conforme `usuarios_tarefas_modelo.csv`:

- `concluiu`: `sim` se a pessoa encontrou uma resposta util e coerente com a fonte; caso contrario `nao`.
- `clareza_1_a_5`: 1 muito confusa, 5 muito clara.
- `confianca_1_a_5`: 1 nenhuma confianca, 5 muita confianca.
- `utilidade_fontes_1_a_5`: 1 nada uteis, 5 muito uteis.
- `comentario`: observacao livre sem dados pessoais.

Ao final, aplique os dez itens SUS listados em `sus.py`, com respostas de 1 a 5, e registre uma linha por pessoa em `sus_respostas.csv` conforme `sus_respostas_modelo.csv`. Execute `python avaliacao/avaliar_usuarios.py avaliacao/usuarios_tarefas.csv` e `python avaliacao/sus.py avaliacao/sus_respostas.csv` somente depois de preencher dados reais.
