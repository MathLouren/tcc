"""Cliente do modelo de linguagem (seção 3.6 do TCC).

Por padrão usa a API nativa do Ollama (/api/chat), que permite definir o tamanho da
janela de contexto (num_ctx). Com LLM_API=openai, usa qualquer API compatível com o
padrão OpenAI (/chat/completions), inclusive a do próprio Ollama em /v1 (RNF05).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

import httpx

from .config import Config

logger = logging.getLogger(__name__)


SEMENTE = 7
MAX_TOKENS_RESPOSTA = 700  # teto padrão (LLM_MAX_TOKENS): uma resposta normal tem de 50 a 250 tokens


class LLMIndisponivel(RuntimeError):
    """O modelo de linguagem não respondeu; o motor RAG usa o modo extrativo."""


class _SemPrimeiroToken(httpx.TimeoutException):
    """O modelo não começou a responder no prazo: vale tentar o próximo modelo da lista."""


def _limpar_resposta(texto: str | None) -> str:
    texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.DOTALL)  # modelos com raciocínio
    return texto.strip()


class ClienteLLM:
    def __init__(self, api: str, base_url: str, modelo: str, api_key: str = "",
                 timeout: float = 120.0, num_ctx: int = 8192, keep_alive: str = "2h", num_gpu: int = -1,
                 modelos_reserva: tuple[str, ...] = (), timeout_primeiro_token: float = 0.0,
                 max_tokens: int = MAX_TOKENS_RESPOSTA):
        self.api = api
        self.base_url = base_url.rstrip("/")
        self.modelo = modelo
        self.api_key = api_key
        self.timeout = timeout
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self.num_gpu = num_gpu
        # API OpenAI: se o modelo principal falhar ou não começar a responder em timeout_primeiro_token
        # segundos, a mesma pergunta vai para o próximo modelo da lista (0 = só o timeout geral).
        self.modelos_reserva = tuple(m for m in modelos_reserva if m and m != modelo)
        self.timeout_primeiro_token = timeout_primeiro_token
        self.max_tokens = max_tokens
        # Métricas das chamadas feitas pela thread atual (cada pergunta roda em uma thread).
        self._local = threading.local()

    # ------------------------------------------------------------ métricas por pergunta
    def iniciar_metricas(self) -> None:
        self._local.chamadas = []

    def consumir_metricas(self) -> list[dict]:
        """Chamadas feitas desde iniciar_metricas(): modelo, provedor, tempos e se usou a reserva."""
        chamadas = getattr(self._local, "chamadas", None) or []
        self._local.chamadas = []
        return chamadas

    def _anotar(self, **chamada) -> None:
        if getattr(self._local, "chamadas", None) is None:
            self._local.chamadas = []
        self._local.chamadas.append(chamada)

    def _cabecalhos(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _chat_ollama(self, mensagens: list[dict], opcoes: dict, ao_token=None) -> str:
        """Chama /api/chat e devolve o texto. Com ao_token, recebe a resposta em partes (stream)."""
        def enviar(opcoes_finais: dict) -> str | None:
            corpo = {"model": self.modelo, "messages": mensagens, "stream": ao_token is not None,
                     "keep_alive": self.keep_alive, "options": {"num_ctx": self.num_ctx, **opcoes_finais}}
            with httpx.stream("POST", f"{self.base_url}/api/chat", json=corpo, timeout=self.timeout) as resposta:
                if resposta.status_code >= 500 and "num_gpu" in opcoes_finais:
                    resposta.read()
                    # Sem memória de vídeo para todas as camadas: volta à divisão automática do Ollama.
                    logger.warning("Ollama recusou LLM_NUM_GPU=%s (%s); usando o ajuste automático.",
                                   self.num_gpu, resposta.text[:200])
                    return None
                resposta.raise_for_status()
                if ao_token is None:
                    resposta.read()
                    return resposta.json()["message"]["content"]
                partes = []
                for linha in resposta.iter_lines():
                    if not linha.strip():
                        continue
                    evento = json.loads(linha)
                    if evento.get("error"):
                        raise ValueError(evento["error"])
                    pedaco = (evento.get("message") or {}).get("content") or ""
                    if pedaco:
                        partes.append(pedaco)
                        ao_token(pedaco)
                return "".join(partes)

        if self.num_gpu >= 0:
            texto = enviar({**opcoes, "num_gpu": self.num_gpu})
            if texto is not None:
                return texto
            self.num_gpu = -1
        return enviar(opcoes)

    def aquecer(self, mensagem_sistema: str) -> None:
        """Carrega o modelo e deixa processada a parte fixa do prompt, para a primeira pergunta não esperar."""
        if self.api != "ollama":
            return
        try:
            self._chat_ollama([{"role": "system", "content": mensagem_sistema}, {"role": "user", "content": "oi"}],
                              {"temperature": 0.0, "num_predict": 1})
            logger.info("Modelo %s carregado e prompt fixo processado.", self.modelo)
        except (httpx.HTTPError, ValueError) as erro:
            logger.warning("Não foi possível aquecer o modelo %s: %s", self.modelo, erro)

    def _chat_openai(self, modelo: str, mensagens: list[dict], temperatura: float, ao_token=None) -> tuple[str, dict]:
        """Chama /chat/completions sempre em stream: o texto chega aos poucos e dá para medir o 1º token.

        Devolve (texto, informações da chamada). Sem nenhum pedaço de texto em timeout_primeiro_token
        segundos, levanta _SemPrimeiroToken (o modelo está numa fila do provedor).
        """
        corpo = {"model": modelo, "messages": mensagens, "temperature": temperatura,
                 "max_tokens": self.max_tokens, "stream": True}
        inicio = time.perf_counter()
        prazo_primeiro = self.timeout_primeiro_token or self.timeout
        espera = httpx.Timeout(self.timeout, connect=10.0, read=min(self.timeout, prazo_primeiro))
        partes: list[str] = []
        info: dict = {"modelo": modelo}
        with httpx.stream("POST", f"{self.base_url}/chat/completions", headers=self._cabecalhos(),
                          json=corpo, timeout=espera) as resposta:
            resposta.raise_for_status()
            for linha in resposta.iter_lines():
                agora = time.perf_counter() - inicio
                if not partes and agora > prazo_primeiro:
                    raise _SemPrimeiroToken(f"{modelo} não respondeu em {prazo_primeiro:.0f} s")
                if agora > self.timeout:
                    raise httpx.ReadTimeout(f"{modelo} passou de {self.timeout:.0f} s")
                # Linhas que começam com ":" são comentários de keep-alive (ex.: OPENROUTER PROCESSING).
                if not linha.startswith("data:"):
                    continue
                dado = linha[5:].strip()
                if dado == "[DONE]":
                    break
                evento = json.loads(dado)
                if evento.get("error"):
                    raise ValueError(str(evento["error"]))
                info["provedor"] = evento.get("provider") or info.get("provedor")
                if evento.get("usage"):
                    info["tokens"] = evento["usage"].get("completion_tokens")
                for escolha in evento.get("choices") or []:
                    pedaco = (escolha.get("delta") or {}).get("content") or ""
                    if pedaco:
                        if not partes:
                            info["primeiro_token_ms"] = int((time.perf_counter() - inicio) * 1000)
                        partes.append(pedaco)
                        if ao_token is not None:
                            ao_token(pedaco)
        return "".join(partes), info

    def gerar(self, mensagens: list[dict], temperatura: float, ao_token=None) -> str:
        """Gera a resposta. ao_token(texto), se informado, recebe cada parte assim que o modelo a produz."""
        inicio = time.perf_counter()
        if self.api == "ollama":
            try:
                # Semente fixa: a mesma pergunta com os mesmos trechos gera sempre a mesma resposta.
                opcoes = {"temperature": temperatura, "seed": SEMENTE, "num_predict": self.max_tokens}
                conteudo = self._chat_ollama(mensagens, opcoes, ao_token)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as erro:
                self._anotar(modelo=self.modelo, erro=type(erro).__name__,
                             tempo_ms=int((time.perf_counter() - inicio) * 1000))
                raise LLMIndisponivel(f"Falha ao consultar o modelo de linguagem: {erro}") from erro
            self._anotar(modelo=self.modelo, tempo_ms=int((time.perf_counter() - inicio) * 1000))
        else:
            conteudo = None
            falhas = []
            modelos = (self.modelo, *self.modelos_reserva)
            for n, modelo in enumerate(modelos):
                emitiu = []
                def repassar(pedaco, _emitiu=emitiu):
                    _emitiu.append(pedaco)
                    ao_token(pedaco)
                t0 = time.perf_counter()
                try:
                    conteudo, info = self._chat_openai(modelo, mensagens, temperatura,
                                                       repassar if ao_token is not None else None)
                except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as erro:
                    falhas.append(f"{modelo}: {erro}")
                    self._anotar(modelo=modelo, erro=type(erro).__name__, reserva=n > 0,
                                 tempo_ms=int((time.perf_counter() - t0) * 1000))
                    logger.warning("Modelo %s falhou (%s)%s", modelo, erro,
                                   "; tentando o próximo da lista" if not emitiu and n + 1 < len(modelos) else "")
                    if emitiu:  # o aluno já viu parte do texto: não mistura com a resposta de outro modelo
                        break
                    continue
                self._anotar(**info, reserva=n > 0, tempo_ms=int((time.perf_counter() - t0) * 1000))
                break
            if conteudo is None:
                raise LLMIndisponivel("Falha ao consultar o modelo de linguagem: " + " | ".join(falhas))
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
        keep_alive=config.llm_keep_alive,
        num_gpu=config.llm_num_gpu,
        modelos_reserva=config.llm_modelos_reserva,
        timeout_primeiro_token=config.llm_timeout_primeiro_token,
        max_tokens=config.llm_max_tokens,
    )
