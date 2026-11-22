#!/usr/bin/env bash
# Deploy contínuo do chatbot no servidor (chamado pelo GitHub Actions via SSH).
#
# Instalado em /usr/local/bin/chatbot-deploy (fora do repositório, para que o `git reset` não altere
# o script enquanto ele roda). A chave SSH do GitHub Actions só consegue executar este script
# (opção command= no authorized_keys). Depois de alterar este arquivo, reinstale com:
#   sudo install -m 755 deploy/deploy.sh /usr/local/bin/chatbot-deploy
#
# Uso: chatbot-deploy [<sha do commit>]   (sem argumento: último commit de origin/main)
#
# Os dados ficam nos volumes Docker (dados, indice, registros, cache) e NUNCA são apagados aqui:
# antes de cada deploy é feito um backup deles em $BACKUP_DIR, e se a nova versão não ficar
# saudável o código volta para o commit anterior e o contêiner antigo é reconstruído.
set -euo pipefail

PROJETO_DIR=${PROJETO_DIR:-/opt/chatbot-universo}
BACKUP_DIR=${BACKUP_DIR:-/root/backups-chatbot}
MANTER_BACKUPS=${MANTER_BACKUPS:-10}
TIMEOUT_SAUDE=${TIMEOUT_SAUDE:-420}   # segundos; o healthcheck da imagem tem start-period de 180 s
BRANCH=main
VOLUMES=(dados indice registros)      # cache (modelos baixados) não precisa de backup

log() { echo "[$(date '+%F %T')] $*"; }

# Pela chave restrita, o argumento chega em SSH_ORIGINAL_COMMAND.
ALVO=${1:-${SSH_ORIGINAL_COMMAND:-}}
if [[ -n "$ALVO" && ! "$ALVO" =~ ^[0-9a-f]{7,40}$ ]]; then
  log "argumento inválido: esperado um sha de commit"; exit 2
fi

# Um deploy por vez.
exec 9>/var/lock/chatbot-deploy.lock
flock -w 600 9 || { log "outro deploy em andamento"; exit 3; }

cd "$PROJETO_DIR"
export COMPOSE_PROJECT_NAME=chatbot-universo   # garante os mesmos volumes

saude() {
  local id status inicio=$SECONDS
  id=$(docker compose ps -q chatbot)
  [[ -n "$id" ]] || return 1
  while (( SECONDS - inicio < TIMEOUT_SAUDE )); do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null || echo ausente)
    case "$status" in
      healthy) return 0 ;;
      unhealthy|ausente) return 1 ;;
    esac
    sleep 5
  done
  return 1
}

subir() { docker compose up -d --build --remove-orphans; }

ANTERIOR=$(git rev-parse HEAD)
git fetch --quiet origin "$BRANCH"
NOVO=$(git rev-parse "${ALVO:-origin/$BRANCH}^{commit}")
if ! git merge-base --is-ancestor "$NOVO" "origin/$BRANCH"; then
  log "o commit $NOVO não pertence a origin/$BRANCH"; exit 2
fi
log "deploy: $(git rev-parse --short "$ANTERIOR") -> $(git rev-parse --short "$NOVO")"

# Backup dos volumes de dados.
CARIMBO=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR/$CARIMBO"
for v in "${VOLUMES[@]}"; do
  origem=$(docker volume inspect -f '{{.Mountpoint}}' "chatbot-universo_$v")
  tar czf "$BACKUP_DIR/$CARIMBO/$v.tar.gz" -C "$origem" .
done
echo "$ANTERIOR" > "$BACKUP_DIR/$CARIMBO/commit-anterior"
log "backup dos volumes em $BACKUP_DIR/$CARIMBO"
ls -1d "$BACKUP_DIR"/*/ | sort | head -n -"$MANTER_BACKUPS" | xargs -r rm -rf

git reset --quiet --hard "$NOVO"
if subir && saude; then
  log "deploy concluído: $(git log -1 --format='%h %s')"
  docker image prune -f >/dev/null || true
  exit 0
fi

log "a nova versão não ficou saudável; revertendo para $(git rev-parse --short "$ANTERIOR")"
docker compose logs --tail 60 chatbot || true
git reset --quiet --hard "$ANTERIOR"
if subir && saude; then
  log "rollback concluído; a versão anterior está no ar"
else
  log "ATENÇÃO: o rollback também falhou. Backup dos dados em $BACKUP_DIR/$CARIMBO"
fi
exit 1
