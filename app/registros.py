"""Registro das interações, avaliações dos alunos e validações da equipe em JSON Lines (RF07 e RF08).

Arquivos gerados em registros/:
- interacoes.jsonl: pergunta, trechos recuperados, resposta, modo e tempo de processamento;
- avaliacoes.jsonl: avaliação do aluno (útil / não útil) para cada interação;
- revisoes.jsonl: validação da resposta pela secretaria ou coordenação (correta, parcial ou incorreta).
O aluno informa apenas o nome antes de conversar; ele é gravado junto de cada interação.

Data e hora são gravadas com o fuso horário (padrão America/Sao_Paulo, ajustável por FUSO_HORARIO):
num servidor configurado em UTC, como um contêiner Docker, o horário continua o de Brasília.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

VEREDITOS = ("correta", "parcial", "incorreta")


def fuso_horario():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(os.getenv("FUSO_HORARIO", "America/Sao_Paulo"))
    except Exception:  # noqa: BLE001 - sem base de fusos no sistema: Brasília não tem horário de verão
        return timezone(timedelta(hours=-3), "BRT")


def agora() -> datetime:
    return datetime.now(fuso_horario())


def ler_data_hora(texto: str | None) -> datetime | None:
    """Data e hora de um registro; registros antigos, sem fuso, são lidos no fuso configurado."""
    if not texto:
        return None
    try:
        valor = datetime.fromisoformat(str(texto))
    except ValueError:
        return None
    if valor.tzinfo is None:
        return valor.replace(tzinfo=fuso_horario())
    return valor.astimezone(fuso_horario())


class Registro:
    def __init__(self, pasta: Path):
        self.pasta = Path(pasta)
        self.pasta.mkdir(parents=True, exist_ok=True)
        self.arq_interacoes = self.pasta / "interacoes.jsonl"
        self.arq_avaliacoes = self.pasta / "avaliacoes.jsonl"
        self.arq_revisoes = self.pasta / "revisoes.jsonl"
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

    def registrar_interacao(self, sessao_id: str, pergunta: str, resposta, nome: str = "") -> str:
        interacao_id = uuid.uuid4().hex
        self._gravar(self.arq_interacoes, {
            "id": interacao_id,
            "data_hora": agora().isoformat(timespec="seconds"),
            "sessao_id": sessao_id,
            "nome": nome,
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

    def pergunta_de(self, interacao_id: str) -> str | None:
        for registro in reversed(self._ler(self.arq_interacoes)):
            if registro.get("id") == interacao_id:
                return registro.get("pergunta")
        return None

    def registrar_avaliacao(self, interacao_id: str, util: bool, comentario: str | None = None) -> bool:
        if not self.existe(interacao_id):
            return False
        self._gravar(self.arq_avaliacoes, {
            "interacao_id": interacao_id,
            "data_hora": agora().isoformat(timespec="seconds"),
            "util": bool(util),
            "comentario": (comentario or "").strip(),
        })
        return True

    def registrar_revisao(self, interacao_id: str, veredito: str, revisor: str, comentario: str | None = None) -> dict | None:
        """Validação humana de uma resposta. A mais recente de cada interação é a que vale."""
        if not self.existe(interacao_id) or veredito not in VEREDITOS:
            return None
        revisao = {
            "interacao_id": interacao_id,
            "data_hora": agora().isoformat(timespec="seconds"),
            "veredito": veredito,
            "revisor": " ".join(str(revisor or "").split()),
            "comentario": (comentario or "").strip(),
        }
        self._gravar(self.arq_revisoes, revisao)
        return revisao

    def dados(self) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
        """Interações, última avaliação e última validação de cada interação."""
        interacoes = self._ler(self.arq_interacoes)
        avaliacoes = {a["interacao_id"]: a for a in self._ler(self.arq_avaliacoes) if "interacao_id" in a}
        revisoes = {r["interacao_id"]: r for r in self._ler(self.arq_revisoes) if "interacao_id" in r}
        return interacoes, avaliacoes, revisoes

    def resumo(self, limite: int = 100) -> dict:
        interacoes, avaliacoes, revisoes = self.dados()
        recentes = interacoes[-limite:][::-1]
        for registro in recentes:
            registro["avaliacao"] = avaliacoes.get(registro.get("id"))
            registro["revisao"] = revisoes.get(registro.get("id"))
        uteis = sum(1 for a in avaliacoes.values() if a.get("util"))
        tempos = [r.get("tempo_ms", 0) for r in interacoes]
        return {
            "total_interacoes": len(interacoes),
            "por_modo": dict(Counter(r.get("modo") for r in interacoes)),
            "avaliacoes": {"uteis": uteis, "nao_uteis": len(avaliacoes) - uteis},
            "revisoes": dict(Counter(r.get("veredito") for r in revisoes.values())),
            "tempo_medio_ms": round(sum(tempos) / len(tempos)) if tempos else 0,
            "interacoes": recentes,
        }
