"""Registro das interações e avaliações em JSON Lines (RF07 e RF08).

Arquivos gerados em registros/:
- interacoes.jsonl: pergunta, trechos recuperados, resposta, modo e tempo de processamento;
- avaliacoes.jsonl: avaliação do aluno (útil / não útil) para cada interação.
Nenhum dado pessoal é solicitado ao aluno (RNF04).
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path


class Registro:
    def __init__(self, pasta: Path):
        self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.arq_interacoes = self.pasta / "interacoes.jsonl"
        self.arq_avaliacoes = self.pasta / "avaliacoes.jsonl"
        self._lock = threading.Lock()
        self._ids = {r.get("id") for r in self._ler(self.arq_interacoes)}

    @staticmethod
    def _ler(arquivo: Path) -> list[dict]:
        if not arquivo.exists():
            return []
        registros = []
        with arquivo.open(encoding="utf-8") as f:
            for linha in f:
                linha = linha.strip()
                if not linha:
                    continue
                try:
                    registros.append(json.loads(linha))
                except json.JSONDecodeError:
                    continue
        return registros

    def _gravar(self, arquivo: Path, registro: dict) -> None:
        with self._lock, arquivo.open("a", encoding="utf-8") as f:
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")

    def registrar_interacao(self, sessao_id: str, pergunta: str, resposta) -> str:
        interacao_id = uuid.uuid4().hex
        self._gravar(self.arq_interacoes, {
            "id": interacao_id,
            "data_hora": datetime.now().isoformat(timespec="seconds"),
            "sessao_id": sessao_id,
            "pergunta": pergunta,
            "resposta": resposta.resposta,
            "modo": resposta.modo,
            "tempo_ms": resposta.tempo_ms,
            "trechos": resposta.trechos,
        })
        self._ids.add(interacao_id)
        return interacao_id

    def existe(self, interacao_id: str) -> bool:
        return interacao_id in self._ids

    def registrar_avaliacao(self, interacao_id: str, util: bool, comentario: str | None = None) -> bool:
        if not self.existe(interacao_id):
            return False
        self._gravar(self.arq_avaliacoes, {
            "interacao_id": interacao_id,
            "data_hora": datetime.now().isoformat(timespec="seconds"),
            "util": bool(util),
            "comentario": (comentario or "").strip(),
        })
        return True

    def resumo(self, limite: int = 100) -> dict:
        interacoes = self._ler(self.arq_interacoes)
        avaliacoes = {a["interacao_id"]: a for a in self._ler(self.arq_avaliacoes) if "interacao_id" in a}
        recentes = interacoes[-limite:][::-1]
        for registro in recentes:
            registro["avaliacao"] = avaliacoes.get(registro.get("id"))
        uteis = sum(1 for a in avaliacoes.values() if a.get("util"))
        tempos = [r.get("tempo_ms", 0) for r in interacoes]
        return {
            "total_interacoes": len(interacoes),
            "por_modo": dict(Counter(r.get("modo") for r in interacoes)),
            "avaliacoes": {"uteis": uteis, "nao_uteis": len(avaliacoes) - uteis},
            "tempo_medio_ms": round(sum(tempos) / len(tempos)) if tempos else 0,
            "interacoes": recentes,
        }
