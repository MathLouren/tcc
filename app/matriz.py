"""Respostas exatas sobre a matriz curricular, lidas direto das tabelas indexadas.

Período, pré-requisito, carga horária e créditos são dados tabulares: consultá-los no código evita
que o modelo de linguagem leia a coluna errada. Perguntas fora desses padrões seguem para o RAG.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_CURRICULO = "109an"
_ROTULO = "matriz curricular 109AN de Sistemas de Informação"
_RE_CODIGO = re.compile(r"[A-Z]{1,3}\d{2,4}|OPT")
_RE_SECAO_PERIODO = re.compile(r"(\d+)º período", re.IGNORECASE)
_PALAVRAS_VAZIAS = {"de", "da", "do", "das", "dos", "e", "em", "a", "o", "para", "na", "no"}
_ORDINAIS = {
    "primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4,
    "quinto": 5, "sexto": 6, "setimo": 7, "oitavo": 8,
}
_RE_FORA_DO_ASSUNTO = re.compile(r"\b(prova|provas|avaliacao|avaliacoes|nota|notas|quando|data|datas|prazo)\b")
_RE_ASSUNTO_DISCIPLINAS = re.compile(r"\b(disciplinas?|materias?|cadeiras?|componentes?|estud\w+|grade)\b")
_RE_QUANTIDADE = re.compile(r"\b(quantas|quantos|numero|total)\b")
_RE_ENUMERACAO = re.compile(r"\b(quais|liste|listar|lista|cite|citar|mostre|mostrar|relacione|nomes?)\b")
_RE_HORAS = re.compile(r"\b(horas|carga horaria|carga)\b")
_RE_DEPENDENTES = re.compile(r"\b(dependem|prende|prendem|libera|liberam)\b")
_RE_REQUISITO = re.compile(r"\b(pre.?requisitos?|requisitos?|depende|para cursar|para fazer|preciso ter|precisa ter|antes de)\b")
_RE_PERGUNTA_PERIODO = re.compile(r"\b(periodo|semestre)\b")
_RE_CREDITOS = re.compile(r"\bcreditos?\b")
_RE_PERGUNTA_CODIGO = re.compile(r"\b(codigo|disciplina|materia|o que e|qual e|qual)\b")


@dataclass(frozen=True)
class Disciplina:
    codigo: str
    nome: str
    periodo: int
    horas: str
    creditos: str
    requisitos: tuple[str, ...]


def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", str(texto).lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


def _tokens(texto: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", _normalizar(texto)) if t not in _PALAVRAS_VAZIAS]


def blocos_da_matriz(blocos: list[dict]) -> list[dict]:
    """Blocos dos arquivos do currículo; o arquivo pode ter sido enviado com qualquer nome."""
    arquivos = {
        b.get("arquivo") for b in blocos
        if _CURRICULO in " ".join(str(b.get(c, "")) for c in ("arquivo", "secao", "texto")).lower()
    }
    return [b for b in blocos if b.get("arquivo") in arquivos]


def _ler_disciplinas(blocos: list[dict]) -> tuple[list[Disciplina], dict[int, dict]]:
    disciplinas: dict[str, Disciplina] = {}
    por_periodo: dict[int, dict] = {}
    for bloco in blocos:
        secao = str(bloco.get("secao", ""))
        periodo = _RE_SECAO_PERIODO.search(secao)
        if not secao.startswith("Disciplinas por período") or not periodo:
            continue
        numero = int(periodo.group(1))
        por_periodo.setdefault(numero, bloco)
        colunas: list[str] = []
        for linha in str(bloco.get("texto", "")).splitlines():
            celulas = [c.strip() for c in linha.split("|")]
            if _normalizar(celulas[0]) == "codigo":
                colunas = [_normalizar(c) for c in celulas]
                continue
            if len(celulas) < 2 or not _RE_CODIGO.fullmatch(celulas[0]):
                continue

            def campo(nome: str) -> str:
                for i, coluna in enumerate(colunas):
                    if nome in coluna and i < len(celulas):
                        return celulas[i]
                return ""

            disciplinas.setdefault(celulas[0], Disciplina(
                codigo=celulas[0],
                nome=celulas[1],
                periodo=numero,
                horas=campo("carga"),
                creditos=campo("credito"),
                requisitos=tuple(_RE_CODIGO.findall(campo("requisito"))),
            ))
    return list(disciplinas.values()), por_periodo


def _periodo_da_pergunta(normalizada: str) -> int | None:
    if re.search(r"\b20\d{2}\b", normalizada):  # "2º semestre de 2026" é semestre letivo, não período do curso
        return None
    encontrado = (
        re.search(r"\b([1-9])\s*(?:o|°)?\s*(?:periodo|semestre)\b", normalizada)
        or re.search(r"\b(?:periodo|semestre)\s+([1-9])\b", normalizada)
    )
    if encontrado:
        return int(encontrado.group(1))
    ordinal = re.search(rf"\b({'|'.join(_ORDINAIS)})\s+(?:periodo|semestre)\b", normalizada)
    return _ORDINAIS[ordinal.group(1)] if ordinal else None


def _tokens_da_pergunta(normalizada: str) -> list[str]:
    tokens = _tokens(normalizada)
    saida: list[str] = []
    for token in tokens:
        if token == "tcc":
            saida += ["trabalho", "conclusao", "curso"]
        elif saida and token in {"1", "2"}:
            saida.append("i" if token == "1" else "ii")
        else:
            saida.append(token)
    return saida


def _variantes_do_nome(nome: str) -> list[list[str]]:
    """Formas aceitas do nome: completa e sem o sufixo do curso ou a palavra "supervisionado"."""
    tokens = _tokens(nome)
    sem_curso = [t for t in tokens if t not in {"sistemas", "informacao"}] if "informacao" in tokens else tokens
    sem_supervisionado = [t for t in sem_curso if t != "supervisionado"]
    variantes = [tokens]
    for variante in (sem_curso, sem_supervisionado):
        if len(variante) >= 2 and variante not in variantes:
            variantes.append(variante)
    return variantes


def _contem_sequencia(tokens: list[str], sequencia: list[str]) -> bool:
    tamanho = len(sequencia)
    return any(tokens[i:i + tamanho] == sequencia for i in range(len(tokens) - tamanho + 1))


def _disciplina_citada(normalizada: str, disciplinas: list[Disciplina]) -> Disciplina | None:
    por_codigo = {d.codigo.lower(): d for d in disciplinas}
    for token in re.findall(r"[a-z]{1,3}\d{2,4}", normalizada):
        if token in por_codigo:
            return por_codigo[token]
    tokens = _tokens_da_pergunta(normalizada)
    melhor, tamanho_melhor = None, 0
    for disciplina in disciplinas:
        for variante in _variantes_do_nome(disciplina.nome):
            if len(variante) > tamanho_melhor and _contem_sequencia(tokens, variante):
                melhor, tamanho_melhor = disciplina, len(variante)
    return melhor


def _rotulo_disciplina(d: Disciplina) -> str:
    return f"{d.codigo} — {d.nome}"


def _item_lista(d: Disciplina) -> str:
    detalhes = ", ".join(p for p in (d.horas, f"{d.creditos} créditos" if d.creditos else "") if p)
    return f"- {_rotulo_disciplina(d)}" + (f" ({detalhes})" if detalhes else "")


def _resposta_periodo(numero, normalizada, disciplinas, por_periodo, blocos):
    do_periodo = [d for d in disciplinas if d.periodo == numero]
    if not do_periodo or _RE_FORA_DO_ASSUNTO.search(normalizada):
        return None

    if _RE_HORAS.search(normalizada):
        resumo = next((b for b in blocos if str(b.get("secao", "")).startswith("Resumo da carga horária")), None)
        linha = re.search(rf"^{numero}º período\s*\|\s*(.+)$", str((resumo or {}).get("texto", "")), re.MULTILINE)
        if linha:
            celulas = [c.strip() for c in linha.group(1).split("|")]
            if len(celulas) == 4:
                carga, aulas, complementares, total = celulas
                return (
                    f"Na {_ROTULO}, o {numero}º período tem {total}: {carga} h de carga horária de disciplinas "
                    f"e {complementares} h de atividades complementares, com {aulas} aulas semanais.",
                    [resumo],
                )
        return None

    if _RE_QUANTIDADE.search(normalizada):
        lista = "\n".join(_item_lista(d) for d in do_periodo)
        return (
            f"O {numero}º período da {_ROTULO} tem {len(do_periodo)} componentes curriculares:\n{lista}",
            [por_periodo[numero]],
        )

    if _RE_ASSUNTO_DISCIPLINAS.search(normalizada):
        lista = "\n".join(_item_lista(d) for d in do_periodo)
        return f"Disciplinas do {numero}º período na {_ROTULO}:\n{lista}", [por_periodo[numero]]
    return None


def _resposta_disciplina(d, normalizada, disciplinas, por_periodo):
    por_codigo = {x.codigo: x for x in disciplinas}
    bloco = [por_periodo[d.periodo]]

    if _RE_DEPENDENTES.search(normalizada):
        dependentes = [x for x in disciplinas if d.codigo in x.requisitos]
        if not dependentes:
            return f"A {_ROTULO} não indica disciplinas que dependam de {_rotulo_disciplina(d)}.", bloco
        lista = "\n".join(f"- {_rotulo_disciplina(x)} ({x.periodo}º período)" for x in dependentes)
        blocos = [por_periodo[n] for n in dict.fromkeys(x.periodo for x in dependentes)]
        return f"Na {_ROTULO}, dependem de {_rotulo_disciplina(d)}:\n{lista}", blocos

    if _RE_REQUISITO.search(normalizada):
        if not d.requisitos:
            return f"A {_ROTULO} não indica pré-requisito para {_rotulo_disciplina(d)}.", bloco
        lista = "\n".join(
            f"- {_rotulo_disciplina(por_codigo[r])}" if r in por_codigo else f"- {r}" for r in d.requisitos
        )
        return f"Na {_ROTULO}, os pré-requisitos de {_rotulo_disciplina(d)} são:\n{lista}", bloco

    if _RE_CREDITOS.search(normalizada) and d.creditos:
        return f"Na {_ROTULO}, {_rotulo_disciplina(d)} vale {d.creditos} créditos.", bloco
    if _RE_HORAS.search(normalizada) and d.horas:
        return f"Na {_ROTULO}, {_rotulo_disciplina(d)} tem carga horária de {d.horas}.", bloco
    if _RE_PERGUNTA_PERIODO.search(normalizada):
        return f"Na {_ROTULO}, {_rotulo_disciplina(d)} é do {d.periodo}º período.", bloco
    if re.search(r"\b[a-z]{1,3}\d{2,4}\b", normalizada) and _RE_PERGUNTA_CODIGO.search(normalizada):
        return f"Na {_ROTULO}, o código {d.codigo} corresponde a {d.nome} ({d.periodo}º período).", bloco
    return None


_RE_OUTRO_ASSUNTO_DE_HORAS = re.compile(
    r"\b(nivelamento|iniciacao|monitoria|extensao|certificados?|tratamento|material|formacao docente|"
    r"programa especial|pos|licenca|cumprir|biblioteca|periodo)\b"
)
_RE_PEDE_HORAS = re.compile(r"\b(quantas horas|carga horaria|total de horas|horas (?:tem|sao|preciso|precisa)|minimo de horas)\b")
_RE_DURACAO = re.compile(r"\b(quanto tempo|duracao|dura|quantos periodos|quantos semestres|quantos anos)\b")
_RE_CURSO_DA_MATRIZ = re.compile(r"\b(sistemas de informacao|meu curso|do curso|o curso)\b")


def _resposta_totais(normalizada: str, blocos: list[dict], periodos: int):
    """Carga horária total, horas de estágio e de atividades complementares, créditos e duração do curso."""
    if _RE_FORA_DO_ASSUNTO.search(normalizada) or _RE_OUTRO_ASSUNTO_DE_HORAS.search(normalizada):
        return None
    totais = next((b for b in blocos if str(b.get("secao", "")).startswith("Totalização")), None)
    texto = str((totais or {}).get("texto", ""))

    def linha(rotulo: str) -> re.Match | None:
        return re.search(rf"{rotulo}[^\n]*?([\d.]+)\s*h(?:oras)?\s*/\s*(\d+)\s*cr", texto, re.IGNORECASE)

    pede_horas = bool(_RE_PEDE_HORAS.search(normalizada))
    if totais and pede_horas and re.search(r"\batividades? complementar(?:es)?\b", normalizada):
        achado = linha("Atividades Complementares")
        if achado:
            return (
                f"A {_ROTULO} exige no mínimo {achado.group(1)} horas de Atividades Complementares "
                f"({achado.group(2)} créditos).",
                [totais],
            )
    if totais and pede_horas and re.search(r"\bestagios?\b", normalizada):
        achado = linha("Horas de Est")
        if achado:
            return (
                f"A {_ROTULO} exige no mínimo {achado.group(1)} horas de estágio ({achado.group(2)} créditos), "
                "divididas entre o Estágio Supervisionado I e o Estágio Supervisionado II.",
                [totais],
            )
    if not _RE_CURSO_DA_MATRIZ.search(normalizada) or re.search(r"\b(estagios?|complementar(?:es)?|optativas?)\b", normalizada):
        return None
    total = linha("HORAS CUMPRIDAS")
    if totais and total and (pede_horas or re.search(r"\b(quantos creditos|total de creditos)\b", normalizada)):
        return (
            f"A {_ROTULO} exige no mínimo {total.group(1)} horas ({total.group(2)} créditos) para a conclusão do curso.",
            [totais],
        )
    if "sistemas de informacao" in normalizada and _RE_DURACAO.search(normalizada) and periodos:
        resumo = next((b for b in blocos if str(b.get("secao", "")).startswith("Resumo da carga")), totais)
        anos = f"{periodos // 2} anos" if periodos % 2 == 0 else f"{periodos / 2:.1f} anos".replace(".", ",")
        return (
            f"A {_ROTULO} organiza o curso em {periodos} períodos (semestres), o que corresponde a {anos}.",
            [resumo] if resumo else [],
        )
    return None


def responder_consulta_matriz(pergunta: str, blocos: list[dict]) -> tuple[str, list[dict]] | None:
    """Devolve (resposta, blocos usados) quando a pergunta é um dado exato da matriz; senão None."""
    normalizada = _normalizar(pergunta)
    matriz = blocos_da_matriz(blocos)
    disciplinas, por_periodo = _ler_disciplinas(matriz)
    if not disciplinas:
        return None

    numero = _periodo_da_pergunta(normalizada)
    resultado = None
    if numero is not None:
        resultado = _resposta_periodo(numero, normalizada, disciplinas, por_periodo, matriz)
    else:
        citada = _disciplina_citada(normalizada, disciplinas)
        if citada:
            resultado = _resposta_disciplina(citada, normalizada, disciplinas, por_periodo)
        elif totais := _resposta_totais(normalizada, matriz, len(por_periodo)):
            resultado = totais
        elif re.search(r"\bquant[oa]s\b", normalizada) and re.search(r"\b(estagios?|tccs?)\b", normalizada):
            termo = "estagio" if "estagio" in normalizada else "trabalho de conclusao"
            achadas = [d for d in disciplinas if termo in _normalizar(d.nome)]
            if achadas:
                lista = "\n".join(f"- {_rotulo_disciplina(d)} ({d.periodo}º período)" for d in achadas)
                resultado = (
                    f"A {_ROTULO} tem {len(achadas)} componentes desse tipo:\n{lista}",
                    [por_periodo[n] for n in dict.fromkeys(d.periodo for d in achadas)],
                )
        elif (
            "sistemas de informacao" in normalizada
            and (re.search(r"\b(grade|matriz)\b", normalizada)
                 or (_RE_ENUMERACAO.search(normalizada) and _RE_ASSUNTO_DISCIPLINAS.search(normalizada)))
            and not _RE_HORAS.search(normalizada)
            # "quantas e quais são?" pede a lista; "quantas são?" sozinho fica com a contagem
            and (not _RE_QUANTIDADE.search(normalizada) or _RE_ENUMERACAO.search(normalizada))
        ):
            partes = []
            for n in sorted(por_periodo):
                lista = "\n".join(_item_lista(d) for d in disciplinas if d.periodo == n)
                partes.append(f"{n}º período:\n{lista}")
            total = len({d.codigo for d in disciplinas})
            resultado = (
                f"A {_ROTULO} tem {total} componentes curriculares:\n\n" + "\n\n".join(partes),
                [por_periodo[n] for n in sorted(por_periodo)],
            )
    if not resultado:
        return None
    texto, usados = resultado
    return texto, [dict(b, similaridade=1.0) for b in usados]
