"""Calcula a pontuação do System Usability Scale (SUS) a partir das respostas dos participantes (seção 4.4 do TCC).

Formato do CSV (separador ; ou ,): participante,q1,q2,...,q10 com respostas de 1 a 5.
Use avaliacao/sus_respostas_modelo.csv como modelo.

Uso:
    python avaliacao/sus.py avaliacao/sus_respostas.csv
"""
from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

AFIRMACOES = [
    "Gostaria de usar este sistema com frequência",
    "Achei o sistema desnecessariamente complexo",
    "Achei o sistema fácil de usar",
    "Precisaria de apoio técnico para usar o sistema",
    "As funções do sistema estão bem integradas",
    "Há muita inconsistência no sistema",
    "A maioria das pessoas aprenderia a usar o sistema rapidamente",
    "Achei o sistema muito complicado de usar",
    "Senti-me confiante ao usar o sistema",
    "Precisei aprender muitas coisas antes de usar o sistema",
]


def pontuacao_sus(respostas: list[int]) -> float:
    """Itens ímpares contribuem (resposta - 1); itens pares, (5 - resposta). A soma é multiplicada por 2,5."""
    if len(respostas) != 10 or any(r not in (1, 2, 3, 4, 5) for r in respostas):
        raise ValueError("cada participante deve ter 10 respostas entre 1 e 5")
    soma = sum((r - 1) if i % 2 == 0 else (5 - r) for i, r in enumerate(respostas))
    return soma * 2.5


def faixa(pontos: float) -> str:
    if pontos >= 80.3:
        return "excelente (nota A na escala de Sauro e Lewis)"
    if pontos >= 68:
        return "acima da média (Bangor, Kortum e Miller, 2009)"
    if pontos >= 51:
        return "aceitável, abaixo da média"
    return "inaceitável"


def ler_respostas(caminho: Path) -> list[tuple[str, list[int]]]:
    texto = caminho.read_text(encoding="utf-8-sig")
    delimitador = ";" if texto.count(";") > texto.count(",") else ","
    leitor = csv.DictReader(texto.splitlines(), delimiter=delimitador)
    participantes = []
    for n, linha in enumerate(leitor, start=1):
        if not any((linha.get(f"q{i}") or "").strip() for i in range(1, 11)):
            continue
        respostas = [int(linha[f"q{i}"]) for i in range(1, 11)]
        participantes.append((linha.get("participante") or f"P{n}", respostas))
    return participantes


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    participantes = ler_respostas(Path(sys.argv[1]))
    if not participantes:
        print("Nenhuma resposta encontrada no arquivo.")
        sys.exit(1)

    pontos = [pontuacao_sus(r) for _, r in participantes]
    print(f"Participantes: {len(participantes)}\n")
    print("Média por item:")
    for i, afirmacao in enumerate(AFIRMACOES):
        media_item = statistics.mean(r[i] for _, r in participantes)
        valor = f"{media_item:.1f}".replace(".", ",")
        print(f"  {i + 1:>2}. {afirmacao}: {valor}")
    print("\nPontuação por participante:")
    for (nome, _), p in zip(participantes, pontos):
        print(f"  {nome}: {p:.1f}")
    media_sus = statistics.mean(pontos)
    desvio = statistics.stdev(pontos) if len(pontos) > 1 else 0.0
    print(f"\nPontuação SUS média: {media_sus:.1f} (desvio-padrão {desvio:.1f}) – {faixa(media_sus)}")


if __name__ == "__main__":
    main()
