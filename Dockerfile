FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/.cache/huggingface \
    OLLAMA_URL=http://host.docker.internal:11434

COPY requirements.txt .
RUN pip install --no-cache-dir torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY web ./web
COPY data ./data

# Dados que precisam sobreviver à troca do contêiner:
#   data       documentos, PDFs enviados pelo painel, manifesto e parâmetros do administrador
#   indice     índice vetorial (refeito a partir de data, mas a reindexação demora)
#   registros  perguntas dos alunos e avaliações das respostas
#   .cache     modelo de embeddings baixado na primeira subida
VOLUME ["/app/data", "/app/indice", "/app/registros", "/app/.cache"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=4)"
CMD ["uvicorn", "app.main:criar_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
