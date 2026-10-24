# Chatbot Institucional UNIVERSO

Sistema de resposta a perguntas em linguagem natural para os alunos do campus São Gonçalo da Universidade Salgado de Oliveira (UNIVERSO), baseado na arquitetura RAG (*Retrieval-Augmented Generation*). Implementa o sistema descrito no capítulo 3 do TCC "Chatbot Institucional UNIVERSO".

O chatbot localiza nos documentos oficiais os trechos que respondem à pergunta, gera a resposta com um modelo de linguagem executado no **Ollama** e mostra as fontes consultadas. Quando nenhum trecho é relevante, ele informa que não encontrou a informação e indica os canais oficiais. Se o Ollama estiver fora do ar, o sistema continua funcionando em **modo extrativo**, exibindo as frases mais relevantes dos documentos.

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
   ADMIN_KEY=uma-senha-forte                # para as rotas /admin
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
| Validação com 45 perguntas reais, em 7 categorias, incluindo perguntas fora do escopo | `python avaliacao/avaliar.py` | Sim (ou `--sem-llm`) |
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

As rotas administrativas exigem o cabeçalho `X-Admin-Key` com o valor de `ADMIN_KEY`. Se `ADMIN_KEY` estiver vazia, elas ficam desativadas.

| Rota | Função |
|---|---|
| `POST /chat` | Recebe a pergunta e devolve resposta, fontes, modo e tempo de processamento |
| `POST /feedback` | Registra a avaliação útil / não útil de uma resposta |
| `GET /health` | Estado do sistema, modelo de linguagem, documentos e data da última indexação |
| `POST /admin/reindex` | Reindexa os documentos sem reiniciar o servidor |
| `GET /admin/logs?limite=100` | Últimas interações, avaliações e resumo de uso |
| `GET /admin/config` / `PUT /admin/config` | Consulta ou ajusta `top_k`, `limiar`, `temperatura`, `historico_trocas` e `prompt_sistema` |

Exemplo de reindexação:
```
curl.exe -X POST http://localhost:8000/admin/reindex -H "X-Admin-Key: SUA_CHAVE"
```

Os registros ficam em `registros/interacoes.jsonl` e `registros/avaliacoes.jsonl`. Os parâmetros ajustados ficam em `data/parametros.json`.

## Atualização da base de conhecimento

1. Coloque o arquivo em `data/documentos/`. Os formatos aceitos são PDF, HTML, Markdown (.md) e texto (.txt).
2. Acrescente uma entrada em `data/manifesto.json` com título, URL, data de atualização e categoria. Esses dados aparecem nas fontes de cada resposta.
3. Chame `POST /admin/reindex` ou reinicie o servidor depois de apagar a pasta `indice/`.

Documentos digitados em Markdown, com um título `##` por seção, dão os melhores resultados. Calendários em PDF também funcionam: arquivos cujo nome contém "calend" passam por uma limpeza que remove a grade de dias e separa as seções por mês.

### Documentos já incluídos

| Documento | Fonte |
|---|---|
| Calendário Letivo 2026.2 – São Gonçalo | PDF oficial transcrito (original em `data/originais/`) |
| Critérios de Avaliação (Regimento Geral, arts. 78 a 81) | PDF oficial transcrito (original em `data/originais/`) |
| Página da Universidade São Gonçalo | Sítio institucional |
| Página do curso de Sistemas de Informação | Sítio institucional |
| Canais de atendimento e serviços ao aluno | Sítio institucional |

### Documentos pendentes

Estes documentos constam do Quadro 1 do TCC, mas não são públicos ou não estavam disponíveis. Peça-os à coordenação e adicione-os à base:
- matriz curricular e ementas de Sistemas de Informação (use `"categoria": "matriz_curricular"` no manifesto para ativar a pergunta 26 da validação);
- regulamentos de estágio, TCC e atividades complementares;
- Manual Informativo do Aluno (MIA).

## Estrutura do projeto

```
app/
  config.py       configurações (.env) e parâmetros ajustáveis
  documentos.py   extração de PDF/HTML/Markdown, limpeza e seções
  indexacao.py    blocos de ~500 tokens com sobreposição de 50, embeddings e índice FAISS
  embeddings.py   multilingual-e5-base (sentence-transformers) ou Ollama
  llm.py          cliente do Ollama (API nativa) ou de API compatível com OpenAI
  rag.py          recuperação, limiar, prompt, histórico, fontes e modo extrativo
  registros.py    log de interações e avaliações
  servico.py      inicialização, reindexação e ajuste de parâmetros
  main.py         API FastAPI e interface web
web/              interface conversacional (HTML, CSS e JavaScript)
data/             documentos, originais em PDF e manifesto
avaliacao/        perguntas de validação, avaliar.py e sus.py
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
| RF07 Avaliação útil / não útil | `POST /feedback`, botões na interface |
| RF08 Registro das interações | `registros.py` |
| RF09 Atualização e reindexação da base | `POST /admin/reindex` |
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
