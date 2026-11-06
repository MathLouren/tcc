"""Crawler incremental de páginas e PDFs públicos da UNIVERSO São Gonçalo.

Não faz login; respeita robots.txt; limita páginas, bytes e domínios; nunca apaga a
coleção Qdrant. Execução explícita: python -m app.crawler --aplicar
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .config import carregar_config
from .documentos import extrair_secoes, inferir_metadados, metadados_secao
from .embeddings import criar_embedder
from .estado_crawler import carregar_estado, salvar_json_atomico
from .identidade import hash_conteudo, normalizar_url
from .indexacao import criar_cliente_qdrant, dividir_em_blocos, texto_para_embedding
from .qdrant_incremental import preparar_pontos, upsert_incremental, validar_colecao

logger = logging.getLogger(__name__)
INICIO = "https://universo.edu.br/a-universo/estrutura/sao-goncalo/"
HOSTS_PERMITIDOS = frozenset({"universo.edu.br", "www.universo.edu.br", "universo-unificado.prod.universo.edu.br"})
PALAVRAS_RELEVANTES = re.compile(
    r"s[aã]o.gon[cç]alo|calend[aá]rio|mia\b|manual|alun[oa]|cursos?|"
    r"avalia[cç][aã]o|matr[ií]cula|regimento|acad[eê]mic|biblioteca|"
    r"sistemas.de.informa[cç][aã]o|prova|est[aá]gio|tcc", re.IGNORECASE,
)
USER_AGENT = "UNIVERSO-TCC-ResearchCrawler/1.0"
MAX_BYTES = 15 * 1024 * 1024
MAX_REDIRECIONAMENTOS = 3


def url_autorizada(url: str) -> bool:
    try:
        p = urlsplit(normalizar_url(url))
        return p.scheme == "https" and p.hostname in HOSTS_PERMITIDOS and not p.port
    except (ValueError, TypeError):
        return False


def link_relevante(url: str, ancora: str, origem: str) -> bool:
    if not url_autorizada(url):
        return False
    caminho = urlsplit(url).path.lower()
    if caminho.endswith((".zip", ".exe", ".js", ".css", ".xml", ".jpg", ".png", ".webp", ".mp4")):
        return False
    if caminho.endswith(".pdf"):
        return bool(PALAVRAS_RELEVANTES.search(url + " " + ancora)) or "sao-goncalo" in origem
    return bool(PALAVRAS_RELEVANTES.search(url + " " + ancora))


def descobrir_links(html: bytes, origem: str) -> list[str]:
    sopa = BeautifulSoup(html, "html.parser")
    links = []
    vistos = set()
    raiz = sopa.find("main") or sopa.find("article") or sopa.body or sopa
    for a in raiz.find_all("a", href=True):
        try:
            link = normalizar_url(urljoin(origem, a["href"]))
        except ValueError:
            continue
        if link not in vistos and link_relevante(link, a.get_text(" ", strip=True), origem):
            vistos.add(link)
            links.append(link)
    return links


class Coletor:
    def __init__(self, *, cliente=None, pausa: float = 1.5, max_bytes: int = MAX_BYTES):
        self.cliente = cliente or httpx.Client(timeout=httpx.Timeout(25.0), follow_redirects=False,
                                                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf"})
        self.pausa = max(0.0, pausa)
        self.max_bytes = max_bytes
        self.robots = {}
        self.ultimo_acesso = 0.0

    def _pausar(self, atraso: float | None = None):
        espera = max(self.pausa, atraso or 0) - (time.monotonic() - self.ultimo_acesso)
        if espera > 0:
            time.sleep(espera)
        self.ultimo_acesso = time.monotonic()

    def _robots(self, url: str):
        p = urlsplit(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self.robots:
            destino = base + "/robots.txt"
            self._pausar()
            resposta = self.cliente.get(destino)
            if resposta.status_code == 404:
                rp = RobotFileParser()
                rp.parse([])
            elif resposta.status_code == 200 and len(resposta.content) < 1024 * 1024:
                rp = RobotFileParser()
                rp.parse(resposta.text.splitlines())
            else:
                raise RuntimeError(f"Não foi possível validar robots.txt de {base}: HTTP {resposta.status_code}")
            self.robots[base] = rp
        return self.robots[base]

    def baixar(self, url: str, *, etag: str = "", last_modified: str = ""):
        if not url_autorizada(url):
            raise ValueError(f"URL fora da lista de domínios permitidos: {url}")
        atual = url
        for _ in range(MAX_REDIRECIONAMENTOS + 1):
            rp = self._robots(atual)
            if not rp.can_fetch(USER_AGENT, atual):
                raise ValueError(f"robots.txt impede a coleta: {atual}")
            atraso = rp.crawl_delay(USER_AGENT) or rp.crawl_delay("*") or 0
            self._pausar(atraso)
            headers = {}
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified
            with self.cliente.stream("GET", atual, headers=headers) as resposta:
                if resposta.status_code in (301, 302, 303, 307, 308):
                    destino = normalizar_url(urljoin(atual, resposta.headers.get("location", "")))
                    if not url_autorizada(destino):
                        raise ValueError("Redirecionamento para URL não autorizada.")
                    atual = destino
                    # Cabeçalhos condicionais só se aplicam à URL canônica anterior.
                    etag = last_modified = ""
                    continue
                if resposta.status_code == 304:
                    return None, None, atual, dict(resposta.headers)
                resposta.raise_for_status()
                mime = resposta.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                if mime not in ("text/html", "application/xhtml+xml", "application/pdf"):
                    raise ValueError(f"Content-Type não suportado: {mime}")
                buf = bytearray()
                for parte in resposta.iter_bytes():
                    if len(buf) + len(parte) > self.max_bytes:
                        raise ValueError("Conteúdo excedeu o limite permitido.")
                    buf.extend(parte)
                conteudo = bytes(buf)
                if mime == "application/pdf" and not conteudo.startswith(b"%PDF-"):
                    raise ValueError("Assinatura PDF inválida.")
                if mime != "application/pdf" and b"<html" not in conteudo[:4096].lower() and b"<!doctype html" not in conteudo[:4096].lower():
                    raise ValueError("Conteúdo HTML inválido.")
                return conteudo, (".pdf" if mime == "application/pdf" else ".html"), atual, dict(resposta.headers)
        raise ValueError("Número máximo de redirecionamentos excedido.")


def _titulo_html(conteudo: bytes, url: str) -> str:
    sopa = BeautifulSoup(conteudo, "html.parser")
    principal = sopa.find("h1") or sopa.find("title")
    return principal.get_text(" ", strip=True)[:200] if principal else urlsplit(url).path.rsplit("/", 1)[-1]


def _data_modificacao(headers: dict) -> str:
    valor = headers.get("last-modified", "")
    try:
        return parsedate_to_datetime(valor).date().isoformat() if valor else ""
    except (ValueError, TypeError, IndexError):
        return ""


def _adicionar_manifesto(caminho: Path, entrada: dict) -> None:
    import json

    dados = json.loads(caminho.read_text(encoding="utf-8")) if caminho.exists() else {"documentos": []}
    if not isinstance(dados, dict) or not isinstance(dados.get("documentos"), list):
        raise ValueError("Manifesto inválido; operação cancelada.")
    docs = dados["documentos"]
    docs[:] = [doc for doc in docs if doc.get("arquivo") != entrada["arquivo"]]
    docs.append(entrada)
    salvar_json_atomico(caminho, dados)


def sincronizar(config, embedder, *, inicio=INICIO, max_paginas=25, aplicar=False, cliente_http=None) -> dict:
    """Detecta mudanças por conteúdo textual; por padrão faz somente dry-run.

    Não remove URLs que desapareceram, evitando exclusões por erros transitórios do site.
    """
    if config.vector_store != "qdrant":
        raise ValueError("Crawler incremental requer VECTOR_STORE=qdrant.")
    inicio = normalizar_url(inicio)
    if not url_autorizada(inicio):
        raise ValueError("URL inicial não autorizada.")
    estado_arquivo = config.manifesto.parent / "crawler_state.json"
    estado = carregar_estado(estado_arquivo)
    manifesto = __import__("json").loads(config.manifesto.read_text(encoding="utf-8")) if config.manifesto.exists() else {"documentos": []}
    # Documentos cadastrados manualmente pertencem ao administrador; nunca sobrescrevê-los.
    urls_manuais = {normalizar_url(d["url"]) for d in manifesto.get("documentos", [])
                    if d.get("url") and d.get("origem") != "crawler" and url_autorizada(d["url"])}
    qdrant = criar_cliente_qdrant(config) if aplicar else None
    coletor = Coletor(cliente=cliente_http)
    fila = deque([inicio]); visitados = set()
    resumo = {"visitadas": 0, "novas": 0, "alteradas": 0, "inalteradas": 0,
              "ignoradas_manuais": 0, "falhas": [], "aplicado": aplicar, "urls": []}
    try:
        while fila and resumo["visitadas"] < max_paginas:
            url = fila.popleft()
            if url in visitados:
                continue
            visitados.add(url)
            resumo["visitadas"] += 1
            anterior = estado["documentos"].get(url, {})
            try:
                conteudo, ext, final, headers = coletor.baixar(
                    url, etag=anterior.get("etag", ""), last_modified=anterior.get("last_modified", "")
                )
                if conteudo is None:
                    resumo["inalteradas"] += 1
                    continue
                if ext == ".html":
                    for link in descobrir_links(conteudo, final):
                        if link not in visitados:
                            fila.append(link)
                if url in urls_manuais or final in urls_manuais:
                    resumo["ignoradas_manuais"] += 1
                    continue
                nome = "crawler_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:24] + ext
                with tempfile.TemporaryDirectory() as pasta:
                    temporario = Path(pasta) / nome
                    temporario.write_bytes(conteudo)
                    secoes = extrair_secoes(temporario)
                    texto_extraido = "\n".join(f"{s.titulo}\n{s.texto}" for s in secoes)
                    if not texto_extraido.strip():
                        raise ValueError("Não foi extraído texto indexável (PDF digitalizado pode exigir OCR).")
                    hash_atual = hash_conteudo(texto_extraido)
                    if hash_atual == anterior.get("content_hash"):
                        resumo["inalteradas"] += 1
                        continue
                    titulo = _titulo_html(conteudo, final) if ext == ".html" else re.sub(r"[-_]", " ", Path(urlsplit(final).path).stem)
                    data_coleta = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    meta = {"arquivo": nome, "titulo": titulo, "url": final,
                            "url_canonica": url, "origem": "crawler",
                            "tipo_documento": "",
                            "categoria": "academico", "atualizado_em": _data_modificacao(headers),
                            "data_coleta": data_coleta, "content_hash": hash_atual,
                            "etag": headers.get("etag", ""), "last_modified": headers.get("last-modified", "")}
                    meta = inferir_metadados(temporario, secoes, meta)
                    if not meta.get("tipo_documento"):
                        meta["tipo_documento"] = "pdf" if ext == ".pdf" else "html"
                    blocos = []
                    for secao in secoes:
                        cabecalho = f"{titulo} – {secao.titulo}"
                        orcamento = max(50, config.chunk_tokens - embedder.contar_tokens(cabecalho))
                        for parte in dividir_em_blocos(secao.texto, orcamento, config.chunk_sobreposicao, embedder.contar_tokens):
                            especificos = metadados_secao(secao, meta)
                            blocos.append({"documento": titulo, "arquivo": nome, "secao": secao.titulo,
                                           "pagina": secao.pagina, "texto": parte, "embedding_modelo": config.embedding_modelo,
                                           **{k: meta[k] for k in ("url", "url_canonica", "origem", "campus", "tipo_documento",
                                                                     "categoria", "modalidade", "curso", "periodo", "atualizado_em",
                                                                     "data_coleta", "content_hash") if meta.get(k)},
                                           **especificos})
                    if not blocos:
                        raise ValueError("Documento sem blocos indexáveis.")
                    resumo["urls"].append({"url": url, "blocos": len(blocos), "tipo": "alterada" if anterior else "nova"})
                    if aplicar:
                        vetores = embedder.embed_documentos([texto_para_embedding(b) for b in blocos])
                        validar_colecao(qdrant, config.qdrant_collection, int(vetores.shape[1]))
                        if not qdrant.collection_exists(config.qdrant_collection):
                            from qdrant_client import models
                            qdrant.create_collection(collection_name=config.qdrant_collection,
                                vectors_config=models.VectorParams(size=int(vetores.shape[1]), distance=models.Distance.COSINE))
                        upsert_incremental(
                            qdrant, config.qdrant_collection, preparar_pontos(blocos, vetores),
                            remover_legados=True,
                        )
                        config.docs_dir.mkdir(parents=True, exist_ok=True)
                        destino = config.docs_dir / nome
                        # Persistência atômica local após sucesso no Qdrant.
                        substituto = destino.with_suffix(destino.suffix + ".tmp")
                        substituto.write_bytes(conteudo)
                        substituto.replace(destino)
                        _adicionar_manifesto(config.manifesto, meta)
                        estado["documentos"][url] = {"arquivo": nome, "url_final": final,
                            "content_hash": hash_atual, "etag": headers.get("etag", ""),
                            "last_modified": headers.get("last-modified", ""), "data_coleta": data_coleta}
                        salvar_json_atomico(estado_arquivo, estado)
                    resumo["alteradas" if anterior else "novas"] += 1
            except Exception as erro:
                logger.exception("Falha ao coletar %s", url)
                resumo["falhas"].append({"url": url, "erro": str(erro)})
    finally:
        if cliente_http is None:
            coletor.cliente.close()
    return resumo


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawler incremental UNIVERSO São Gonçalo")
    parser.add_argument("--aplicar", action="store_true", help="Escreve no Qdrant; sem flag apenas simula")
    parser.add_argument("--max-paginas", type=int, default=25)
    args = parser.parse_args()
    if not 1 <= args.max_paginas <= 200:
        parser.error("--max-paginas deve ser de 1 a 200")
    config = carregar_config()
    # Em dry-run também usa o embedder do projeto para divisão idêntica dos chunks.
    embedder = criar_embedder(config)
    import json
    print(json.dumps(sincronizar(config, embedder, max_paginas=args.max_paginas, aplicar=args.aplicar), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
