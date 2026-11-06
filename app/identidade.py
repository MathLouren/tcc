"""Identificadores determinísticos e assinatura do conteúdo do crawler/RAG."""
from __future__ import annotations

import hashlib
import re
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PARAMETROS_REMOVER = frozenset({"fbclid", "gclid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"})


def normalizar_url(url: str) -> str:
    partes = urlsplit(url.strip())
    if partes.scheme.lower() not in ("http", "https") or not partes.hostname:
        raise ValueError("Somente URLs HTTP(S) absolutas são permitidas.")
    if partes.username or partes.password:
        raise ValueError("URLs com credenciais não são permitidas.")
    host = partes.hostname.lower().rstrip(".")
    porta = partes.port
    netloc = host + (f":{porta}" if porta and porta not in (80, 443) else "")
    caminho = re.sub(r"/{2,}", "/", partes.path or "/")
    if caminho != "/":
        # WordPress frequentemente exige a barra final para páginas HTML.
        # Preserve o sufixo de diretório para não entrar em loop de redirects.
        final = caminho.rsplit("/", 1)[-1]
        caminho = caminho.rstrip("/") + ("/" if "." not in final else "")
    parametros = [(k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True) if k.lower() not in PARAMETROS_REMOVER and not k.lower().startswith("utm_")]
    return urlunsplit((partes.scheme.lower(), netloc, caminho, urlencode(sorted(parametros)), ""))


def hash_conteudo(texto: str) -> str:
    # Normalização conservadora; mudanças em termos, pontuação e datas geram novo hash.
    texto = re.sub(r"\s+", " ", texto).strip()
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def gerar_document_id(arquivo: str, url: str = "") -> str:
    chave = f"url:{normalizar_url(url)}" if url else f"arquivo:{arquivo.lower()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"universo-tcc:{chave}"))


def gerar_chunk_id(document_id: str, secao: str, ordem: int, texto: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"universo-tcc:{document_id}:{secao}:{ordem}:{hash_conteudo(texto)}"))
