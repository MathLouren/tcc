# Chatbot Institucional UNIVERSO

Sistema de resposta a perguntas em linguagem natural para os alunos do campus São Gonçalo da Universidade Salgado de Oliveira (UNIVERSO), baseado na arquitetura RAG (*Retrieval-Augmented Generation*). Implementa o sistema descrito no capítulo 3 do TCC "Chatbot Institucional UNIVERSO".

O chatbot localiza nos documentos oficiais os trechos que respondem à pergunta, gera a resposta com um modelo de linguagem executado no **Ollama** e mostra as fontes consultadas. Quando uma dúvida depende de curso, modalidade, semestre ou tipo de avaliação, ele solicita apenas os esclarecimentos necessários e refaz a busca com as respostas do aluno. Quando nenhum trecho é relevante, ele informa que não encontrou a informação e indica os canais oficiais. Se o Ollama estiver fora do ar, o sistema continua funcionando em **modo extrativo**, exibindo as frases mais relevantes dos documentos.

### Como uma pergunta é respondida

1. **Saudação ou "quem é você"**: resposta direta, sem busca.
2. **Dado exato da grade** (disciplinas por período, pré-requisito, créditos, carga horária total, horas de estágio e de atividades complementares, duração do curso): lido da tabela da matriz pelo código.
3. **Data de avaliação** (AT, AF, AP, AS, V1...): lida das linhas do calendário pelo código, com abertura e fechamento.
4. **Esclarecimento**: quando a resposta depende de modalidade, avaliação, curso ou semestre, o chatbot pergunta antes.
5. **Busca híbrida**: similaridade vetorial somada à busca por palavras (BM25 com sinônimos de aluno, como "matéria" para "disciplina"). Sem nenhuma palavra em comum com a base, a recusa é imediata.
6. **Geração**: o modelo recebe os trechos, as regras obrigatórias, o significado das siglas citadas (extraído dos próprios documentos) e equivalências de vocabulário ("segunda chamada" = "2ª chamada").
7. **Verificações**: datas e valores em reais que não estão nos trechos geram nova tentativa e, se persistirem, recusa. Pedidos de dado feitos pelo modelo são descartados.
8. **Citação do documento**: se o modelo recusa mas existe uma frase que contém os termos da pergunta, o chatbot devolve a frase original do documento, entre aspas, com a fonte.
9. **Cache**: a mesma pergunta, sem depender da conversa, é respondida na hora. Marcar "não útil" tira a resposta do cache.

Perguntas de continuação ("e no EaD?", "qual a nota máxima dela?") usam a pergunta anterior na busca. Perguntas independentes não levam a conversa ao modelo.

### Fluxo de esclarecimento

1. O backend identifica ambiguidades acadêmicas conhecidas antes de gerar uma resposta.
2. A pendência e a pergunta original são guardadas pelo `sessao_id`.
3. Cada resposta do aluno preenche um dado ausente, sem repetir dados já informados.
4. Ao completar os esclarecimentos, o backend junta a pergunta original aos dados informados e executa uma nova recuperação.
5. O LLM recebe a pergunta original, os esclarecimentos e somente os trechos recuperados nessa nova busca.

Respostas intermediárias usam o modo `esclarecimento`, não apresentam fontes e não exibem os botões de avaliação. Perguntas claras continuam diretamente para a recuperação. Perguntas claras sem cobertura documental recebem a mensagem de limitação institucional, sem perguntas artificiais.

## Como rodar no ambiente com Ollama

1. **Prepare o Ollama** (no computador onde ele roda):
   ```
   ollama pull llama3.1:8b
   ```
   Se o Ollama estiver em **outra máquina** da rede, inicie-o aceitando conexões externas, definindo a variável `OLLAMA_HOST=0.0.0.0` antes de `ollama serve`.

2. **Configure o endereço do Ollama.** Na primeira execução o script cria o arquivo `.env` a partir de `.env.example`. Edite apenas:
   ```
   OLLAMA_URL=http://localhost:11434        # ou http://IP_DA_MAQUINA:11434
   LLM_MODELO=llama3.1:8b                   # o modelo baixado no passo 1
   EMAIL=voce@exemplo.com                   # login do painel /admin
   PASS=uma-senha-forte                     # senha do painel /admin
   ```

3. **Inicie o chatbot:**
   - Windows: dê dois cliques em `iniciar.bat` (ou rode-o no terminal).
   - Linux/macOS: `sh iniciar.sh`

   Na primeira vez o script cria o ambiente virtual, instala as dependências, baixa o modelo de embeddings (cerca de 1 GB, precisa de internet uma única vez) e indexa os documentos. Depois disso a inicialização leva poucos segundos.

4. **Acesse** http://localhost:8000. A documentação interativa da API fica em http://localhost:8000/docs.

O selo no topo da página fica verde quando o modelo de linguagem está acessível e laranja quando o sistema está em modo extrativo.

### Requisitos

- Python 3.11 ou 3.12 (o `iniciar.bat` procura essas versões primeiro).
- Ollama com um modelo de conversa baixado. Para máquinas com pouca memória, use `llama3.2:3b` ou `gemma3:4b`. Para melhor português, `qwen2.5:7b`.
- Cerca de 3 GB livres em disco para dependências e modelo de embeddings.

### Alternativa: tudo pelo Ollama, sem PyTorch

Para não instalar o PyTorch nem baixar o modelo de embeddings do HuggingFace, use o próprio Ollama também para os embeddings:
```
ollama pull bge-m3
```
No `.env`, acrescente `EMBEDDING_PROVEDOR=ollama` e `EMBEDDING_MODELO=bge-m3`, e remova a linha `sentence-transformers` do `requirements.txt` antes da primeira instalação. O índice é reconstruído automaticamente quando o modelo de embeddings muda.

## Checklist da primeira execução

1. Rodar os testes automatizados (não precisam do Ollama):
   ```
   .venv\Scripts\python -m pip install -r requirements-dev.txt
   .venv\Scripts\python -m pytest
   ```
2. Validar a recuperação sem o LLM: `.venv\Scripts\python avaliacao\avaliar.py --sem-llm`
3. Validar com o Ollama: `.venv\Scripts\python avaliacao\avaliar.py`
4. Ajustar o limiar de similaridade com o valor sugerido no relatório (seção abaixo).
5. Abrir a interface, fazer algumas perguntas e conferir as fontes exibidas.

No Linux/macOS, troque `.venv\Scripts\python` por `.venv/bin/python`.

## Testes e validação

| O que | Comando | Precisa do Ollama? |
|---|---|---|
| Testes unitários e de API (ingestão, recuperação, prompt, histórico, recusa, modo extrativo, rotas e autenticação) | `python -m pytest` | Não |
| Busca sem o modelo: os trechos enviados contêm os termos esperados? | `python avaliacao/diagnostico_busca.py --termos` | Não |
| Respostas com o modelo real, gabarito original (35 com resposta, 12 sem) | `python avaliacao/validar_respostas.py` | Sim |
| Gabarito ampliado (96 com resposta, 15 sem, 8 conversas) | `python avaliacao/validar_respostas.py --gabarito avaliacao/gabarito_ampliado.json` | Sim |
| Gabarito do calendário EaD (22 com resposta, 4 sem, 2 conversas) | `python avaliacao/validar_respostas.py --gabarito avaliacao/gabarito_calendario.json` | Sim |
| Validação antiga com 45 perguntas, em 7 categorias | `python avaliacao/avaliar.py` | Sim (ou `--sem-llm`) |
| Pontuação do questionário de usabilidade SUS | `python avaliacao/sus.py avaliacao/sus_respostas.csv` | Não |

Os testes automatizados usam um embedder e um LLM simulados, por isso rodam em segundos e não dependem de rede.

O script `avaliar.py` grava em `avaliacao/resultados/`:
- um relatório em Markdown com Hit@k, MRR, precisão do contexto, acurácia automática, abstenção fora do escopo, tempos de resposta e limiar sugerido;
- os resultados completos em JSON;
- uma planilha CSV para a revisão humana, com colunas para dois avaliadores e para o consenso, como descrito na seção 4.3 do TCC.

Para o SUS, copie `avaliacao/sus_respostas_modelo.csv` para `avaliacao/sus_respostas.csv` e preencha uma linha por participante com as respostas de 1 a 5. O questionário e o TCLE estão nos apêndices B e C do TCC.

### Calibração do limiar de similaridade

O limiar padrão é 0,60, conforme o TCC. Os modelos E5 costumam gerar similaridades altas mesmo para textos pouco relacionados (entre 0,70 e 0,90), então o limiar ideal depende do modelo. O relatório de `avaliar.py` sugere o valor que melhor separa as perguntas dentro e fora do escopo. Para aplicá-lo sem reiniciar:
```
curl.exe -X PUT http://localhost:8000/admin/config -H "X-Admin-Key: SUA_CHAVE" -H "Content-Type: application/json" -d "{\"limiar\": 0.82}"
```

## Administração

### Painel `/admin`

Acesse `http://localhost:8000/admin` e entre com `EMAIL` e `PASS` do `.env`. O painel tem duas abas:

- **Logs de conversas**: nome do aluno, pergunta, resposta, modo e avaliação de cada interação.
- **Documentos**:
  - envio de um ou vários PDFs, convertidos um a um, com o estado de cada arquivo na fila;
  - relatório de cada conversão (páginas, seções, blocos, tabelas, tokens) e avisos de qualidade, por exemplo PDF digitalizado com pouco texto;
  - botão **Ver**: mostra os blocos que o chatbot consulta e permite baixar o Markdown, o JSON do Docling, os blocos em JSONL e o PDF original;
  - **Testar a busca**: mostra os trechos que uma pergunta recupera, sem chamar o modelo;
  - remoção de documentos.

Antes de conversar, o aluno precisa informar o nome (obrigatório; gravado nos logs).

As rotas `/admin/...` aceitam a sessão do painel (cookie) ou o cabeçalho `X-Admin-Key` com o valor opcional de `ADMIN_KEY`, para scripts. Sem `EMAIL`/`PASS` e sem `ADMIN_KEY`, ficam desativadas.

| Rota | Função |
|---|---|
| `POST /chat` | Recebe a pergunta e devolve resposta, fontes, modo e tempo de processamento |
| `POST /chat/stream` | Igual a `/chat`, mas envia o texto enquanto o modelo escreve (uma linha JSON por evento: `parcial`, depois `final`); é a rota usada pela interface web |
| `POST /feedback` | Registra a avaliação útil / não útil de uma resposta |
| `GET /health` | Estado do sistema, modelo de linguagem, documentos e data da última indexação |
| `POST /admin/reindex` | Reindexa os documentos sem reiniciar o servidor |
| `POST /admin/documentos` | Recebe um PDF, converte com Docling, reindexa a base e devolve o relatório da conversão |
| `GET /admin/documentos/{arquivo}/conteudo` | Relatório e blocos extraídos de um documento |
| `GET /admin/documentos/{arquivo}/baixar?formato=markdown\|json\|blocos\|pdf` | Baixa um dos formatos do documento |
| `POST /admin/busca` | Trechos que uma pergunta recupera, sem chamar o modelo |
| `GET /admin/logs?limite=100` | Últimas interações, avaliações e resumo de uso |
| `GET /admin/config` / `PUT /admin/config` | Consulta ou ajusta `top_k`, `limiar`, `temperatura`, `historico_trocas`, `peso_lexical` e `prompt_sistema` |

Exemplo de reindexação:
```
curl.exe -X POST http://localhost:8000/admin/reindex -H "X-Admin-Key: SUA_CHAVE"
```

Os registros ficam em `registros/interacoes.jsonl` e `registros/avaliacoes.jsonl`. Os parâmetros ajustados ficam em `data/parametros.json`.

## Atualização da base de conhecimento

Para PDFs, use o upload administrativo. O sistema executa o Docling com OCR e reconhecimento de tabelas e guarda cada documento em quatro formatos:

| Formato | Onde fica | Para que serve |
|---|---|---|
| PDF original | `data/originais/uploads/` | Fonte de verdade; permite reconverter |
| Markdown limpo | `data/documentos/` | Texto que é indexado; pode ser corrigido à mão |
| JSON do Docling | `data/docling/` | Posição de cada texto na página; usado para reconstruir fluxogramas e calendários |
| Blocos em JSONL | `indice/blocos/` | Uma linha por bloco, com documento, seção, contexto herdado e metadados: é o que o chatbot consulta |

Tratamentos aplicados na conversão:

- **Calendário diagramado** (mini-calendário de cada mês ao lado dos eventos): cada evento é atribuído ao mês pela posição na página e escrito com a data completa, por exemplo "14 de setembro de 2026 – Abertura do período de realização da Avaliação AT".
- **Fluxograma curricular**: vira tabela de disciplinas por período.
- **Cabeçalhos e rodapés repetidos** e logotipos: removidos.
- **Tabelas longas**: cada bloco leva a frase que apresenta a tabela e os nomes das colunas.
- **Regimentos**: os blocos são cortados nos parágrafos (§) e artigos, para não misturar a regra de um escopo com a de outro.

O manifesto e o índice são atualizados somente se o processamento terminar corretamente.

```powershell
curl.exe -X POST http://localhost:8000/admin/documentos `
  -H "X-Admin-Key: SUA_CHAVE" `
  -F "arquivo=@C:\caminho\manual.pdf;type=application/pdf" `
  -F "titulo=Manual Acadêmico" `
  -F "url=https://universo.edu.br/documentos/manual.pdf" `
  -F "categoria=manual" `
  -F "atualizado_em=2026-11-07"
```

O primeiro PDF pode demorar mais porque o Docling baixa seus modelos locais. Os uploads seguintes reutilizam o cache. O limite padrão é 25 MB e pode ser alterado por `PDF_UPLOAD_MAX_MB` no `.env`.

Para HTML, Markdown ou texto, coloque o arquivo em `data/documentos/`, acrescente a entrada correspondente em `data/manifesto.json` e chame `POST /admin/reindex`.

Documentos digitados em Markdown, com um título `##` por seção, dão os melhores resultados. PDFs enviados pela rota administrativa sempre são convertidos pelo Docling antes da indexação.

### Documentos na base

| Documento | Arquivo |
|---|---|
| Manual do Aluno 2026-2 (MIA) | `universo_l_mia_l_2026_2_l_03112026_2_1.md` |
| Grade curricular de Sistemas de Informação (matriz 109AN) | `grade_curricular.md` |
| Calendário Acadêmico EaD/semipresencial 2026.2 | `calendario_ead_2026_2.md` |

Ainda falta o **calendário dos cursos presenciais**: sem ele, datas de V1, V2, VS e R1 não têm resposta. Regulamentos de estágio, TCC e atividades complementares também melhoram a cobertura.

## Estrutura do projeto

```
app/
  config.py       configurações (.env) e parâmetros ajustáveis
  docling_pdf.py  conversão de PDF para Markdown e JSON estruturado
  docling_layout.py      fluxogramas, tabelas e cabeçalhos repetidos, pela posição na página
  docling_calendario.py  calendários diagramados: cada evento no seu mês, com a data completa
  lexico.py       busca por palavras (BM25) com sinônimos, usada na busca híbrida
  matriz.py       respostas exatas lidas da tabela da grade curricular
  documentos.py   extração de PDF/HTML/Markdown, limpeza e seções
  indexacao.py    blocos de ~500 tokens com sobreposição de 50, embeddings e índice FAISS
  embeddings.py   multilingual-e5-base (sentence-transformers) ou Ollama
  llm.py          cliente do Ollama (API nativa) ou de API compatível com OpenAI
  rag.py          busca híbrida, esclarecimentos, prompt, verificações, citação, cache e modo extrativo
  registros.py    log de interações e avaliações
  servico.py      inicialização, reindexação e ajuste de parâmetros
  main.py         API FastAPI e interface web
web/              interface conversacional (HTML, CSS e JavaScript)
data/             documentos, originais em PDF, JSON do Docling e manifesto
avaliacao/        gabaritos, validar_respostas.py, diagnostico_busca.py, avaliar.py e sus.py
tests/            testes automatizados (pytest)
```

## Correspondência com o TCC

| Requisito | Onde está implementado |
|---|---|
| RF01 Perguntas em linguagem natural | `web/`, `POST /chat` |
| RF02 Busca semântica | `rag.py` (`recuperar`), `indexacao.py` (`IndiceVetorial`) |
| RF03 Resposta baseada nos trechos | `rag.py` (`montar_mensagens`), `config.py` (`PROMPT_PADRAO`) |
| RF04 Fontes com documento e seção | `rag.py` (`montar_fontes`), `data/manifesto.json` |
| RF05 Aviso quando não encontra a informação | `rag.py` (`MENSAGEM_NAO_ENCONTRADA`, limiar) |
| RF06 Contexto das últimas mensagens | `rag.py` (histórico por sessão, últimas 2 trocas) |
| RF06a Esclarecimento de perguntas ambíguas | `rag.py` (pendência por sessão e nova recuperação) |
| RF07 Avaliação útil / não útil | `POST /feedback`, botões na interface |
| RF08 Registro das interações | `registros.py` |
| RF09 Atualização e reindexação da base | `POST /admin/documentos`, Docling e `POST /admin/reindex` |
| RF10 Ajuste de parâmetros | `PUT /admin/config` |
| RNF01 Web responsiva | `web/style.css` |
| RNF02 Resposta em menos de 5 s | medido por `avaliar.py` (`tempo_ms`) |
| RNF03 Somente documentos oficiais | prompt de sistema e limiar de relevância |
| RNF04 Sem dados pessoais | base apenas com documentos públicos; o log não pede identificação |
| RNF05 Troca de modelos sem alterar as camadas | `LLM_MODELO`, `LLM_API`, `EMBEDDING_PROVEDOR` no `.env` |
| RNF06 Atualização sem alterar código | pasta `data/documentos` e manifesto |
| RNF07 Credenciais fora do código | arquivo `.env` |
| RNF08 Baixo custo | modelos locais via Ollama, sem custo por requisição |

## Docker (opcional)

```
docker build -t chatbot-universo .
docker run -p 8000:8000 --env-file .env -e OLLAMA_URL=http://host.docker.internal:11434 chatbot-universo
```

## Solução de problemas

| Sintoma | Causa provável e solução |
|---|---|
| Selo laranja e respostas em "modo extrativo" | O Ollama não respondeu ou o modelo não foi baixado. Confira `OLLAMA_URL`, rode `ollama list` e `ollama pull <modelo>`. |
| Ollama em outra máquina não conecta | Inicie o Ollama com `OLLAMA_HOST=0.0.0.0` e libere a porta 11434 no firewall. |
| Erro ao instalar `faiss-cpu` | Remova a linha do `requirements.txt`. O sistema usa uma busca equivalente em NumPy. |
| Erro ao instalar o PyTorch | Use Python 3.12 ou a alternativa "tudo pelo Ollama" descrita acima. |
| Primeira resposta demora | O Ollama carrega o modelo na memória na primeira chamada. As seguintes são mais rápidas. |
| Respostas cortadas ou ignorando as instruções | Aumente `LLM_NUM_CTX` no `.env` (padrão 8192). |
| Perguntas fora do escopo recebem resposta | Aumente o limiar com o valor sugerido por `avaliar.py`. |
| Perguntas válidas recebem "não localizei" | Diminua o limiar ou acrescente o documento que falta à base. |
