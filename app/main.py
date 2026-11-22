"""API REST e interface web do Chatbot Institucional UNIVERSO (seção 3.7 do TCC).

Execução:
    uvicorn app.main:criar_app --factory --host 0.0.0.0 --port 8000

Rotas (Quadro 3 do TCC):
    POST /chat              pergunta do aluno -> resposta, fontes e tempo de processamento
    POST /chat/stream       o mesmo, com o texto enviado enquanto o modelo escreve (NDJSON)
    POST /feedback          avaliação útil / não útil de uma resposta
    GET  /health            estado da aplicação e data da última indexação
    GET  /admin             painel administrativo (login com EMAIL e PASS do .env)
    GET/POST/DELETE /admin/documentos, GET /admin/logs, ...  exigem sessão do painel ou X-Admin-Key
    POST /admin/reindex     reindexa a base de conhecimento          (cabeçalho X-Admin-Key)
    POST /admin/documentos  converte PDF com Docling e reindexa       (cabeçalho X-Admin-Key)
    GET  /admin/logs        registros de interações                  (cabeçalho X-Admin-Key)
    GET  /admin/config      parâmetros atuais                        (cabeçalho X-Admin-Key)
    PUT  /admin/config      ajusta k, limiar, temperatura e prompt   (cabeçalho X-Admin-Key)
A documentação interativa fica em /docs.
"""
import json
import logging
import queue
import re
import secrets
import threading
import time
import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Config
from .dashboard import montar_dashboard
from .docling_pdf import ErroConversaoPDF
from .servico import Servico

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


SESSAO_ADMIN_S = 8 * 3600
COOKIE_ADMIN = "admin_sessao"
MAX_FALHAS_LOGIN = 5
JANELA_FALHAS_S = 300


class WebEstatica(StaticFiles):
    """Faz o navegador revalidar os arquivos, para não usar uma interface antiga com a API nova."""

    async def get_response(self, path, scope):
        resposta = await super().get_response(path, scope)
        resposta.headers["Cache-Control"] = "no-cache"
        return resposta


class LoginIn(BaseModel):
    email: str = Field(..., min_length=1, max_length=200)
    senha: str = Field(..., min_length=1, max_length=200)


class PerguntaIn(BaseModel):
    nome: str = Field(..., min_length=1, max_length=80)
    pergunta: str = Field(..., min_length=1, max_length=1000)
    sessao_id: str | None = Field(default=None, max_length=64)


class AvaliacaoIn(BaseModel):
    interacao_id: str = Field(..., min_length=1, max_length=64)
    util: bool
    comentario: str | None = Field(default=None, max_length=500)


class BuscaIn(BaseModel):
    pergunta: str = Field(..., min_length=1, max_length=1000)


class RevisaoIn(BaseModel):
    interacao_id: str = Field(..., min_length=1, max_length=64)
    veredito: str = Field(..., pattern="^(correta|parcial|incorreta)$")
    revisor: str | None = Field(default=None, max_length=80)
    comentario: str | None = Field(default=None, max_length=1000)


class ParametrosIn(BaseModel):
    top_k: int | None = Field(default=None, ge=1, le=20)
    limiar: float | None = Field(default=None, ge=0.0, le=1.0)
    temperatura: float | None = Field(default=None, ge=0.0, le=2.0)
    historico_trocas: int | None = Field(default=None, ge=0, le=10)
    prompt_sistema: str | None = Field(default=None, min_length=20, max_length=4000)
    peso_lexical: float | None = Field(default=None, ge=0.0, le=1.0)
    margem_relativa: float | None = Field(default=None, ge=0.0, le=1.0)


def criar_app(config: Config | None = None, embedder=None, llm=None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    servico = Servico(config, embedder=embedder, llm=llm)
    if llm is None:  # cliente real: carrega o modelo já na subida (nos testes, o LLM falso é injetado)
        servico.aquecer_llm_em_segundo_plano()
        servico.aquecer_cache_em_segundo_plano()

    app = FastAPI(
        title="Chatbot Institucional UNIVERSO",
        version="1.0.0",
        description="Sistema de resposta a perguntas baseado em RAG para atendimento aos alunos.",
    )
    app.state.servico = servico

    sessoes_admin: dict[str, float] = {}
    falhas_login: dict[str, list[float]] = {}

    def _igual(a: str, b: str) -> bool:
        return secrets.compare_digest(a.encode(), b.encode())

    def exigir_admin(
        x_admin_key: str | None = Header(default=None),
        admin_sessao: str | None = Cookie(default=None),
    ) -> None:
        cfg = servico.config
        login_ok = bool(cfg.admin_email and cfg.admin_pass)
        if not login_ok and not cfg.admin_key:
            raise HTTPException(503, "Acesso administrativo não configurado (defina EMAIL e PASS no .env).")
        if admin_sessao:
            validade = sessoes_admin.get(admin_sessao)
            if validade and validade > time.time():
                return
            sessoes_admin.pop(admin_sessao, None)
        if cfg.admin_key and x_admin_key and _igual(x_admin_key, cfg.admin_key):
            return
        raise HTTPException(401, "Autenticação administrativa necessária.")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def pagina_admin():
        return FileResponse(WEB_DIR / "admin.html", headers={"Cache-Control": "no-store"})

    @app.post("/admin/login")
    def login(dados: LoginIn, request: Request, resposta: Response):
        cfg = servico.config
        if not (cfg.admin_email and cfg.admin_pass):
            raise HTTPException(503, "Login administrativo não configurado (defina EMAIL e PASS no .env).")
        origem = request.client.host if request.client else "?"
        agora = time.time()
        recentes = [t for t in falhas_login.get(origem, []) if agora - t < JANELA_FALHAS_S]
        falhas_login[origem] = recentes
        if len(recentes) >= MAX_FALHAS_LOGIN:
            raise HTTPException(429, "Muitas tentativas. Aguarde alguns minutos.")
        email_ok = _igual(dados.email.strip().lower(), cfg.admin_email.lower())
        senha_ok = _igual(dados.senha, cfg.admin_pass)
        if not (email_ok and senha_ok):
            recentes.append(agora)
            raise HTTPException(401, "E-mail ou senha inválidos.")
        falhas_login.pop(origem, None)
        for token, validade in list(sessoes_admin.items()):
            if validade <= agora:
                del sessoes_admin[token]
        token = secrets.token_urlsafe(32)
        sessoes_admin[token] = agora + SESSAO_ADMIN_S
        resposta.set_cookie(
            COOKIE_ADMIN, token, max_age=SESSAO_ADMIN_S, httponly=True, samesite="strict", path="/"
        )
        return {"ok": True}

    @app.post("/admin/logout")
    def logout(resposta: Response, admin_sessao: str | None = Cookie(default=None)):
        if admin_sessao:
            sessoes_admin.pop(admin_sessao, None)
        resposta.delete_cookie(COOKIE_ADMIN, path="/")
        return {"ok": True}

    @app.get("/admin/sessao", dependencies=[Depends(exigir_admin)])
    def sessao_admin():
        return {"ok": True, "email": servico.config.admin_email}

    def _ler_pergunta(dados: PerguntaIn) -> tuple[str, str, str]:
        pergunta = dados.pergunta.strip()
        nome = " ".join(dados.nome.split())
        if not pergunta:
            raise HTTPException(422, "A pergunta não pode ser vazia.")
        if not nome:
            raise HTTPException(422, "Informe seu nome para conversar.")
        return pergunta, nome, dados.sessao_id or uuid.uuid4().hex

    def _responder_e_registrar(pergunta: str, nome: str, sessao_id: str, ao_token=None) -> dict:
        resposta = servico.motor.responder(pergunta, sessao_id, ao_token=ao_token)
        interacao_id = servico.registro.registrar_interacao(sessao_id, pergunta, resposta, nome=nome)
        return {
            "id": interacao_id,
            "sessao_id": sessao_id,
            "resposta": resposta.resposta,
            "fontes": resposta.fontes,
            "modo": resposta.modo,
            "tempo_ms": resposta.tempo_ms,
        }

    @app.post("/chat")
    def chat(dados: PerguntaIn):
        return _responder_e_registrar(*_ler_pergunta(dados))

    @app.post("/chat/stream")
    def chat_stream(dados: PerguntaIn):
        """Como /chat, mas envia a resposta enquanto o modelo escreve (uma linha JSON por evento).

        Eventos: {"tipo": "parcial", "texto": ...} com cada pedaço do texto; depois um único
        {"tipo": "final", ...} com os mesmos campos de /chat, que substitui o texto parcial;
        ou {"tipo": "erro", "mensagem": ...}.
        """
        pergunta, nome, sessao_id = _ler_pergunta(dados)
        fila: queue.Queue = queue.Queue()

        def trabalhar() -> None:
            try:
                final = _responder_e_registrar(
                    pergunta, nome, sessao_id, ao_token=lambda texto: fila.put({"tipo": "parcial", "texto": texto})
                )
                fila.put({"tipo": "final", **final})
            except Exception:  # noqa: BLE001 - o erro vai para o log; o aluno recebe uma mensagem genérica
                logging.getLogger(__name__).exception("Falha ao responder em stream")
                fila.put({"tipo": "erro", "mensagem": "O servidor não conseguiu processar a pergunta."})
            finally:
                fila.put(None)

        threading.Thread(target=trabalhar, name="chat-stream", daemon=True).start()

        def eventos():
            while (evento := fila.get()) is not None:
                yield json.dumps(evento, ensure_ascii=False) + "\n"

        return StreamingResponse(eventos(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/feedback")
    def feedback(dados: AvaliacaoIn):
        if not servico.registro.registrar_avaliacao(dados.interacao_id, dados.util, dados.comentario):
            raise HTTPException(404, "Interação não encontrada.")
        if not dados.util:
            # Resposta marcada como não útil sai do cache: a próxima pergunta igual é gerada de novo.
            pergunta = servico.registro.pergunta_de(dados.interacao_id)
            if pergunta:
                servico.motor.esquecer_resposta(pergunta)
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

    @app.get("/admin/documentos", dependencies=[Depends(exigir_admin)])
    def listar_documentos():
        return {"documentos": servico.listar_documentos()}

    @app.get("/admin/documentos/{arquivo}/conteudo", dependencies=[Depends(exigir_admin)])
    def conteudo_documento(arquivo: str):
        try:
            return servico.conteudo_do_documento(arquivo)
        except FileNotFoundError as erro:
            raise HTTPException(404, str(erro)) from erro

    @app.get("/admin/documentos/{arquivo}/baixar", dependencies=[Depends(exigir_admin)])
    def baixar_documento(arquivo: str, formato: str = Query(default="markdown", pattern="^(markdown|json|blocos|pdf)$")):
        try:
            caminho = servico.arquivo_para_download(arquivo, formato)
        except FileNotFoundError as erro:
            raise HTTPException(404, str(erro)) from erro
        return FileResponse(caminho, filename=caminho.name, headers={"Cache-Control": "no-store"})

    @app.post("/admin/busca", dependencies=[Depends(exigir_admin)])
    def testar_busca(dados: BuscaIn):
        pergunta = dados.pergunta.strip()
        if not pergunta:
            raise HTTPException(422, "A pergunta não pode ser vazia.")
        return servico.testar_busca(pergunta)

    @app.delete("/admin/documentos/{arquivo}", dependencies=[Depends(exigir_admin)])
    def remover_documento(arquivo: str):
        try:
            resultado = servico.remover_documento(arquivo)
        except FileNotFoundError as erro:
            raise HTTPException(404, str(erro)) from erro
        return {"ok": True, **resultado}

    @app.post("/admin/documentos", dependencies=[Depends(exigir_admin)])
    def enviar_documento(
        arquivo: UploadFile = File(...),
        titulo: str = Form(default="", max_length=200),
        url: str = Form(default="", max_length=1000),
        categoria: str = Form(default="documento", max_length=80),
        atualizado_em: str = Form(default="", max_length=10),
    ):
        nome = Path(arquivo.filename or "").name
        if Path(nome).suffix.lower() != ".pdf":
            raise HTTPException(422, "Envie um arquivo com extensão .pdf.")
        if url and not re.match(r"^https?://", url, re.IGNORECASE):
            raise HTTPException(422, "A URL da fonte deve começar com http:// ou https://.")
        if atualizado_em and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", atualizado_em):
            raise HTTPException(422, "A data de atualização deve usar o formato AAAA-MM-DD.")

        limite = servico.config.pdf_upload_max_mb * 1024 * 1024
        caminho_temporario: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temporario:
                caminho_temporario = Path(temporario.name)
                total = 0
                cabecalho = b""
                while bloco := arquivo.file.read(1024 * 1024):
                    if not cabecalho:
                        cabecalho = bloco[:5]
                    total += len(bloco)
                    if total > limite:
                        raise HTTPException(
                            413,
                            f"O PDF excede o limite de {servico.config.pdf_upload_max_mb} MB.",
                        )
                    temporario.write(bloco)
            if cabecalho != b"%PDF-":
                raise HTTPException(422, "O conteúdo enviado não possui uma assinatura PDF válida.")
            resultado = servico.adicionar_pdf(
                caminho_temporario,
                nome,
                titulo=titulo,
                url=url,
                categoria=categoria,
                atualizado_em=atualizado_em,
            )
        except FileExistsError as erro:
            raise HTTPException(409, str(erro)) from erro
        except ErroConversaoPDF as erro:
            raise HTTPException(422, str(erro)) from erro
        finally:
            arquivo.file.close()
            if caminho_temporario:
                caminho_temporario.unlink(missing_ok=True)
        return {"ok": True, "extrator": "docling", **resultado}

    @app.get("/admin/logs", dependencies=[Depends(exigir_admin)])
    def logs(limite: int = Query(default=100, ge=1, le=5000)):
        return servico.registro.resumo(limite)

    @app.get("/admin/dashboard", dependencies=[Depends(exigir_admin)])
    def dashboard(periodo: str = Query(default="30d", pattern="^(hoje|7d|30d|90d|tudo)$")):
        interacoes, avaliacoes, revisoes = servico.registro.dados()
        dados = montar_dashboard(interacoes, avaliacoes, revisoes, periodo)
        dados["modelo"] = {
            "api": servico.config.llm_api,
            "nome": servico.config.llm_modelo,
            "disponivel": servico.llm.disponivel(),
        }
        return dados

    @app.post("/admin/revisoes", dependencies=[Depends(exigir_admin)])
    def validar_resposta(dados: RevisaoIn):
        """Validação humana de uma resposta (secretaria ou coordenação): correta, parcial ou incorreta."""
        revisor = " ".join((dados.revisor or "").split()) or servico.config.admin_email or "Administrador"
        revisao = servico.registro.registrar_revisao(dados.interacao_id, dados.veredito, revisor, dados.comentario)
        if revisao is None:
            raise HTTPException(404, "Interação não encontrada.")
        return {"ok": True, "revisao": revisao}

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
    app.mount("/", WebEstatica(directory=str(WEB_DIR), html=True), name="web")
    return app
