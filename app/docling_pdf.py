"""Conversão estruturada de PDFs com Docling para Markdown e JSON."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from .docling_layout import markdown_estruturado

os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")


class ErroConversaoPDF(RuntimeError):
    """O Docling não conseguiu produzir conteúdo utilizável para o PDF."""


_conversor = None
_lock = threading.RLock()


def _obter_conversor():
    global _conversor
    with _lock:
        if _conversor is None:
            # O transporte Xet/CAS falha em alguns ambientes Windows; HTTP mantém o cache compatível.
            try:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption
            except ImportError as erro:  # pragma: no cover - depende da instalação do ambiente
                raise ErroConversaoPDF(
                    "Docling não está instalado. Execute: python -m pip install -r requirements.txt"
                ) from erro

            opcoes = PdfPipelineOptions()
            opcoes.do_ocr = True
            opcoes.do_table_structure = True
            _conversor = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(pipeline_options=opcoes),
                }
            )
        return _conversor


def converter_pdf_docling(caminho: Path) -> tuple[str, dict]:
    """Converte um PDF local e devolve Markdown e o modelo estruturado do Docling."""
    caminho = Path(caminho)
    try:
        with _lock:
            resultado = _obter_conversor().convert(caminho)
            markdown = resultado.document.export_to_markdown().strip()
            estruturado = resultado.document.export_to_dict()
            if isinstance(estruturado, dict):
                markdown = markdown_estruturado(markdown, estruturado)
    except ErroConversaoPDF:
        raise
    except Exception as erro:
        raise ErroConversaoPDF(f"Falha do Docling ao converter {caminho.name}: {erro}") from erro
    if not markdown:
        raise ErroConversaoPDF(f"O Docling não extraiu conteúdo de {caminho.name}.")
    if not isinstance(estruturado, dict):
        raise ErroConversaoPDF(f"O Docling retornou uma estrutura inválida para {caminho.name}.")
    return markdown, estruturado
