"""Persistência atômica, por URL, do estado de coleta e indexação."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def carregar_estado(caminho: Path) -> dict:
    if not caminho.exists():
        return {"versao": 1, "documentos": {}}
    dado = json.loads(caminho.read_text(encoding="utf-8"))
    if not isinstance(dado, dict) or not isinstance(dado.get("documentos"), dict):
        raise ValueError("Estado do crawler inválido; nenhuma alteração foi realizada.")
    return dado


def salvar_json_atomico(caminho: Path, dado: dict) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    fd, nome = tempfile.mkstemp(prefix=caminho.name + ".", suffix=".tmp", dir=caminho.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as arquivo:
            json.dump(dado, arquivo, ensure_ascii=False, indent=2)
            arquivo.write("\n")
            arquivo.flush()
            os.fsync(arquivo.fileno())
        os.replace(nome, caminho)
    finally:
        if os.path.exists(nome):
            os.unlink(nome)
