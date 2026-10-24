"""Fixtures compartilhadas pelos testes.

Os testes não usam o modelo de embeddings real nem o Ollama: um embedder determinístico
(bag-of-words com hashing) e um LLM simulado permitem validar todo o fluxo em segundos.
"""
import hashlib
import json
import re
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from app.config import Parametros, carregar_config  # noqa: E402
from app.llm import LLMIndisponivel  # noqa: E402
from app.servico import Servico  # noqa: E402


class EmbedderFalso:
    """Vetor de contagem de palavras (com 3 letras ou mais) espalhadas por hashing.

    A dimensão alta torna desprezível a chance de duas palavras caírem na mesma posição.
    """

    dimensao = 1 << 16

    def _vetor(self, texto: str) -> np.ndarray:
        vetor = np.zeros(self.dimensao, dtype="float32")
        for palavra in re.findall(r"\w+", texto.lower()):
            if len(palavra) < 3:
                continue
            posicao = int(hashlib.md5(palavra.encode("utf-8")).hexdigest(), 16) % self.dimensao
            vetor[posicao] += 1.0
        norma = np.linalg.norm(vetor)
        return vetor / norma if norma else vetor

    def embed_documentos(self, textos):
        return np.vstack([self._vetor(t) for t in textos]).astype("float32")

    def embed_consulta(self, texto):
        return self._vetor(texto)

    def contar_tokens(self, texto):
        return len(re.findall(r"\w+|[^\w\s]", texto))


class LLMFalso:
    """Simula o Ollama: registra as chamadas e devolve uma resposta fixa (ou falha)."""

    def __init__(self, resposta="Resposta do modelo com base nos trechos.", falhar=False):
        self.resposta = resposta
        self.falhar = falhar
        self.chamadas = []

    def gerar(self, mensagens, temperatura):
        self.chamadas.append({"mensagens": mensagens, "temperatura": temperatura})
        if self.falhar:
            raise LLMIndisponivel("Ollama fora do ar (simulado)")
        return self.resposta

    def disponivel(self):
        return not self.falhar


CALENDARIO_MD = """# Calendário Letivo de Teste

## Setembro
- 30 de setembro – Prazo final para solicitação de trancamento de matrícula.
- 07 de setembro – Feriado Nacional – Independência do Brasil.

## Outubro
- 05 a 10 de outubro – Período de aplicação da V1 das disciplinas presenciais.
- 15 a 20 de outubro – Período de solicitação da R1 no Espaço do Aluno.
"""

CRITERIOS_MD = """# Critérios de Avaliação de Teste

## Art. 79 – Média semestral
A média do semestre (MS) é calculada por MS = (V1 × 2 + VT + V2 × 2) / 5.
Com média igual ou superior a 7,0 o aluno é aprovado diretamente.

## Art. 80 – Segunda chamada
O aluno que perder a V1 ou a V2 poderá fazer a segunda chamada mediante requerimento com justificativa, em até 2 dias após a prova.
"""

CAMPUS_HTML = """<html><head><title>Campus</title><script>var menu = 'nao indexar';</script></head>
<body><nav><a href="#">Menu principal secreto</a></nav>
<main>
  <h2>Endereço</h2><p>A unidade fica na Rua Lambari, 10, Trindade, São Gonçalo.</p>
  <h2>Biblioteca</h2><ul><li>Biblioteca com acervo físico e digital.</li></ul>
</main>
<footer>Rodapé institucional</footer></body></html>"""

MANIFESTO = {
    "documentos": [
        {"arquivo": "calendario_teste.md", "titulo": "Calendário Letivo de Teste",
         "url": "https://universo.edu.br/calendarios/", "atualizado_em": "2026-07-22", "categoria": "calendario"},
        {"arquivo": "criterios_teste.md", "titulo": "Critérios de Avaliação de Teste",
         "url": "https://universo.edu.br/criterios.pdf", "atualizado_em": "2018-01-10", "categoria": "avaliacao"},
        {"arquivo": "campus_teste.html", "titulo": "Página do Campus de Teste",
         "url": "https://universo.edu.br/sao-goncalo/", "atualizado_em": "2026-10-23", "categoria": "campus"},
    ]
}

PERGUNTA_TRANCAMENTO = "Qual o prazo final para trancamento de matrícula?"
PERGUNTA_FORA = "xilofone quântico zebrado"


@pytest.fixture
def ambiente(tmp_path):
    """Configuração isolada em uma pasta temporária, com três documentos de teste."""
    docs = tmp_path / "documentos"
    docs.mkdir()
    (docs / "calendario_teste.md").write_text(CALENDARIO_MD, encoding="utf-8")
    (docs / "criterios_teste.md").write_text(CRITERIOS_MD, encoding="utf-8")
    (docs / "campus_teste.html").write_text(CAMPUS_HTML, encoding="utf-8")
    manifesto = tmp_path / "manifesto.json"
    manifesto.write_text(json.dumps(MANIFESTO, ensure_ascii=False), encoding="utf-8")

    config = replace(
        carregar_config(),
        docs_dir=docs,
        manifesto=manifesto,
        indice_dir=tmp_path / "indice",
        registros_dir=tmp_path / "registros",
        parametros_arquivo=tmp_path / "parametros.json",
        admin_key="chave-teste",
        chunk_tokens=120,
        chunk_sobreposicao=20,
    )
    # O embedder falso gera similaridades menores que o modelo real; o limiar é ajustado para ele.
    Parametros(limiar=0.15).salvar(config.parametros_arquivo)
    return config


@pytest.fixture
def llm():
    return LLMFalso()


@pytest.fixture
def servico(ambiente, llm):
    return Servico(ambiente, embedder=EmbedderFalso(), llm=llm)
