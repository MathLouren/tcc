# CI/CD

O workflow `.github/workflows/ci-cd.yml` roda a cada push e pull request:

| Job | Quando | O que faz |
|---|---|---|
| **Testes** | sempre | `pytest` com embedder e LLM simulados (não baixa modelos) |
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
