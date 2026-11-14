"""Busca lexical (BM25) que complementa a busca vetorial.

Os embeddings aproximam o assunto, mas erram quando a resposta depende de uma palavra exata
("vestibular", "reitora", "jaleco"). O BM25 pontua os blocos pelas palavras da pergunta, e o motor
RAG soma essa nota à similaridade vetorial (busca híbrida). Sem dependências além do NumPy.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

import numpy as np

_K1 = 1.4
_B = 0.6
_PESO_PREFIXO = 0.5  # o radical (5 letras) vale menos que a palavra exata
_TAMANHO_PREFIXO = 5

_PALAVRAS_VAZIAS = frozenset("""
a o as os um uma uns umas de da do das dos em na no nas nos por para pra pro pela pelo pelas pelos com sem
que qual quais quem quando quanto quanta quantos quantas como onde e ou mas se eu me meu minha meus minhas
voce tu te seu sua seus suas ele ela eles elas nos isso isto esse essa este esta aquele aquela ao aos
ser sou e eh sao foi era estar esta estou estao tem tenho ter temos ha havera fazer faco faz posso pode
podem preciso precisa devo deve vou vai ja nao sim so tambem muito mais menos sobre ate entre mesmo la
ai entao the of oi ola bom dia tarde noite favor gostaria queria saber dizer informar existe algum alguma
""".split())

# Como o aluno escreve -> como os documentos escrevem. Só amplia a consulta lexical.
_SINONIMOS: dict[str, tuple[str, ...]] = {
    "materia": ("disciplina",), "materias": ("disciplinas", "disciplina"), "cadeira": ("disciplina",),
    "faculdade": ("universidade", "universo", "instituicao"), "facul": ("universidade", "universo"),
    "prova": ("avaliacao", "verificacao"), "provas": ("avaliacoes", "verificacoes", "avaliacao"),
    "faltas": ("frequencia",), "falta": ("frequencia",), "faltar": ("frequencia",), "presenca": ("frequencia",),
    "boleto": ("parcela", "pagamento"), "mensalidade": ("semestralidade", "parcela", "parcelas"),
    "devendo": ("debito", "inadimplemento", "debitos"), "divida": ("debito", "inadimplemento"),
    "inadimplente": ("debito", "inadimplemento"),
    "formar": ("colacao", "grau", "integralizacao"), "formatura": ("colacao", "grau"),
    "livro": ("obra", "bibliografico", "livros"), "livros": ("obras", "bibliografico"),
    "zap": ("whatsapp",), "wpp": ("whatsapp",), "telefone": ("0800", "numero"), "contato": ("0800", "fale"),
    "tcc": ("trabalho", "conclusao"), "monografia": ("tcc", "conclusao"),
    "ead": ("distancia",), "online": ("distancia", "ead"), "pos": ("lato", "stricto", "especializacao"),
    "passar": ("aprovado", "aprovacao"), "passo": ("aprovado", "aprovacao"), "passei": ("aprovado",),
    "reprovar": ("reprovado",), "reprovo": ("reprovado",), "reprovei": ("reprovado",), "bombar": ("reprovado",),
    "recuperacao": ("r1", "suplementar"), "final": ("suplementar", "vs"),
    "trancar": ("trancamento",), "tranco": ("trancamento",), "destrancar": ("reabertura", "reintegracao"),
    "voltar": ("reabertura", "reintegracao"), "transferir": ("transferencia",), "mudar": ("transferencia",),
    "nota": ("media", "notas"), "notas": ("media", "nota"), "media": ("nota",),
    "reitor": ("reitora",), "diretor": ("diretora",), "dono": ("reitora", "dirigentes"),
    "endereco": ("rua", "avenida"), "fica": ("rua", "avenida", "endereco"), "localizacao": ("rua", "endereco"),
    "gravida": ("gestante",), "gravidez": ("gestante",), "doente": ("doenca", "atestado", "medico"),
    "atestado": ("medico", "licenca"), "bolsa": ("bolsas", "desconto"), "desconto": ("bolsas",),
    "entrar": ("ingresso",), "ingressar": ("ingresso",), "inscricao": ("ingresso", "seletivo"),
    "punicao": ("sancoes", "penalidade"), "punicoes": ("sancoes", "penalidades", "advertencia"),
    "castigo": ("sancoes", "penalidade"), "expulso": ("desligamento",), "expulsao": ("desligamento",),
    "perdi": ("perda", "extravio", "perder"), "achei": ("achados", "encontrados"),
    "emprestado": ("emprestimo",), "pegar": ("emprestimo", "retirada"), "devolver": ("devolucao",),
    "atraso": ("atrasar", "multa"), "duracao": ("integralizacao", "tempo"), "dura": ("integralizacao", "tempo"),
    "anos": ("integralizacao",), "horario": ("horarios", "funcionamento"), "abre": ("funcionamento", "horario"),
    "app": ("aplicativo",), "aplicativo": ("app",), "site": ("portal", "www"), "estagio": ("estagios",),
    "antes": ("antecipar", "abreviacao"), "adiantar": ("antecipar", "abreviacao"),
    "vagas": ("vagas", "anuais"), "requisito": ("requisitos", "prende"), "certificado": ("certificados",),
    "comer": ("alimento", "alimentos", "consumo"), "lanchar": ("alimento", "consumo"),
    "celular": ("telefone", "eletronicos", "objetos"), "roupa": ("trajes", "vestimenta", "jaleco"),
    "psicologo": ("psicopedagogico", "naps"), "ajuda": ("apoio",), "revisao": ("revisao", "recurso"),
    "recorrer": ("recurso",), "trote": ("trotes", "calouradas"), "segunda": ("2a",), "primeira": ("1a",),
}

_RE_TOKEN = re.compile(r"[a-z0-9]+")
_RE_ORDINAL = re.compile(r"\b(\d+)\s*[ªº°]")


def _normalizar(texto: str) -> str:
    texto = _RE_ORDINAL.sub(r"\1a", str(texto).lower())  # "2ª chamada" e "segunda chamada" casam por "2a"
    texto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in texto if not unicodedata.combining(c))


def _singular(token: str) -> str:
    if len(token) <= 3 or token.isdigit():
        return token
    for sufixo, troca in (("oes", "ao"), ("aes", "ao"), ("ais", "al"), ("eis", "el"), ("ns", "m")):
        if token.endswith(sufixo):
            return token[: -len(sufixo)] + troca
    if token.endswith("es") and len(token) > 5 and token[-3] in "rzs":
        return token[:-2]
    return token[:-1] if token.endswith("s") and not token.endswith("ss") else token


def termos(texto: str, expandir: bool = False) -> list[tuple[str, float]]:
    """Termos ponderados do texto: palavra no singular (peso 1) e radical de 5 letras (peso 0,5)."""
    saida: list[tuple[str, float]] = []
    vistos_na_expansao: set[str] = set()
    for bruto in _RE_TOKEN.findall(_normalizar(texto)):
        if bruto in _PALAVRAS_VAZIAS or (len(bruto) < 2 and not bruto.isdigit()):
            continue
        candidatos = [(bruto, 1.0)]
        if expandir:
            for sinonimo in _SINONIMOS.get(bruto, ()) + _SINONIMOS.get(_singular(bruto), ()):
                if sinonimo not in vistos_na_expansao:
                    vistos_na_expansao.add(sinonimo)
                    candidatos.append((sinonimo, 0.6))
        for token, peso in candidatos:
            token = _singular(token)
            saida.append((token, peso))
            if len(token) > _TAMANHO_PREFIXO and not token[0].isdigit():
                saida.append(("~" + token[:_TAMANHO_PREFIXO], peso * _PESO_PREFIXO))
    return saida


class IndiceLexical:
    """BM25 sobre o cabeçalho (documento e seção) e o texto de cada bloco."""

    def __init__(self, textos: list[str]):
        self.n = len(textos)
        self._frequencias: list[Counter] = []
        tamanhos = []
        documentos_por_termo: Counter = Counter()
        for texto in textos:
            contagem: Counter = Counter()
            for termo, peso in termos(texto):
                contagem[termo] += peso
            self._frequencias.append(contagem)
            tamanhos.append(sum(contagem.values()))
            documentos_por_termo.update(contagem.keys())
        self._tamanhos = np.asarray(tamanhos, dtype="float32")
        self._media = float(self._tamanhos.mean()) if self.n else 0.0
        self._idf = {
            termo: math.log(1.0 + (self.n - df + 0.5) / (df + 0.5)) for termo, df in documentos_por_termo.items()
        }

    def pontuar(self, consulta: str) -> np.ndarray:
        """Nota BM25 de cada bloco para a consulta (0 quando nenhuma palavra coincide)."""
        notas = np.zeros(self.n, dtype="float32")
        if not self.n:
            return notas
        pesos: dict[str, float] = {}
        for termo, peso in termos(consulta, expandir=True):
            pesos[termo] = max(pesos.get(termo, 0.0), peso)
        for termo, peso in pesos.items():
            idf = self._idf.get(termo)
            if idf is None:
                continue
            for i, contagem in enumerate(self._frequencias):
                f = contagem.get(termo)
                if f:
                    norma = _K1 * (1.0 - _B + _B * self._tamanhos[i] / (self._media or 1.0))
                    notas[i] += peso * idf * f * (_K1 + 1.0) / (f + norma)
        return notas

    def linhas_relevantes(self, consulta: str, linhas: list[str]) -> list[float]:
        """Nota de cada linha para a consulta: soma da raridade das palavras da pergunta que a linha contém."""
        pesos: dict[str, float] = {}
        for termo, peso in termos(consulta, expandir=True):
            if termo in self._idf:
                pesos[termo] = max(pesos.get(termo, 0.0), peso * self._idf[termo])
        notas = []
        for linha in linhas:
            presentes = {termo for termo, _ in termos(linha)}
            notas.append(sum(peso for termo, peso in pesos.items() if termo in presentes))
        return notas

    def cobertura(self, consulta: str) -> float:
        """Parcela (ponderada pela raridade) das palavras da consulta que existem em algum bloco.

        Perto de 0, a pergunta não tem nenhuma palavra em comum com a base: está fora do escopo.
        """
        total = achado = 0.0
        for termo, _ in termos(consulta):
            if termo.startswith("~"):
                continue
            idf = self._idf.get(termo)
            raridade = idf if idf is not None else math.log(1.0 + (self.n + 0.5) / 0.5)
            total += raridade
            achado += raridade if idf is not None else 0.0
        return achado / total if total else 0.0

    def tem_termo_especifico(self, consulta: str, parcela_maxima: float = 0.15) -> bool:
        """A consulta cita alguma palavra da base que aparece em poucos blocos (um assunto próprio)?"""
        if not self.n:
            return False
        limite = math.log(1.0 + (self.n - parcela_maxima * self.n + 0.5) / (parcela_maxima * self.n + 0.5))
        return any(
            not termo.startswith("~") and self._idf.get(termo, 0.0) >= limite for termo, _ in termos(consulta)
        )
