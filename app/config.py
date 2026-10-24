"""Configurações do chatbot.

Duas camadas de configuração:
- Config: definida por variáveis de ambiente (arquivo .env), fixa durante a execução.
  O único item que normalmente precisa ser alterado é OLLAMA_URL.
- Parametros: ajustáveis em tempo de execução pelo administrador (RF10),
  persistidos em data/parametros.json.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

try:  # python-dotenv é opcional: sem ele, valem apenas as variáveis do sistema
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # pragma: no cover
    pass


def _env(nome: str, padrao: str) -> str:
    valor = os.getenv(nome)
    return valor.strip() if valor and valor.strip() else padrao


@dataclass(frozen=True)
class Config:
    # Modelo de linguagem (Ollama por padrão)
    ollama_url: str
    llm_api: str  # "ollama" (API nativa) ou "openai" (API compatível com OpenAI)
    llm_base_url: str
    llm_modelo: str
    llm_api_key: str
    llm_timeout: float
    llm_num_ctx: int
    # Embeddings
    embedding_provedor: str  # "sentence-transformers" ou "ollama"
    embedding_modelo: str
    # Segmentação
    chunk_tokens: int
    chunk_sobreposicao: int
    # Caminhos
    docs_dir: Path
    manifesto: Path
    indice_dir: Path
    registros_dir: Path
    parametros_arquivo: Path
    # Segurança (RNF07: credenciais fora do código)
    admin_key: str


def carregar_config() -> Config:
    ollama = _env("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    llm_api = _env("LLM_API", "ollama").lower()
    if llm_api == "ollama":
        llm_base = ollama
    else:
        llm_base = _env("LLM_BASE_URL", ollama + "/v1").rstrip("/")

    provedor = _env("EMBEDDING_PROVEDOR", "sentence-transformers").lower()
    modelo_emb_padrao = "intfloat/multilingual-e5-base" if provedor == "sentence-transformers" else "bge-m3"

    return Config(
        ollama_url=ollama,
        llm_api=llm_api,
        llm_base_url=llm_base,
        llm_modelo=_env("LLM_MODELO", "llama3.1:8b"),
        llm_api_key=_env("LLM_API_KEY", ""),
        llm_timeout=float(_env("LLM_TIMEOUT", "120")),
        llm_num_ctx=int(_env("LLM_NUM_CTX", "8192")),
        embedding_provedor=provedor,
        embedding_modelo=_env("EMBEDDING_MODELO", modelo_emb_padrao),
        chunk_tokens=int(_env("CHUNK_TOKENS", "500")),
        chunk_sobreposicao=int(_env("CHUNK_SOBREPOSICAO", "50")),
        docs_dir=Path(_env("DOCS_DIR", str(BASE_DIR / "data" / "documentos"))),
        manifesto=Path(_env("MANIFESTO", str(BASE_DIR / "data" / "manifesto.json"))),
        indice_dir=Path(_env("INDICE_DIR", str(BASE_DIR / "indice"))),
        registros_dir=Path(_env("REGISTROS_DIR", str(BASE_DIR / "registros"))),
        parametros_arquivo=Path(_env("PARAMETROS_ARQUIVO", str(BASE_DIR / "data" / "parametros.json"))),
        admin_key=_env("ADMIN_KEY", ""),
    )


PROMPT_PADRAO = (
    "Você é o assistente virtual da UNIVERSO para alunos do campus São Gonçalo. "
    "Responda somente com base nos trechos fornecidos. "
    "Se a informação não estiver nos trechos, diga que não a localizou nos documentos oficiais "
    "e indique o Espaço do Aluno ou a central de atendimento (0800 721 0251). "
    "Não invente datas, valores ou procedimentos e não combine regras de seções diferentes "
    "sem que os trechos indiquem essa relação. "
    "Responda em português do Brasil, de forma clara, objetiva e cordial. "
    "Ao final, liste as fontes utilizadas no formato [Documento – seção]."
)


@dataclass
class Parametros:
    """Parâmetros de recuperação e geração (valores padrão conforme a seção 3.6 do TCC)."""

    top_k: int = 4
    limiar: float = 0.60
    temperatura: float = 0.1
    historico_trocas: int = 2
    prompt_sistema: str = PROMPT_PADRAO

    def validar(self) -> None:
        if not 1 <= int(self.top_k) <= 20:
            raise ValueError("top_k deve estar entre 1 e 20.")
        if not 0.0 <= float(self.limiar) <= 1.0:
            raise ValueError("limiar deve estar entre 0 e 1.")
        if not 0.0 <= float(self.temperatura) <= 2.0:
            raise ValueError("temperatura deve estar entre 0 e 2.")
        if not 0 <= int(self.historico_trocas) <= 10:
            raise ValueError("historico_trocas deve estar entre 0 e 10.")
        if not str(self.prompt_sistema).strip():
            raise ValueError("prompt_sistema não pode ser vazio.")

    @classmethod
    def carregar(cls, arquivo: Path) -> "Parametros":
        if arquivo.exists():
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
            validos = {f.name for f in fields(cls)}
            parametros = cls(**{k: v for k, v in dados.items() if k in validos})
        else:
            parametros = cls()
        parametros.validar()
        return parametros

    def salvar(self, arquivo: Path) -> None:
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        arquivo.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
