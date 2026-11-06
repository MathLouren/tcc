"""API REST e interface web do Chatbot Institucional UNIVERSO (seção 3.7 do TCC).

Execução:
    uvicorn app.main:criar_app --factory --host 0.0.0.0 --port 8000

Rotas (Quadro 3 do TCC):
    POST /chat              pergunta do aluno -> resposta, fontes e tempo de processamento
    POST /feedback          avaliação útil / não útil de uma resposta
    GET  /health            estado da aplicação e data da última indexação
    POST /admin/reindex     reindexa a base de conhecimento
    GET  /admin/logs        registros de interações
    GET  /admin/config      parâmetros atuais
    PUT  /admin/config      ajusta k, limiar, temperatura e prompt
As rotas administrativas aceitam sessão de login ou X-Admin-Key para scripts.
A documentação interativa fica em /docs.
"""
import logging
import json
import re
import secrets
import uuid
from datetime import date
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config
from .admin_auth import AdminAuth, COOKIE_NAME, SESSION_SECONDS
from .servico import Servico

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
EXTENSOES_UPLOAD = {".pdf", ".html", ".htm", ".md", ".txt"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


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
    contexto_max_blocos: int | None = Field(default=None, ge=4, le=128)
    multiplicador_calendario: int | None = Field(default=None, ge=1, le=10)
    prompt_sistema: str | None = Field(default=None, min_length=20, max_length=4000)


class CrawlerIn(BaseModel):
    aplicar: bool = False
    max_paginas: int = Field(default=25, ge=1, le=200)


class VectorStoreIn(BaseModel):
    vector_store: str = Field(..., pattern="^(local|qdrant)$")


class LoginIn(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    senha: str = Field(..., min_length=1, max_length=1024)


def _nome_seguro(nome: str) -> str:
    """Gera um nome de arquivo previsível sem permitir caminhos ou caracteres perigosos."""
    base = Path(nome).stem.strip().lower()
    base = re.sub(r"[^a-z0-9áéíóúâêôãõçü]+", "_", base, flags=re.IGNORECASE).strip("_")
    return base or "documento"


def _proximo_caminho(diretorio: Path, nome_original: str) -> Path:
    extensao = Path(nome_original).suffix.lower()
    base = _nome_seguro(nome_original)
    destino = diretorio / f"{base}{extensao}"
    contador = 2
    while destino.exists():
        destino = diretorio / f"{base}_{contador}{extensao}"
        contador += 1
    return destino


def _atualizar_manifesto(caminho: Path, entrada: dict) -> None:
    if caminho.exists():
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    else:
        dados = {"documentos": []}
    if not isinstance(dados, dict) or not isinstance(dados.get("documentos"), list):
        raise ValueError("O manifesto não possui uma lista de documentos válida.")
    dados["documentos"].append(entrada)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporario.replace(caminho)


def criar_app(config: Config | None = None, embedder=None, llm=None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    servico = Servico(config, embedder=embedder, llm=llm)

    app = FastAPI(
        title="Chatbot Institucional UNIVERSO",
        version="1.0.0",
        description="Sistema de resposta a perguntas baseado em RAG para atendimento aos alunos.",
    )
    app.state.servico = servico
    admin_auth = AdminAuth(servico.config.admin_email, servico.config.admin_password)

    def exigir_sessao(request: Request) -> dict:
        if not admin_auth.habilitado:
            raise HTTPException(503, "Configure ADMIN_EMAIL e ADMIN_PASSWORD no .env.")
        sessao = admin_auth.verificar_sessao(request.cookies.get(COOKIE_NAME))
        if sessao is None:
            raise HTTPException(401, "Faça login para acessar a área administrativa.")
        return sessao

    def exigir_admin(
        request: Request,
        x_admin_key: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> None:
        chave = servico.config.admin_key
        if not chave and not admin_auth.habilitado:
            raise HTTPException(503, "Configure ADMIN_EMAIL e ADMIN_PASSWORD ou ADMIN_KEY no .env.")
        if x_admin_key is not None:
            if chave and secrets.compare_digest(x_admin_key.encode(), chave.encode()):
                return
            raise HTTPException(401, "Chave administrativa inválida.")
        if not admin_auth.habilitado:
            raise HTTPException(401, "Chave administrativa ausente.")
        sessao = exigir_sessao(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not x_csrf_token or not secrets.compare_digest(x_csrf_token, sessao["csrf"]):
                raise HTTPException(403, "Token de segurança inválido. Atualize a página e tente novamente.")

    @app.post("/admin/auth/login")
    def login(dados: LoginIn, request: Request):
        if not admin_auth.habilitado:
            raise HTTPException(503, "Configure ADMIN_EMAIL e ADMIN_PASSWORD no .env.")
        endereco = request.client.host if request.client else "desconhecido"
        if not admin_auth.pode_tentar(endereco):
            raise HTTPException(429, "Muitas tentativas de login. Tente novamente em alguns minutos.")
        if not admin_auth.autenticar(dados.email, dados.senha):
            admin_auth.registrar_falha(endereco)
            raise HTTPException(401, "E-mail ou senha inválidos.")
        token, csrf = admin_auth.criar_sessao()
        resposta = JSONResponse({"email": admin_auth.email, "csrf_token": csrf}, headers={"Cache-Control": "no-store"})
        resposta.set_cookie(
            COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
            secure=request.url.scheme == "https", samesite="strict", path="/admin",
        )
        return resposta

    @app.get("/admin/auth/session")
    def sessao_atual(sessao: dict = Depends(exigir_sessao)):
        return JSONResponse(
            {"email": admin_auth.email, "csrf_token": sessao["csrf"]},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/admin/auth/logout", dependencies=[Depends(exigir_admin)])
    def logout():
        resposta = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        resposta.delete_cookie(COOKIE_NAME, path="/admin", samesite="strict")
        return resposta

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
            "busca": getattr(servico.indice, "tipo_busca", "faiss" if servico.indice.usa_faiss else "numpy"),
            "vector_store": servico.config.vector_store,
            "ultima_indexacao": info.get("data_indexacao"),
            "n_documentos": info.get("n_documentos", 0),
            "n_blocos": info.get("n_blocos", 0),
            "documentos": [
                {
                    "arquivo": d["arquivo"],
                    "titulo": d["titulo"],
                    "atualizado_em": d.get("atualizado_em", ""),
                    "url": d.get("url", ""),
                    "categoria": d.get("categoria", ""),
                }
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

    @app.post("/admin/documentos/{nome_arquivo}/reindexar", dependencies=[Depends(exigir_admin)])
    def reindexar_documento(nome_arquivo: str):
        """Reindexa a base dando ao administrador uma ação específica para o documento."""
        nome = Path(nome_arquivo).name
        if nome != nome_arquivo or Path(nome).suffix.lower() not in EXTENSOES_UPLOAD:
            raise HTTPException(400, "Nome ou formato de documento inválido.")
        caminho = servico.config.docs_dir / nome
        if not caminho.is_file():
            raise HTTPException(404, "Documento não encontrado.")
        try:
            info = servico.reindexar_documento(nome)
        except Exception as erro:
            raise HTTPException(500, f"Não foi possível reindexar o documento: {erro}") from erro
        indexados = {documento["arquivo"] for documento in info.get("documentos", [])}
        if nome not in indexados:
            raise HTTPException(422, "O arquivo existe, mas não gerou conteúdo indexável.")
        return {
            "ok": True,
            "arquivo": nome,
            "n_documentos": info["n_documentos"],
            "n_blocos": info["n_blocos"],
            "data_indexacao": info["data_indexacao"],
        }

    @app.get("/admin/logs", dependencies=[Depends(exigir_admin)])
    def logs(limite: int = Query(default=100, ge=1, le=5000)):
        return servico.registro.resumo(limite)

    @app.get("/admin/config", dependencies=[Depends(exigir_admin)])
    def obter_config():
        return JSONResponse(asdict(servico.parametros), headers={"Cache-Control": "no-store"})

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

    @app.put("/admin/vector-store", dependencies=[Depends(exigir_admin)])
    def alterar_vector_store(dados: VectorStoreIn):
        try:
            parametros, info = servico.alterar_vector_store(dados.vector_store)
        except ValueError as erro:
            raise HTTPException(422, str(erro)) from erro
        except RuntimeError as erro:
            raise HTTPException(503, str(erro)) from erro
        return {
            "ok": True,
            "vector_store": servico.config.vector_store,
            "busca": servico.indice.tipo_busca,
            "n_documentos": info["n_documentos"],
            "n_blocos": info["n_blocos"],
            "data_indexacao": info["data_indexacao"],
            "parametros": asdict(parametros),
        }

    @app.post("/admin/upload", dependencies=[Depends(exigir_admin)])
    async def upload_documento(
        arquivo: UploadFile = File(...),
        titulo: str | None = Form(default=None),
        origem: str | None = Form(default=None),
        url: str | None = Form(default=None),
        atualizado_em: str | None = Form(default=None),
        categoria: str | None = Form(default=None),
    ):
        """Recebe um documento, atualiza o manifesto e reindexa a base."""
        nome_original = Path(arquivo.filename or "").name
        extensao = Path(nome_original).suffix.lower()
        if not nome_original or extensao not in EXTENSOES_UPLOAD:
            raise HTTPException(415, "Formato não suportado. Use PDF, HTML, MD ou TXT.")

        destino = _proximo_caminho(servico.config.docs_dir, nome_original)
        servico.config.docs_dir.mkdir(parents=True, exist_ok=True)
        tamanho = 0
        try:
            with destino.open("wb") as saida:
                while bloco := await arquivo.read(1024 * 1024):
                    tamanho += len(bloco)
                    if tamanho > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "O arquivo excede o limite de 25 MB.")
                    saida.write(bloco)

            if tamanho == 0:
                raise HTTPException(422, "O arquivo está vazio.")

            entrada = {
                "arquivo": destino.name,
                "titulo": (titulo or "").strip() or Path(nome_original).stem.replace("_", " "),
                "origem": (origem or "").strip() or "Enviado pela interface administrativa",
                "url": (url or "").strip(),
                "atualizado_em": (atualizado_em or "").strip() or date.today().isoformat(),
                "categoria": (categoria or "").strip() or "geral",
            }
            _atualizar_manifesto(servico.config.manifesto, entrada)
            info = servico.reindexar_documento(destino.name)
        except HTTPException:
            destino.unlink(missing_ok=True)
            raise
        except (OSError, ValueError, json.JSONDecodeError) as erro:
            destino.unlink(missing_ok=True)
            raise HTTPException(500, f"Não foi possível cadastrar o documento: {erro}") from erro
        except Exception as erro:
            raise HTTPException(500, f"O arquivo foi salvo, mas a reindexação falhou: {erro}") from erro
        finally:
            await arquivo.close()

        return {
            "ok": True,
            "arquivo": entrada["arquivo"],
            "titulo": entrada["titulo"],
            "tamanho_bytes": tamanho,
            "data_indexacao": info["data_indexacao"],
            "n_documentos": info["n_documentos"],
            "n_blocos": info["n_blocos"],
        }

    @app.delete("/admin/documentos/{nome_arquivo}", dependencies=[Depends(exigir_admin)])
    def remover_documento(nome_arquivo: str):
        """Remove o arquivo, sua entrada no manifesto e reconstrói o índice."""
        nome = Path(nome_arquivo).name
        if nome != nome_arquivo or Path(nome).suffix.lower() not in EXTENSOES_UPLOAD:
            raise HTTPException(400, "Nome ou formato de documento inválido.")

        caminho = servico.config.docs_dir / nome
        if not caminho.is_file():
            raise HTTPException(404, "Documento não encontrado.")

        manifesto = {}
        if servico.config.manifesto.exists():
            try:
                manifesto = json.loads(servico.config.manifesto.read_text(encoding="utf-8"))
            except json.JSONDecodeError as erro:
                raise HTTPException(500, "O manifesto está inválido; a remoção foi cancelada.") from erro
        documentos = manifesto.get("documentos", []) if isinstance(manifesto, dict) else []
        if not isinstance(documentos, list):
            raise HTTPException(500, "O manifesto não possui uma lista de documentos válida; a remoção foi cancelada.")
        documentos_restantes = [
            item for item in documentos
            if not isinstance(item, dict) or item.get("arquivo") != nome
        ]

        if servico.config.vector_store == "qdrant":
            try:
                servico.remover_documento_qdrant(nome)
            except Exception as erro:
                raise HTTPException(500, f"Não foi possível remover o índice do documento: {erro}") from erro
        caminho.unlink()
        if isinstance(manifesto, dict) and len(documentos_restantes) != len(documentos):
            manifesto["documentos"] = documentos_restantes
            temporario = servico.config.manifesto.with_suffix(servico.config.manifesto.suffix + ".tmp")
            temporario.write_text(
                json.dumps(manifesto, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporario.replace(servico.config.manifesto)

        try:
            info = servico.reindexar() if servico.config.vector_store != "qdrant" else servico.info
        except Exception as erro:
            raise HTTPException(500, f"Documento removido, mas a reindexação falhou: {erro}") from erro

        return {
            "ok": True,
            "arquivo": nome,
            "n_documentos": info["n_documentos"],
            "n_blocos": info["n_blocos"],
            "data_indexacao": info["data_indexacao"],
        }


    @app.get("/admin/crawler/status", dependencies=[Depends(exigir_admin)])
    def estado_crawler():
        from .estado_crawler import carregar_estado
        caminho = servico.config.manifesto.parent / "crawler_state.json"
        estado = carregar_estado(caminho)
        return {"documentos": len(estado["documentos"]), "urls": list(estado["documentos"].keys())}

    @app.post("/admin/crawler/sincronizar", dependencies=[Depends(exigir_admin)])
    def sincronizar_crawler(dados: CrawlerIn):
        if servico.config.vector_store != "qdrant":
            raise HTTPException(422, "Crawler requer VECTOR_STORE=qdrant")
        try:
            return servico.sincronizar_crawler(aplicar=dados.aplicar, max_paginas=dados.max_paginas)
        except (ValueError, RuntimeError) as erro:
            raise HTTPException(422, str(erro)) from erro

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def pagina_admin():
        return FileResponse(WEB_DIR / "admin.html")

    # Interface web: montada por último para não encobrir as rotas da API.
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app
