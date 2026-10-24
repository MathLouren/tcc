"""Extração, limpeza e divisão em seções dos documentos institucionais (seção 3.5 do TCC).

Formatos suportados: PDF (pypdf), HTML (BeautifulSoup), Markdown e texto simples.
Cada documento é convertido em uma lista de seções (título + texto), que depois
são segmentadas em blocos pelo módulo de indexação.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

EXTENSOES_SUPORTADAS = {".pdf", ".html", ".htm", ".md", ".txt"}
TITULO_PADRAO = "Geral"

MESES = (
    "JANEIRO", "FEVEREIRO", "MARÇO", "ABRIL", "MAIO", "JUNHO",
    "JULHO", "AGOSTO", "SETEMBRO", "OUTUBRO", "NOVEMBRO", "DEZEMBRO",
)

_MAIUSCULAS = "A-ZÁÉÍÓÚÂÊÔÃÕÇ"
_MINUSCULAS = "a-záéíóúâêôãõç"

_RE_ARTIGO = re.compile(r"^(?:Art(?:igo)?\.?)\s*(\d+)\s*[º°o]?\b", re.IGNORECASE)
_RE_NUMERADO = re.compile(rf"^(\d{{1,2}}\.(?:\d{{1,2}}\.?){{0,4}})\s*([{_MAIUSCULAS}].{{2,90}})$")
_RE_PAGINA = re.compile(r"^(?:p[áa]gina\s+)?\d{1,3}(?:\s+de\s+\d{1,3})?$", re.IGNORECASE)
_RE_ITEM = re.compile(r"^(?:[-*•▪◦]|\(?[a-z0-9]{1,3}[).])\s+")
_RE_MES = re.compile(r"\b(" + "|".join(MESES) + r")\b")
_RE_GRADE = re.compile(r"(?:\b[DSTQ]\b\s*){5,}|(?:\b\d{1,2}\b\s+){4,}\b\d{1,2}\b")
_RE_MD_TITULO = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")


@dataclass
class Secao:
    titulo: str
    texto: str
    pagina: int | None = None


# ---------------------------------------------------------------- limpeza
def normalizar(texto: str) -> str:
    """Normaliza Unicode, remove caracteres de controle e desfaz hifenização de fim de linha."""
    texto = unicodedata.normalize("NFC", texto)
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = texto.replace("­", "").replace("\xa0", " ").replace("\t", " ")
    texto = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", texto)
    texto = re.sub(rf"([{_MINUSCULAS}])-\n([{_MINUSCULAS}])", r"\1\2", texto)
    return texto


def limpar_linhas(texto: str) -> list[str]:
    """Quebra o texto em linhas limpas, descartando números de página.

    Linhas vazias são mantidas como separadores de parágrafo.
    """
    linhas = []
    for bruta in normalizar(texto).split("\n"):
        linha = re.sub(r" {2,}", " ", bruta).strip()
        if linha and _RE_PAGINA.match(linha):
            continue
        linhas.append(linha)
    return linhas


def juntar_paragrafos(linhas: Iterable[str]) -> list[str]:
    """Reúne linhas quebradas no meio da frase (comum em PDFs) em parágrafos."""
    paragrafos: list[str] = []
    atual = ""
    for linha in linhas:
        if not linha:
            if atual:
                paragrafos.append(atual)
                atual = ""
            continue
        continua = (
            atual
            and not atual.endswith((".", ":", ";", "!", "?"))
            and linha[0].islower()
            and not _RE_ITEM.match(linha)
        )
        if continua:
            atual += " " + linha
        else:
            if atual:
                paragrafos.append(atual)
            atual = linha
    if atual:
        paragrafos.append(atual)
    return paragrafos


# ---------------------------------------------------------------- seções
class _Acumulador:
    """Acumula linhas e fecha uma seção a cada novo título encontrado."""

    def __init__(self, juntar: bool = True):
        self.secoes: list[Secao] = []
        self.titulo = TITULO_PADRAO
        self.linhas: list[str] = []
        self.pagina: int | None = None
        self.juntar = juntar

    def novo(self, titulo: str, pagina: int | None = None) -> None:
        self.fechar()
        self.titulo = titulo.strip() or TITULO_PADRAO
        self.pagina = pagina

    def adicionar(self, linha: str, pagina: int | None = None) -> None:
        if self.pagina is None:
            self.pagina = pagina
        self.linhas.append(linha)

    def fechar(self) -> None:
        if self.juntar:
            texto = "\n".join(juntar_paragrafos(self.linhas))
        else:
            texto = "\n".join(l for l in self.linhas if l)
        texto = texto.strip()
        if texto:
            self.secoes.append(Secao(self.titulo, texto, self.pagina))
        self.linhas = []
        self.pagina = None


def _eh_maiusculo(linha: str) -> bool:
    letras = [c for c in linha if c.isalpha()]
    return len(letras) >= 4 and all(c.isupper() for c in letras) and len(linha) <= 80


def _titulo_de_linha(linha: str) -> tuple[str | None, bool]:
    """Identifica títulos de seção. Retorna (título, manter_linha_no_texto)."""
    artigo = _RE_ARTIGO.match(linha)
    if artigo:
        return f"Art. {artigo.group(1)}", True
    curta = len(linha) <= 90 and len(linha.split()) <= 12
    if curta and not linha.endswith((".", ";", ",", ":")):
        if _RE_NUMERADO.match(linha) or _eh_maiusculo(linha):
            return linha, False
    return None, False


def _extrair_mes(linha: str) -> tuple[str, str | None]:
    """Em calendários, remove a grade de dias e detecta o nome do mês."""
    linha = _RE_GRADE.sub(" ", linha)
    mes = None
    encontrado = _RE_MES.search(linha)
    if encontrado:
        mes = encontrado.group(1).capitalize()
        linha = linha[: encontrado.start()] + linha[encontrado.end():]
    return re.sub(r"\s{2,}", " ", linha).strip(), mes


def _segmentar_linhas(pares: Iterable[tuple[int | None, str]], calendario: bool = False) -> list[Secao]:
    acc = _Acumulador()
    for pagina, linha in pares:
        if not linha:
            acc.adicionar("", pagina)
            continue
        if calendario:
            linha, mes = _extrair_mes(linha)
            if mes:
                acc.novo(mes, pagina)
            if not linha:
                continue
        titulo, manter = _titulo_de_linha(linha)
        if titulo:
            acc.novo(titulo, pagina)
            if not manter:
                continue
        acc.adicionar(linha, pagina)
    acc.fechar()
    return acc.secoes


# ---------------------------------------------------------------- formatos
def _linhas_repetidas(paginas: list[list[str]]) -> set[str]:
    """Linhas presentes em pelo menos metade das páginas (cabeçalhos e rodapés)."""
    if len(paginas) < 2:
        return set()
    contagem = Counter(l for linhas in paginas for l in set(linhas) if l)
    minimo = max(2, round(len(paginas) * 0.5))
    return {l for l, n in contagem.items() if n >= minimo and len(l) < 200}


def _extrair_pdf(caminho: Path, calendario: bool) -> list[Secao]:
    from pypdf import PdfReader

    leitor = PdfReader(str(caminho))
    paginas = [(n, limpar_linhas(p.extract_text() or "")) for n, p in enumerate(leitor.pages, start=1)]
    repetidas = _linhas_repetidas([linhas for _, linhas in paginas])
    pares = ((n, l) for n, linhas in paginas for l in linhas if l not in repetidas)
    return _segmentar_linhas(pares, calendario)


def _extrair_html(caminho: Path) -> list[Secao]:
    from bs4 import BeautifulSoup

    sopa = BeautifulSoup(caminho.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for tag in sopa(["script", "style", "noscript", "nav", "header", "footer", "form", "aside", "svg", "iframe"]):
        tag.decompose()
    seletores = '[class*="menu"], [id*="menu"], [class*="navbar"], [class*="cookie"], [id*="cookie"]'
    for tag in sopa.select(seletores):
        if not getattr(tag, "decomposed", False):
            tag.decompose()

    raiz = sopa.find("main") or sopa.find("article") or sopa.body or sopa
    acc = _Acumulador()
    for el in raiz.find_all(["h1", "h2", "h3", "h4", "p", "li", "td", "th", "dd", "dt"]):
        if el.name in ("h1", "h2", "h3", "h4"):
            titulo = el.get_text(" ", strip=True)
            if titulo:
                acc.novo(titulo)
            continue
        if el.find_parent(["p", "li", "td", "th", "dd"]):
            continue  # o texto já foi incluído pelo elemento pai
        texto = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if texto:
            acc.adicionar(texto)
            acc.adicionar("")
    acc.fechar()
    return acc.secoes


def _limpar_markdown(texto: str) -> str:
    texto = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", texto)
    texto = re.sub(r"(\*\*|__|`)", "", texto)
    return texto.strip()


def _extrair_markdown(texto: str) -> list[Secao]:
    """Seções pelos títulos Markdown; o título da seção inclui a hierarquia (ex.: 'Art. 80 > § 1º')."""
    acc = _Acumulador(juntar=False)
    pilha: list[tuple[int, str]] = []
    for bruta in normalizar(texto).split("\n"):
        linha = bruta.strip()
        titulo_md = _RE_MD_TITULO.match(linha)
        if titulo_md:
            nivel, titulo = len(titulo_md.group(1)), _limpar_markdown(titulo_md.group(2))
            pilha = [(n, t) for n, t in pilha if n < nivel] + [(nivel, titulo)]
            caminho = [t for n, t in pilha if n > 1] or [titulo]
            acc.novo(" > ".join(caminho))
            continue
        if linha.startswith(">"):
            linha = linha.lstrip("> ").strip()
        if linha.startswith("|"):
            celulas = [c.strip() for c in linha.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in celulas if c):
                continue  # linha separadora da tabela
            linha = " | ".join(c for c in celulas if c)
        linha = re.sub(r"^[-*+]\s+", "", linha)
        acc.adicionar(_limpar_markdown(linha))
    acc.fechar()
    return acc.secoes


def extrair_secoes(caminho: Path) -> list[Secao]:
    """Extrai as seções de um documento de acordo com sua extensão."""
    caminho = Path(caminho)
    extensao = caminho.suffix.lower()
    calendario = "calend" in caminho.stem.lower()
    if extensao == ".pdf":
        return _extrair_pdf(caminho, calendario)
    if extensao in (".html", ".htm"):
        return _extrair_html(caminho)
    texto = caminho.read_text(encoding="utf-8", errors="ignore")
    if extensao == ".md":
        return _extrair_markdown(texto)
    if extensao == ".txt":
        return _segmentar_linhas(((None, l) for l in limpar_linhas(texto)), calendario)
    raise ValueError(f"Formato não suportado: {caminho.name}")
