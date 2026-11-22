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
    llm_keep_alive: str
    # Embeddings
    embedding_provedor: str  # "sentence-transformers" ou "ollama"
    embedding_modelo: str
    # Segmentação
    chunk_tokens: int
    chunk_sobreposicao: int
    # Caminhos
    docs_dir: Path
    originais_dir: Path
    docling_dir: Path
    manifesto: Path
    indice_dir: Path
    registros_dir: Path
    parametros_arquivo: Path
    # Segurança (RNF07: credenciais fora do código)
    admin_key: str
    pdf_upload_max_mb: int
    admin_email: str = ""
    admin_pass: str = ""
    llm_num_gpu: int = -1  # camadas do modelo na GPU (Ollama); -1 = automático
    # API OpenAI: modelos tentados, em ordem, quando o principal falha ou demora a começar a responder
    llm_modelos_reserva: tuple[str, ...] = ()
    llm_timeout_primeiro_token: float = 0.0  # segundos sem nenhum texto até trocar de modelo; 0 = desligado
    llm_max_tokens: int = 700
    # Perguntas mais frequentes dos registros respondidas ao subir e após reindexar (0 = desligado)
    cache_aquecer: int = 0
    # Reordenação dos candidatos por um cross-encoder (vazio = desligado)
    reranker_modelo: str = ""
    reranker_candidatos: int = 6


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
        llm_keep_alive=_env("LLM_KEEP_ALIVE", "2h"),
        llm_num_gpu=int(_env("LLM_NUM_GPU", "-1")),
        embedding_provedor=provedor,
        embedding_modelo=_env("EMBEDDING_MODELO", modelo_emb_padrao),
        chunk_tokens=int(_env("CHUNK_TOKENS", "500")),
        chunk_sobreposicao=int(_env("CHUNK_SOBREPOSICAO", "50")),
        docs_dir=Path(_env("DOCS_DIR", str(BASE_DIR / "data" / "documentos"))),
        originais_dir=Path(_env("ORIGINAIS_DIR", str(BASE_DIR / "data" / "originais" / "uploads"))),
        docling_dir=Path(_env("DOCLING_DIR", str(BASE_DIR / "data" / "docling"))),
        manifesto=Path(_env("MANIFESTO", str(BASE_DIR / "data" / "manifesto.json"))),
        indice_dir=Path(_env("INDICE_DIR", str(BASE_DIR / "indice"))),
        registros_dir=Path(_env("REGISTROS_DIR", str(BASE_DIR / "registros"))),
        parametros_arquivo=Path(_env("PARAMETROS_ARQUIVO", str(BASE_DIR / "data" / "parametros.json"))),
        admin_key=_env("ADMIN_KEY", ""),
        pdf_upload_max_mb=max(1, int(_env("PDF_UPLOAD_MAX_MB", "25"))),
        admin_email=_env("EMAIL", ""),
        admin_pass=os.getenv("PASS", "").strip(),
        llm_modelos_reserva=tuple(m.strip() for m in _env("LLM_MODELOS_RESERVA", "").split(",") if m.strip()),
        llm_timeout_primeiro_token=float(_env("LLM_TIMEOUT_PRIMEIRO_TOKEN", "0")),
        llm_max_tokens=max(50, int(_env("LLM_MAX_TOKENS", "700"))),
        cache_aquecer=max(0, int(_env("CACHE_AQUECER", "0"))),
        reranker_modelo=_env("RERANKER_MODELO", ""),
        reranker_candidatos=max(2, int(_env("RERANKER_CANDIDATOS", "6"))),
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

# Valem para qualquer prompt_sistema: o administrador ajusta o tom, mas estas regras não saem.
# Pedir dados ao aluno é decisão do sistema (MotorRAG._campos_para_esclarecer), nunca do modelo.
MENSAGEM_NAO_LOCALIZADA = "Não localizei essa informação nos documentos oficiais."

REGRAS_RESPOSTA = (
    "Regras obrigatórias:\n"
    "1. Responda diretamente, usando somente o que está escrito nos trechos.\n"
    # Testado em 2026-11-07: permitir que o modelo pergunte (formato "ESCLARECIMENTO:") baixou o acerto
    # de 33/35 para 30-31/35 e ele nunca usou o formato. A pergunta de volta é decidida pelo código.
    "2. Nunca faça perguntas ao usuário nem peça curso, modalidade, período, semestre ou qualquer "
    "outro dado. Responda com o que os trechos trazem.\n"
    f"3. Se a informação pedida não estiver nos trechos, escreva: \"{MENSAGEM_NAO_LOCALIZADA}\" e "
    "indique o Espaço do Aluno ou a central 0800 721 0251. Não dê resposta vaga.\n"
    "4. Em perguntas de data ou prazo, responda com a data escrita nos trechos. Sem data nos trechos, "
    "aplique a regra 3.\n"
    "5. Não invente datas, valores, notas ou procedimentos. Não complete lacunas com suposições.\n"
    "6. Não aplique a um nível de ensino ou modalidade regras escritas para outro, como pós-graduação "
    "para graduação ou presencial para EaD, e não combine regras de seções diferentes sem que os "
    "trechos indiquem essa relação.\n"
    "7. Não expanda nem redefina siglas por conta própria; preserve o significado dos trechos.\n"
    "8. Não cite \"Trecho 1\" ou números de trecho na resposta; as fontes são exibidas ao usuário.\n"
    "9. Datas de calendário valem para o calendário de onde vieram: ao informar uma data, diga de qual "
    "calendário ela é (por exemplo, o dos cursos a distância), conforme o nome da fonte.\n"
    "10. Escreva fórmulas em texto simples, como MF = (MS + VS)/2. Não use LaTeX nem símbolos como \\frac."
)


@dataclass
class Parametros:
    """Parâmetros de recuperação e geração (valores padrão conforme a seção 3.6 do TCC)."""

    top_k: int = 4
    limiar: float = 0.60
    temperatura: float = 0.1
    historico_trocas: int = 2
    prompt_sistema: str = PROMPT_PADRAO
    # Peso da busca lexical (BM25) somado à similaridade vetorial; 0 desliga a busca híbrida.
    peso_lexical: float = 0.08
    # Corte relativo: descarta trechos com similaridade abaixo de (melhor similaridade - margem).
    # Com o e5, até trechos sem relação ficam acima de 0,8, e o limiar absoluto quase não filtra. 0 desliga.
    margem_relativa: float = 0.0

    def validar(self) -> None:
        if not 1 <= int(self.top_k) <= 20:
            raise ValueError("top_k deve estar entre 1 e 20.")
        if not 0.0 <= float(self.limiar) <= 1.0:
            raise ValueError("limiar deve estar entre 0 e 1.")
        if not 0.0 <= float(self.temperatura) <= 2.0:
            raise ValueError("temperatura deve estar entre 0 e 2.")
        if not 0 <= int(self.historico_trocas) <= 10:
            raise ValueError("historico_trocas deve estar entre 0 e 10.")
        if not 0.0 <= float(self.peso_lexical) <= 1.0:
            raise ValueError("peso_lexical deve estar entre 0 e 1.")
        if not 0.0 <= float(self.margem_relativa) <= 1.0:
            raise ValueError("margem_relativa deve estar entre 0 e 1.")
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
