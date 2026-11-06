"""Extração, limpeza e divisão em seções dos documentos institucionais (seção 3.5 do TCC).

Formatos suportados: PDF (pypdf), HTML (BeautifulSoup), Markdown e texto simples.
Cada documento é convertido em uma lista de seções (título + texto), que depois
são segmentadas em blocos pelo módulo de indexação.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
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
_RE_SECAO_PRINCIPAL = re.compile(r"^[IVXLCDM]{1,4}\s+.+$", re.IGNORECASE)
_RE_LISTA_PRINCIPAL = re.compile(r"^\d{1,2}\.\s+")
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
    metadados: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DiagnosticoExtracao:
    """Sinais de qualidade da extração sem esconder falhas do parser."""

    formato: str
    paginas: int = 0
    paginas_com_texto: int = 0
    paginas_com_baixa_extracao: tuple[int, ...] = ()
    tabelas_detectadas: int = 0
    ocr_disponivel: bool = False
    ocr_usado: bool = False
    alertas: tuple[str, ...] = ()

    def como_dict(self) -> dict:
        return {
            "formato": self.formato,
            "paginas": self.paginas,
            "paginas_com_texto": self.paginas_com_texto,
            "paginas_com_baixa_extracao": list(self.paginas_com_baixa_extracao),
            "tabelas_detectadas": self.tabelas_detectadas,
            "ocr_disponivel": self.ocr_disponivel,
            "ocr_usado": self.ocr_usado,
            "alertas": list(self.alertas),
        }


# ---------------------------------------------------------------- limpeza
def normalizar(texto: str) -> str:
    """Normaliza Unicode, remove caracteres de controle e desfaz hifenização de fim de linha."""
    texto = unicodedata.normalize("NFC", texto)
    texto = texto.replace("\r\n", "\n").replace("\r", "\n")
    texto = texto.replace("­", "").replace("\xa0", " ").replace("\t", " ")
    texto = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", texto)
    texto = re.sub(rf"([{_MINUSCULAS}])-\n([{_MINUSCULAS}])", r"\1\2", texto)
    # Alguns PDFs colocam vários eventos na mesma linha visual. Preserve cada
    # marcador para não fundir datas e eventos no mesmo chunk.
    texto = re.sub(r"\s+(?=[✓•▪◦])", "\n", texto)
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
    if _RE_SECAO_PRINCIPAL.match(linha):
        return linha.rstrip(":").strip(), False
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
    em_lista_principal = False
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
        if em_lista_principal and _RE_LISTA_PRINCIPAL.match(linha):
            acc.adicionar(linha, pagina)
            continue
        titulo, manter = _titulo_de_linha(linha)
        if titulo:
            acc.novo(titulo, pagina)
            em_lista_principal = bool(_RE_SECAO_PRINCIPAL.match(titulo))
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


def _ocr_paginas(caminho: Path, paginas: list[int]) -> dict[int, str]:
    """OCR opcional para páginas sem texto; ausência da ferramenta é explícita."""
    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError:
        return {}

    resultados = {}
    try:
        documento = fitz.open(str(caminho))
        for numero in paginas:
            pagina = documento.load_page(numero - 1)
            pixmap = pagina.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            imagem = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)
            resultados[numero] = pytesseract.image_to_string(imagem, lang="por+eng")
        documento.close()
    except Exception:
        return {}
    return resultados


def _tem_ocr() -> bool:
    try:
        import fitz  # noqa: F401
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        return False
    return True


def _extrair_tabelas(caminho: Path) -> tuple[list[Secao], int]:
    """Usa pdfplumber quando instalado, preservando células como colunas delimitadas."""
    try:
        import pdfplumber
    except ImportError:
        return [], 0

    secoes = []
    total = 0
    try:
        with pdfplumber.open(str(caminho)) as documento:
            for numero, pagina in enumerate(documento.pages, start=1):
                for tabela in pagina.extract_tables() or []:
                    linhas = []
                    for linha in tabela:
                        celulas = [re.sub(r"\s+", " ", (celula or "")).strip() for celula in linha]
                        if any(celulas):
                            linhas.append(" | ".join(celulas))
                    texto = "\n".join(linhas).strip()
                    if texto:
                        total += 1
                        secoes.append(Secao(f"Tabela - página {numero}", texto, numero))
    except Exception:
        return [], 0
    return secoes, total


def _extrair_pdf_detalhado(caminho: Path, calendario: bool) -> tuple[list[Secao], DiagnosticoExtracao]:
    from pypdf import PdfReader

    leitor = PdfReader(str(caminho))
    paginas = []
    baixas = []
    for n, pagina in enumerate(leitor.pages, start=1):
        try:
            texto = pagina.extract_text() or ""
        except Exception:
            texto = ""
        linhas = limpar_linhas(texto)
        paginas.append((n, linhas))
        quantidade = len(re.sub(r"\s+", "", "".join(linhas)))
        if quantidade < 40:
            baixas.append(n)

    ocr_paginas = _ocr_paginas(caminho, baixas)
    ocr_usado = False
    if ocr_paginas:
        paginas = [
            (n, limpar_linhas(ocr_paginas[n])) if n in ocr_paginas and len("".join(linhas)) < len("".join(ocr_paginas[n]))
            else (n, linhas)
            for n, linhas in paginas
        ]
        ocr_usado = True

    repetidas = _linhas_repetidas([linhas for _, linhas in paginas])
    pares = ((n, l) for n, linhas in paginas for l in linhas if l not in repetidas)
    secoes = _segmentar_linhas(pares, calendario)
    tabelas, quantidade_tabelas = _extrair_tabelas(caminho)
    # Só adiciona tabelas que não sejam meras duplicações do texto principal.
    texto_principal = " ".join(s.texto for s in secoes)
    for tabela in tabelas:
        amostra = tabela.texto[:300]
        if amostra and amostra not in texto_principal:
            secoes.append(tabela)

    alertas = []
    if baixas and not ocr_paginas:
        alertas.append("Há páginas com pouca ou nenhuma extração; OCR opcional não disponível ou falhou.")
    if not secoes:
        alertas.append("Nenhuma seção textual foi extraída.")
    diagnostico = DiagnosticoExtracao(
        formato="pdf",
        paginas=len(paginas),
        paginas_com_texto=sum(bool(linhas) for _, linhas in paginas),
        paginas_com_baixa_extracao=tuple(baixas),
        tabelas_detectadas=quantidade_tabelas,
        ocr_disponivel=_tem_ocr(),
        ocr_usado=ocr_usado,
        alertas=tuple(alertas),
    )
    return secoes, diagnostico


def _extrair_pdf(caminho: Path, calendario: bool) -> list[Secao]:
    return _extrair_pdf_detalhado(caminho, calendario)[0]


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


def inferir_metadados(caminho: Path, secoes: list[Secao], metadados: dict | None = None) -> dict:
    """Preenche somente metadados sustentados pelo nome, URL ou texto extraído."""
    dados = dict(metadados or {})
    evidencia = " ".join([caminho.name, str(dados.get("titulo", "")), str(dados.get("url", ""))] +
                         [f"{s.titulo} {s.texto}" for s in secoes])
    normalizada = unicodedata.normalize("NFKD", evidencia).encode("ascii", "ignore").decode().lower()

    if not dados.get("tipo_documento"):
        tipos = (
            ("calendario", r"calend[aá]rio|calendario academico"),
            ("regimento", r"regimento|criterios? de avaliacao"),
            ("manual", r"manual informativo|\bmia\b"),
            ("plano_ensino", r"plano de ensino|ementa"),
        )
        for tipo, padrao in tipos:
            if re.search(padrao, normalizada, re.IGNORECASE):
                dados["tipo_documento"] = tipo
                break

    if not dados.get("modalidade"):
        if re.search(r"\bead\b|a distancia|a distância|semipresencial", normalizada):
            dados["modalidade"] = "ead" if re.search(r"\bead\b|a distancia|a distância", normalizada) else "semipresencial"
        elif re.search(r"\bpresencial\b", normalizada):
            dados["modalidade"] = "presencial"

    if not dados.get("periodo"):
        periodo = re.search(r"([12])\s*(?:º|o|°)?\s*semestre\s*(?:de\s*)?(20\d{2})", normalizada)
        if periodo:
            dados["periodo"] = f"{periodo.group(2)}.{periodo.group(1)}"
        else:
            periodo = re.search(r"\b(20\d{2})[_./-]([12])\b", normalizada)
            if periodo:
                dados["periodo"] = f"{periodo.group(1)}.{periodo.group(2)}"

    if not dados.get("campus") and re.search(r"sao goncalo|são gonçalo", normalizada):
        dados["campus"] = "sao_goncalo"

    return dados


def metadados_secao(secao: Secao, documento: dict) -> dict:
    """Obtém mês/ano apenas quando a própria seção ou o documento os sustentam."""
    dados = {}
    nome = normalizar(secao.titulo).upper()
    for numero, mes in enumerate(MESES, start=1):
        if mes in nome:
            dados["mes"] = numero
            break
    periodo = str(documento.get("periodo", ""))
    ano = re.match(r"(20\d{2})\.\d", periodo)
    if ano:
        dados["ano"] = int(ano.group(1))
    return dados


def extrair_documento(caminho: Path) -> tuple[list[Secao], dict]:
    """API detalhada: seções e diagnóstico, mantendo extrair_secoes compatível."""
    caminho = Path(caminho)
    extensao = caminho.suffix.lower()
    calendario = "calend" in caminho.stem.lower()
    if extensao == ".pdf":
        secoes, diagnostico = _extrair_pdf_detalhado(caminho, calendario)
        return secoes, diagnostico.como_dict()
    return extrair_secoes(caminho), {"formato": extensao.lstrip("."), "paginas": 0, "alertas": []}
