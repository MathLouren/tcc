#!/usr/bin/env sh
# Prepara o ambiente (na primeira vez) e inicia o chatbot em http://localhost:8000
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if [ ! -x .venv/bin/python ]; then
  echo "Criando ambiente virtual..."
  "$PYTHON" -m venv .venv
fi

if [ ! -f .venv/.instalado ]; then
  echo "Instalando dependências. Na primeira vez isso pode levar alguns minutos..."
  .venv/bin/python -m pip install --upgrade pip
  # PyTorch somente para CPU (evita baixar as bibliotecas CUDA no Linux)
  .venv/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.instalado
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Arquivo .env criado. Ajuste OLLAMA_URL e ADMIN_KEY nele se necessário."
fi

echo "Iniciando o chatbot em http://localhost:${PORTA:-8000} (Ctrl+C para encerrar)"
exec .venv/bin/python -m uvicorn app.main:criar_app --factory --host 0.0.0.0 --port "${PORTA:-8000}"
