"""Cliente do modelo de linguagem (seção 3.6 do TCC).

Por padrão usa a API nativa do Ollama (/api/chat), que permite definir o tamanho da
janela de contexto (num_ctx). Com LLM_API=openai, usa qualquer API compatível com o
padrão OpenAI (/chat/completions), inclusive a do próprio Ollama em /v1 (RNF05).
"""
from __future__ import annotations

import re

import httpx

from .config import Config


class LLMIndisponivel(RuntimeError):
    """O modelo de linguagem não respondeu; o motor RAG usa o modo extrativo."""


def _limpar_resposta(texto: str | None) -> str:
    texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.DOTALL)  # modelos com raciocínio
    return texto.strip()


class ClienteLLM:
    def __init__(self, api: str, base_url: str, modelo: str, api_key: str = "",
                 timeout: float = 120.0, num_ctx: int = 8192):
        self.api = api
        self.base_url = base_url.rstrip("/")
        self.modelo = modelo
        self.api_key = api_key
        self.timeout = timeout
        self.num_ctx = num_ctx

    def _cabecalhos(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def gerar(self, mensagens: list[dict], temperatura: float) -> str:
        try:
            if self.api == "ollama":
                resposta = httpx.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.modelo,
                        "messages": mensagens,
                        "stream": False,
                        "options": {"temperature": temperatura, "num_ctx": self.num_ctx},
                    },
                    timeout=self.timeout,
                )
                resposta.raise_for_status()
                conteudo = resposta.json()["message"]["content"]
            else:
                resposta = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._cabecalhos(),
                    json={"model": self.modelo, "messages": mensagens, "temperature": temperatura},
                    timeout=self.timeout,
                )
                resposta.raise_for_status()
                conteudo = resposta.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as erro:
            raise LLMIndisponivel(f"Falha ao consultar o modelo de linguagem: {erro}") from erro
        texto = _limpar_resposta(conteudo)
        if not texto:
            raise LLMIndisponivel("O modelo de linguagem retornou uma resposta vazia.")
        return texto

    def disponivel(self) -> bool:
        """Verifica se o servidor responde e, no Ollama, se o modelo já foi baixado."""
        try:
            if self.api == "ollama":
                resposta = httpx.get(f"{self.base_url}/api/tags", timeout=3.0)
                if resposta.status_code != 200:
                    return False
                nomes = {m.get("name", "") for m in resposta.json().get("models", [])}
                alvo = self.modelo if ":" in self.modelo else f"{self.modelo}:latest"
                return alvo in nomes or self.modelo in nomes
            resposta = httpx.get(f"{self.base_url}/models", headers=self._cabecalhos(), timeout=3.0)
            return resposta.status_code == 200
        except (httpx.HTTPError, ValueError):
            return False


def criar_cliente_llm(config: Config) -> ClienteLLM:
    return ClienteLLM(
        api=config.llm_api,
        base_url=config.llm_base_url,
        modelo=config.llm_modelo,
        api_key=config.llm_api_key,
        timeout=config.llm_timeout,
        num_ctx=config.llm_num_ctx,
    )
