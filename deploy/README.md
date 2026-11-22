# CI/CD

O workflow `.github/workflows/ci-cd.yml` roda a cada push e pull request:

| Job | Quando | O que faz |
|---|---|---|
| **Testes** | sempre | `pytest` com embedder e LLM simulados (não baixa modelos) |
| **Qualidade da busca** | sempre | indexa os documentos do repositório e mede o gabarito de 122 perguntas sem LLM; falha abaixo de 118 |
| **Build da imagem** | sempre | compila o `Dockerfile` (sem publicar) para pegar erros antes do deploy |
| **Deploy** | push na `main`, com os dois anteriores verdes | entra no servidor por SSH e executa `chatbot-deploy <sha>` |

## Deploy no servidor (`deploy.sh`)

1. Faz backup dos volumes `dados`, `indice` e `registros` em `/root/backups-chatbot/<data>/` (mantém os 10 últimos).
2. `git reset --hard <sha>` em `/opt/chatbot-universo` e `docker compose up -d --build`.
3. Espera o healthcheck do contêiner ficar `healthy` (até 7 min).
4. Se não ficar saudável, volta para o commit anterior e reconstrói, e o job do GitHub falha.

Os volumes **nunca** são apagados. Os documentos enviados pelo painel ficam no volume `dados` e sobrevivem a
qualquer deploy. Alterações feitas direto em `/opt/chatbot-universo` são descartadas no próximo deploy:
edite pelo GitHub.

## Segredos do GitHub (Settings → Secrets and variables → Actions)

| Segredo | Valor |
|---|---|
| `DEPLOY_HOST` | IP do servidor |
| `DEPLOY_USER` | `root` |
| `DEPLOY_SSH_KEY` | chave privada `/root/.ssh/github_deploy` |
| `DEPLOY_KNOWN_HOSTS` | saída de `ssh-keyscan <IP>` |

A chave pública está em `/root/.ssh/authorized_keys` com `command="/usr/local/bin/chatbot-deploy"`:
ela só consegue disparar o deploy, não abre shell.

## Manutenção

- Depois de alterar `deploy/deploy.sh`: `install -m 755 deploy/deploy.sh /usr/local/bin/chatbot-deploy`
- Deploy manual: `chatbot-deploy` (último commit da `main`)
- Restaurar um backup (exemplo para `dados`):
  ```bash
  docker compose stop
  tar xzf /root/backups-chatbot/<data>/dados.tar.gz -C "$(docker volume inspect -f '{{.Mountpoint}}' chatbot-universo_dados)"
  docker compose start
  ```

## Configuração de desempenho em produção (2026-11-22)

No `.env` do servidor (fora do git):

| Variável | Valor | Por quê |
|---|---|---|
| `LLM_MAX_TOKENS` | `700` | sem teto, o OpenRouter reserva 16.384 tokens por pergunta e recusa (402) com saldo baixo |
| `LLM_MODELOS_RESERVA` | `qwen/qwen3-235b-a22b-2507,meta-llama/llama-3.3-70b-instruct` | o `qwen-2.5-72b` tem um único provedor; na avaliação de 2026-11-22 ele falhou em 21 de 119 perguntas |
| `LLM_TIMEOUT_PRIMEIRO_TOKEN` | `12` | sem nenhum texto em 12 s, a pergunta vai para o modelo reserva |
| `RERANKER_MODELO` | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | reordena os 6 melhores candidatos (~0,3 s de CPU) |
| `CACHE_AQUECER` | `0` | cada pergunta aquecida é uma chamada paga; ligar (ex.: `20`) quando houver créditos |

Em `parametros.json` (rota `PUT /admin/config`): `top_k=3` e `margem_relativa=0.025`.

Gabarito da busca (`diagnostico_busca.py --termos`), na base de produção:

| Configuração | Termos nos trechos | Tokens enviados ao modelo |
|---|---|---|
| antes (top_k=4, sem margem, sem reranker) | 120 / 122 | 1.285 |
| margem 0,025 | 120 / 122 | 1.032 |
| margem 0,025 + reranker + top_k=3 | 121 / 122 | 882 |

Para voltar ao comportamento anterior: apague `RERANKER_MODELO` do `.env`, rode `docker compose up -d`
e, em `/admin/config`, volte `top_k=4` e `margem_relativa=0`.
