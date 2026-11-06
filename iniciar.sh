#!/usr/bin/env sh
# Prepara o ambiente (na primeira vez) e inicia o chatbot em http://localhost:8000
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  echo "Criando ambiente virtual..."
  "$PYTHON" -m venv .venv
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Arquivo .env criado. Ajuste OLLAMA_URL, ADMIN_EMAIL e ADMIN_PASSWORD nele se necessario."
fi

if [ ! -f .venv/.instalado ] \
  || ! .venv/bin/python -c 'import multipart' >/dev/null 2>&1 \
  || ! .venv/bin/python -c 'from app.config import carregar_config; import importlib.util; c=carregar_config(); raise SystemExit(0 if c.vector_store != "qdrant" or importlib.util.find_spec("qdrant_client") else 1)' >/dev/null 2>&1; then
  echo "Instalando dependências. Na primeira vez isso pode levar alguns minutos..."
  .venv/bin/python -m pip install --upgrade pip
  # PyTorch somente para CPU (evita baixar as bibliotecas CUDA no Linux)
  .venv/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
  .venv/bin/python -m pip install -r requirements.txt
  if ! .venv/bin/python -c 'from app.config import carregar_config; raise SystemExit(0 if carregar_config().vector_store != "qdrant" else 1)' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements-qdrant.txt
  fi
  touch .venv/.instalado
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Arquivo .env criado. Ajuste OLLAMA_URL, ADMIN_EMAIL e ADMIN_PASSWORD nele se necessário."
fi

echo "Iniciando o chatbot em http://localhost:${PORTA:-8000} (Ctrl+C para encerrar)"
exec .venv/bin/python -m uvicorn app.main:criar_app --factory --host 0.0.0.0 --port "${PORTA:-8000}"
