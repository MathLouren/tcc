"""API REST e interface web do Chatbot Institucional UNIVERSO (seção 3.7 do TCC).

Execução:
    uvicorn app.main:criar_app --factory --host 0.0.0.0 --port 8000

Rotas (Quadro 3 do TCC):
    POST /chat              pergunta do aluno -> resposta, fontes e tempo de processamento
    POST /feedback          avaliação útil / não útil de uma resposta
    GET  /health            estado da aplicação e data da última indexação
    POST /admin/reindex     reindexa a base de conhecimento          (cabeçalho X-Admin-Key)
    GET  /admin/logs        registros de interações                  (cabeçalho X-Admin-Key)
    GET  /admin/config      parâmetros atuais                        (cabeçalho X-Admin-Key)
    PUT  /admin/config      ajusta k, limiar, temperatura e prompt   (cabeçalho X-Admin-Key)
A documentação interativa fica em /docs.
"""
import logging
import secrets
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config
from .servico import Servico

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class PerguntaIn(BaseModel):
    pergunta: str = Field(..., min_length=1, max_length=1000)
    sessao_id: str | None = Field(default=None, max_length=64)


class AvaliacaoIn(BaseModel):
    interacao_id: str = Field(..., min_length=1, max_length=64)
    util: bool
    comentario: str | None = Field(default=None, max_length=500)


class ParametrosIn(BaseModel):
    top_k: int | None = Field(default=None, ge=1, le=20)
    limiar: float | None = Field(default=None, ge=0.0, le=1.0)
    temperatura: float | None = Field(default=None, ge=0.0, le=2.0)
    historico_trocas: int | None = Field(default=None, ge=0, le=10)
    prompt_sistema: str | None = Field(default=None, min_length=20, max_length=4000)


def criar_app(config: Config | None = None, embedder=None, llm=None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    servico = Servico(config, embedder=embedder, llm=llm)

    app = FastAPI(
        title="Chatbot Institucional UNIVERSO",
        version="1.0.0",
        description="Sistema de resposta a perguntas baseado em RAG para atendimento aos alunos.",
    )
    app.state.servico = servico

    def exigir_admin(x_admin_key: str | None = Header(default=None)) -> None:
        chave = servico.config.admin_key
        if not chave:
            raise HTTPException(503, "ADMIN_KEY não configurada no servidor (arquivo .env).")
        if not x_admin_key or not secrets.compare_digest(x_admin_key.encode(), chave.encode()):
            raise HTTPException(401, "Chave administrativa inválida.")

    @app.post("/chat")
    def chat(dados: PerguntaIn):
        pergunta = dados.pergunta.strip()
        if not pergunta:
            raise HTTPException(422, "A pergunta não pode ser vazia.")
        sessao_id = dados.sessao_id or uuid.uuid4().hex
        resposta = servico.motor.responder(pergunta, sessao_id)
        interacao_id = servico.registro.registrar_interacao(sessao_id, pergunta, resposta)
        return {
            "id": interacao_id,
            "sessao_id": sessao_id,
            "resposta": resposta.resposta,
            "fontes": resposta.fontes,
            "modo": resposta.modo,
            "tempo_ms": resposta.tempo_ms,
        }

    @app.post("/feedback")
    def feedback(dados: AvaliacaoIn):
        if not servico.registro.registrar_avaliacao(dados.interacao_id, dados.util, dados.comentario):
            raise HTTPException(404, "Interação não encontrada.")
        return {"ok": True}

    @app.get("/health")
    def health():
        info = servico.info
        return {
            "status": "ok",
            "llm": {
                "api": servico.config.llm_api,
                "modelo": servico.config.llm_modelo,
                "disponivel": servico.llm.disponivel(),
            },
            "embeddings": {
                "provedor": servico.config.embedding_provedor,
                "modelo": servico.config.embedding_modelo,
            },
            "busca": "faiss" if servico.indice.usa_faiss else "numpy",
            "ultima_indexacao": info.get("data_indexacao"),
            "n_documentos": info.get("n_documentos", 0),
            "n_blocos": info.get("n_blocos", 0),
            "documentos": [
                {"titulo": d["titulo"], "atualizado_em": d.get("atualizado_em", ""), "url": d.get("url", "")}
                for d in info.get("documentos", [])
            ],
        }

    @app.post("/admin/reindex", dependencies=[Depends(exigir_admin)])
    def reindex():
        info = servico.reindexar()
        return {
            "ok": True,
            "data_indexacao": info["data_indexacao"],
            "n_documentos": info["n_documentos"],
            "n_blocos": info["n_blocos"],
            "tempo_indexacao_s": info["tempo_indexacao_s"],
        }

    @app.get("/admin/logs", dependencies=[Depends(exigir_admin)])
    def logs(limite: int = Query(default=100, ge=1, le=5000)):
        return servico.registro.resumo(limite)

    @app.get("/admin/config", dependencies=[Depends(exigir_admin)])
    def obter_config():
        return asdict(servico.parametros)

    @app.put("/admin/config", dependencies=[Depends(exigir_admin)])
    def alterar_config(dados: ParametrosIn):
        alteracoes = {k: v for k, v in dados.model_dump().items() if v is not None}
        if not alteracoes:
            raise HTTPException(422, "Nenhum parâmetro informado.")
        try:
            novos = servico.atualizar_parametros(alteracoes)
        except ValueError as erro:
            raise HTTPException(422, str(erro)) from erro
        return asdict(novos)

    # Interface web: montada por último para não encobrir as rotas da API.
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app
