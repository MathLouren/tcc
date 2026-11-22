"""Serviço que reúne índice, embeddings, LLM, motor RAG e registros.

Na inicialização, carrega o índice salvo em indice/; se ele não existir ou tiver sido
gerado com outro modelo de embeddings, indexa os documentos automaticamente.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import replace
from pathlib import Path

from .config import Config, Parametros, carregar_config
from .docling_pdf import converter_pdf_docling
from .embeddings import criar_embedder
from .indexacao import carregar_indice, carregar_manifesto, construir_indice
from .llm import criar_cliente_llm
from .rag import MotorRAG, chave_de_cache
from .registros import Registro
from .reranker import criar_reranker

logger = logging.getLogger(__name__)


class Servico:
    def __init__(self, config: Config | None = None, embedder=None, llm=None):
        self.config = config or carregar_config()
        self.parametros = Parametros.carregar(self.config.parametros_arquivo)
        self.embedder = embedder if embedder is not None else criar_embedder(self.config)
        self.llm = llm if llm is not None else criar_cliente_llm(self.config)
        self.registro = Registro(self.config.registros_dir)
        self._lock = threading.RLock()

        carregado = carregar_indice(self.config)
        if carregado is None:
            logger.info("Índice ausente ou desatualizado: indexando os documentos de %s.", self.config.docs_dir)
            self.indice, self.info = construir_indice(self.config, self.embedder)
        else:
            self.indice, self.info = carregado
        self.motor = MotorRAG(self.indice, self.embedder, self.llm, self.parametros, criar_reranker(self.config))

    def aquecer_llm_em_segundo_plano(self) -> None:
        """Carrega o modelo (e o reranker, se houver) ao iniciar o servidor, sem atrasar a subida da API."""
        reranker = self.motor.reranker
        if reranker is not None:
            def carregar_reranker() -> None:
                try:
                    reranker.pontuar("aquecimento", ["aquecimento"])
                except Exception:  # noqa: BLE001 - a busca segue sem reranker (ver MotorRAG._reordenar)
                    logger.exception("Não foi possível carregar o reranker %s", reranker.modelo)

            threading.Thread(target=carregar_reranker, name="aquecer-reranker", daemon=True).start()
        aquecer = getattr(self.llm, "aquecer", None)
        if aquecer is None:
            return
        sistema = self.motor.montar_mensagens("", [], [])[0]["content"]
        threading.Thread(target=aquecer, args=(sistema,), name="aquecer-llm", daemon=True).start()

    # ------------------------------------------------------------ pré-aquecimento do cache
    def perguntas_frequentes(self, limite: int) -> list[str]:
        """As perguntas mais repetidas pelos alunos que o modelo respondeu (empate: a mais recente)."""
        contagem: Counter = Counter()
        ultima: dict[str, tuple[int, str]] = {}
        for n, registro in enumerate(self.registro._ler(self.registro.arq_interacoes)):
            pergunta = str(registro.get("pergunta") or "").strip()
            chave = chave_de_cache(pergunta)
            # Só perguntas que o modelo respondeu sem depender da conversa nem de esclarecimento.
            if registro.get("modo") not in {"llm", "citacao"} or len(chave) < 8:
                continue
            contagem[chave] += 1
            ultima[chave] = (n, pergunta)
        ordem = sorted(contagem, key=lambda c: (-contagem[c], -ultima[c][0]))
        return [ultima[c][1] for c in ordem[:limite]]

    def aquecer_cache(self) -> int:
        """Responde as perguntas frequentes para que fiquem no cache; devolve quantas foram respondidas."""
        feitas = 0
        for pergunta in self.perguntas_frequentes(self.config.cache_aquecer):
            try:
                resposta = self.motor.responder(pergunta)  # sem sessão: nada vai para os registros
            except Exception:  # noqa: BLE001 - o aquecimento nunca derruba o servidor
                logger.exception("Falha ao aquecer o cache com %r", pergunta)
                continue
            if resposta.modo == "extrativo":  # modelo fora do ar: tenta de novo na próxima mudança
                logger.warning("Aquecimento do cache interrompido: modelo indisponível.")
                break
            feitas += 1
        return feitas

    def aquecer_cache_em_segundo_plano(self, intervalo: float = 60.0) -> None:
        """Reaquece o cache sempre que ele é invalidado (subida, reindexação, parâmetros ou novo dia)."""
        if self.config.cache_aquecer <= 0:
            return

        def laco() -> None:
            aquecida = None
            while True:
                versao = self.motor.versao_do_cache()
                if versao != aquecida:
                    inicio = time.perf_counter()
                    n = self.aquecer_cache()
                    logger.info("Cache aquecido com %d perguntas frequentes em %.1f s.", n, time.perf_counter() - inicio)
                    aquecida = versao
                time.sleep(intervalo)

        threading.Thread(target=laco, name="aquecer-cache", daemon=True).start()

    def reindexar(self) -> dict:
        """RF09: reprocessa a base de conhecimento sem reiniciar a aplicação."""
        with self._lock:
            indice, info = construir_indice(self.config, self.embedder)
            self.indice, self.info = indice, info
            self.motor.indice = indice
        return info

    def atualizar_parametros(self, alteracoes: dict) -> Parametros:
        """RF10: ajusta top_k, limiar, temperatura, histórico e prompt; persiste em parametros.json."""
        with self._lock:
            novos = replace(self.parametros, **alteracoes)
            novos.validar()
            novos.salvar(self.config.parametros_arquivo)
            self.parametros = novos
            self.motor.parametros = novos
        return novos

    @staticmethod
    def _nome_seguro(nome: str) -> str:
        base = unicodedata.normalize("NFKD", Path(nome).stem)
        base = base.encode("ascii", "ignore").decode("ascii").lower()
        base = re.sub(r"[^a-z0-9]+", "_", base).strip("_")
        return base[:100] or "documento"

    def adicionar_pdf(
        self,
        caminho_temporario: Path,
        nome_original: str,
        titulo: str = "",
        url: str = "",
        categoria: str = "documento",
        atualizado_em: str = "",
    ) -> dict:
        """Converte um PDF com Docling, persiste os artefatos e reindexa a base."""
        markdown, estruturado = converter_pdf_docling(caminho_temporario)
        nome_base = self._nome_seguro(nome_original)
        arquivo_md = f"{nome_base}.md"
        arquivo_json = f"{nome_base}.docling.json"
        arquivo_pdf = f"{nome_base}.pdf"

        with self._lock:
            self.config.docs_dir.mkdir(parents=True, exist_ok=True)
            self.config.originais_dir.mkdir(parents=True, exist_ok=True)
            self.config.docling_dir.mkdir(parents=True, exist_ok=True)
            destino_md = self.config.docs_dir / arquivo_md
            destino_json = self.config.docling_dir / arquivo_json
            destino_pdf = self.config.originais_dir / arquivo_pdf
            existentes = [p.name for p in (destino_md, destino_json, destino_pdf) if p.exists()]
            if existentes:
                raise FileExistsError(f"Já existe um documento com esse nome: {', '.join(existentes)}")

            temporario_manifesto = self.config.manifesto.with_suffix(".json.tmp")
            manifesto_anterior = (
                self.config.manifesto.read_bytes() if self.config.manifesto.exists() else None
            )
            try:
                shutil.copy2(caminho_temporario, destino_pdf)
                destino_md.write_text(markdown + "\n", encoding="utf-8")
                destino_json.write_text(
                    json.dumps(estruturado, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                manifesto = {"documentos": []}
                if manifesto_anterior is not None:
                    carregado = json.loads(manifesto_anterior.decode("utf-8"))
                    manifesto = carregado if isinstance(carregado, dict) else {"documentos": carregado}
                documentos = manifesto.setdefault("documentos", [])
                documentos.append({
                    "arquivo": arquivo_md,
                    "titulo": titulo.strip() or Path(nome_original).stem,
                    "origem": "Upload administrativo processado com Docling",
                    "url": url.strip(),
                    "atualizado_em": atualizado_em.strip(),
                    "categoria": categoria.strip() or "documento",
                    "extrator": "docling",
                    "original": f"uploads/{arquivo_pdf}",
                    "estrutura_json": arquivo_json,
                })
                temporario_manifesto.write_text(
                    json.dumps(manifesto, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporario_manifesto.replace(self.config.manifesto)

                indice, info = construir_indice(self.config, self.embedder)
            except Exception:
                temporario_manifesto.unlink(missing_ok=True)
                destino_md.unlink(missing_ok=True)
                destino_json.unlink(missing_ok=True)
                destino_pdf.unlink(missing_ok=True)
                if manifesto_anterior is None:
                    self.config.manifesto.unlink(missing_ok=True)
                else:
                    self.config.manifesto.write_bytes(manifesto_anterior)
                raise

            self.indice, self.info = indice, info
            self.motor.indice = indice

        relatorio = self.relatorio_do_documento(arquivo_md, paginas=self._paginas(estruturado))
        self._gravar_relatorio(arquivo_md, relatorio)
        return {
            "arquivo_original": f"uploads/{arquivo_pdf}",
            "arquivo_markdown": arquivo_md,
            "arquivo_json": arquivo_json,
            "arquivo_blocos": f"{nome_base}.jsonl",
            "relatorio": relatorio,
            "titulo": titulo.strip() or Path(nome_original).stem,
            "n_documentos": info["n_documentos"],
            "n_blocos": info["n_blocos"],
            "data_indexacao": info["data_indexacao"],
        }

    # ------------------------------------------------------------ qualidade da ingestão
    @staticmethod
    def _paginas(estruturado: dict) -> int:
        paginas = estruturado.get("pages") if isinstance(estruturado, dict) else None
        return len(paginas) if isinstance(paginas, (dict, list)) else 0

    def relatorio_do_documento(self, arquivo: str, paginas: int = 0) -> dict:
        """Números da conversão e avisos que indicam extração ruim, para o administrador conferir."""
        blocos = [b for b in self.indice.blocos if b.get("arquivo") == arquivo]
        caminho = self.config.docs_dir / arquivo
        caracteres = len(caminho.read_text(encoding="utf-8", errors="ignore")) if caminho.is_file() else 0
        secoes = list(dict.fromkeys(b["secao"] for b in blocos))
        tokens = [self.embedder.contar_tokens(b["texto"]) for b in blocos]
        tabelas = sum(1 for b in blocos if " | " in b["texto"])
        avisos = []
        if not blocos:
            avisos.append("Nenhum bloco foi gerado: o documento não será usado nas respostas.")
        if paginas and caracteres / paginas < 300:
            avisos.append(
                "Pouco texto por página: o PDF parece digitalizado e depende de OCR. Confira o conteúdo extraído."
            )
        sem_titulo = sum(1 for b in blocos if b["secao"] == "Geral")
        if blocos and sem_titulo / len(blocos) > 0.5:
            avisos.append("A maior parte do texto ficou sem título de seção: as fontes das respostas ficarão genéricas.")
        if secoes and len(blocos) / len(secoes) > 12:
            avisos.append("Seções muito longas: o documento tem poucos títulos reconhecidos.")
        return {
            "paginas": paginas,
            "caracteres": caracteres,
            "secoes": len(secoes),
            "blocos": len(blocos),
            "blocos_com_tabela": tabelas,
            "tokens": int(sum(tokens)),
            "tokens_por_bloco": int(sum(tokens) / len(tokens)) if tokens else 0,
            "avisos": avisos,
        }

    def _gravar_relatorio(self, arquivo: str, relatorio: dict) -> None:
        with self._lock:
            if not self.config.manifesto.exists():
                return
            manifesto = json.loads(self.config.manifesto.read_text(encoding="utf-8"))
            for doc in (manifesto.get("documentos", []) if isinstance(manifesto, dict) else manifesto):
                if isinstance(doc, dict) and doc.get("arquivo") == arquivo:
                    doc["ingestao"] = relatorio
            self.config.manifesto.write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")

    def _documento(self, arquivo: str) -> tuple[Path, dict]:
        if not arquivo or arquivo != Path(arquivo).name or not (self.config.docs_dir / arquivo).is_file():
            raise FileNotFoundError("Documento não encontrado.")
        return self.config.docs_dir / arquivo, carregar_manifesto(self.config.manifesto).get(arquivo, {})

    def conteudo_do_documento(self, arquivo: str) -> dict:
        """O que foi extraído do documento: relatório, seções e blocos, como o chatbot os consulta."""
        _, meta = self._documento(arquivo)
        blocos = [b for b in self.indice.blocos if b.get("arquivo") == arquivo]
        relatorio = self.relatorio_do_documento(arquivo, paginas=(meta.get("ingestao") or {}).get("paginas", 0))
        return {
            "arquivo": arquivo,
            "titulo": meta.get("titulo") or Path(arquivo).stem,
            "relatorio": relatorio,
            "formatos": [f for f, c in self._arquivos_do_documento(arquivo, meta).items() if c.is_file()],
            "blocos": [
                {"id": b["id"], "secao": b["secao"], "texto": b["texto"],
                 "contexto": b.get("contexto") or b.get("tabela") or "",
                 "tokens": self.embedder.contar_tokens(b["texto"])}
                for b in blocos
            ],
        }

    def _arquivos_do_documento(self, arquivo: str, meta: dict) -> dict[str, Path]:
        caminhos = {
            "markdown": self.config.docs_dir / arquivo,
            "blocos": self.config.indice_dir / "blocos" / f"{Path(arquivo).stem}.jsonl",
        }
        if meta.get("estrutura_json"):
            caminhos["json"] = self.config.docling_dir / Path(meta["estrutura_json"]).name
        if str(meta.get("original", "")).startswith("uploads/"):
            caminhos["pdf"] = self.config.originais_dir / Path(meta["original"]).name
        return caminhos

    def arquivo_para_download(self, arquivo: str, formato: str) -> Path:
        _, meta = self._documento(arquivo)
        caminho = self._arquivos_do_documento(arquivo, meta).get(formato)
        if caminho is None or not caminho.is_file():
            raise FileNotFoundError("Formato não disponível para este documento.")
        return caminho

    def testar_busca(self, pergunta: str) -> dict:
        """Mostra o que a pergunta recupera, sem chamar o modelo: útil para conferir um documento novo."""
        exata = self.motor._resposta_factual_da_matriz(pergunta)
        trechos = self.motor.recuperar(pergunta)
        return {
            "resposta_exata": exata[0] if exata else None,
            "esclarecimentos": self.motor._campos_para_esclarecer(pergunta),
            "trechos": [
                {"documento": t["documento"], "arquivo": t["arquivo"], "secao": t["secao"],
                 "similaridade": t["similaridade"], "texto": t["texto"]}
                for t in trechos
            ],
        }

    def listar_documentos(self) -> list[dict]:
        """Documentos da base (arquivos em docs_dir), com metadados do manifesto."""
        from .indexacao import EXTENSOES_SUPORTADAS

        manifesto = carregar_manifesto(self.config.manifesto)
        itens = []
        if self.config.docs_dir.exists():
            for caminho in sorted(self.config.docs_dir.iterdir()):
                if not caminho.is_file() or caminho.suffix.lower() not in EXTENSOES_SUPORTADAS:
                    continue
                meta = manifesto.get(caminho.name, {})
                itens.append({
                    "arquivo": caminho.name,
                    "titulo": meta.get("titulo") or caminho.stem,
                    "categoria": meta.get("categoria", ""),
                    "atualizado_em": meta.get("atualizado_em", ""),
                    "origem": meta.get("origem", ""),
                    "url": meta.get("url", ""),
                    "tamanho_kb": round(caminho.stat().st_size / 1024, 1),
                    "blocos": sum(1 for b in self.indice.blocos if b.get("arquivo") == caminho.name),
                    "avisos": len((meta.get("ingestao") or {}).get("avisos", [])),
                })
        return itens

    def remover_documento(self, arquivo: str) -> dict:
        """Remove o documento (e seus artefatos Docling/original), atualiza o manifesto e reindexa."""
        if arquivo != Path(arquivo).name or not arquivo:
            raise FileNotFoundError("Documento não encontrado.")
        with self._lock:
            alvo = self.config.docs_dir / arquivo
            if not alvo.is_file():
                raise FileNotFoundError("Documento não encontrado.")

            manifesto_bytes = self.config.manifesto.read_bytes() if self.config.manifesto.exists() else None
            entrada: dict = {}
            if manifesto_bytes is not None:
                carregado = json.loads(manifesto_bytes.decode("utf-8"))
                manifesto = carregado if isinstance(carregado, dict) else {"documentos": carregado}
                restantes = []
                for doc in manifesto.get("documentos", []):
                    if doc.get("arquivo") == arquivo:
                        entrada = doc
                    else:
                        restantes.append(doc)
                manifesto["documentos"] = restantes
                self.config.manifesto.write_text(
                    json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            # Artefatos associados: só dentro das pastas configuradas (sem path traversal).
            associados = []
            if entrada.get("estrutura_json"):
                associados.append(self.config.docling_dir / Path(entrada["estrutura_json"]).name)
            if entrada.get("original", "").startswith("uploads/"):
                associados.append(self.config.originais_dir / Path(entrada["original"]).name)
            backups = {}
            try:
                for caminho in [alvo, *associados]:
                    if caminho.is_file():
                        backups[caminho] = caminho.read_bytes()
                        caminho.unlink()
                indice, info = construir_indice(self.config, self.embedder)
            except Exception:
                for caminho, conteudo in backups.items():
                    caminho.write_bytes(conteudo)
                if manifesto_bytes is not None:
                    self.config.manifesto.write_bytes(manifesto_bytes)
                raise
            self.indice, self.info = indice, info
            self.motor.indice = indice
        return {
            "arquivo": arquivo,
            "n_documentos": info["n_documentos"],
            "n_blocos": info["n_blocos"],
            "data_indexacao": info["data_indexacao"],
        }
