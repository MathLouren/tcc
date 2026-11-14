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
# Assuntos que a tabela da matriz não tem: a pergunta segue para o RAG, que recusa ou busca no manual.
# Antes, "quem é o professor de N098?" recebia "N098 corresponde a Inteligência Artificial".
_RE_FORA_DO_ASSUNTO = re.compile(
    r"\b(prova|provas|avaliacao|avaliacoes|nota|notas|quando|data|datas|prazo|professor\w*|docentes?|"
    r"ementas?|conteudos?|programatico|horarios?|dias? da semana|turmas?|salas?|temas?|empresas?|"
    r"mensalidades?|valor|online|modalidade|ead|presencial|legenda|significa\w*)\b"
)
_RE_ASSUNTO_DISCIPLINAS = re.compile(r"\b(disciplinas?|materias?|cadeiras?|componentes?|estud\w+|grade)\b")
_RE_QUANTIDADE = re.compile(r"\b(quantas|quantos|numero|total)\b")
_RE_ENUMERACAO = re.compile(r"\b(quais|liste|listar|lista|cite|citar|mostre|mostrar|relacione|nomes?)\b")
_RE_HORAS = re.compile(r"\b(horas|carga horaria|carga)\b")
# Pedem as disciplinas que exigem a citada: "quais exigem N303", "dependentes de N014", "têm N014 como pré-requisito".
_RE_DEPENDENTES = re.compile(
    r"\b(dependem|dependentes?|prende|prendem|libera|liberam|exigem|exige|como pre.?requisitos?)\b"
)
_RE_REQUISITO = re.compile(r"\b(pre.?requisitos?|requisitos?|depende|para cursar|para fazer|preciso ter|precisa ter|antes de)\b")
_RE_PERGUNTA_PERIODO = re.compile(r"\b(periodo|semestre)\b")
_RE_CREDITOS = re.compile(r"\bcreditos?\b")
_RE_PERGUNTA_CODIGO = re.compile(r"\b(codigo|o que e|que disciplina|qual (?:e )?(?:a )?(?:disciplina|materia))\b")
_RE_MARCACAO = re.compile(r"\(ol\)|\bol\b|\bmarcad[oa]s?\b|\bmarcacao\b")
_RE_AULAS_SEMANAIS = re.compile(r"\baulas? (?:semanais|por semana)\b")
_RE_COMPARACAO = re.compile(r"\b(a mais|a menos|diferenca|comparacao|comparad[oa]|em relacao)\b")
_RE_SOMA = re.compile(r"\b(soma|somando|somadas?|total|distribuidas?|juntos?|juntas?)\b")
_RE_MAIOR_MENOR = re.compile(r"\b(maior|menor|mais pesado|mais leve)\b")
_RE_MESMA = re.compile(r"\b(mesma|mesmas|igual|iguais)\b")
_NUMEROS_POR_EXTENSO = {"dois": 2, "duas": 2, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8}


@dataclass(frozen=True)
class Disciplina:
    codigo: str
    nome: str
    periodo: int
    horas: str
    creditos: str
    requisitos: tuple[str, ...]
    marcacao: str = ""


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
                marcacao="" if campo("marcacao") in {"", "—", "-"} else campo("marcacao"),
            ))
    return list(disciplinas.values()), por_periodo


def _resumo_por_periodo(blocos: list[dict]) -> tuple[dict[int, dict[str, int]], dict | None]:
    """Tabela "Resumo da carga horária por período": {período: {carga, aulas, complementares, total}}."""
    resumo = next((b for b in blocos if str(b.get("secao", "")).startswith("Resumo da carga horária")), None)
    linhas: dict[int, dict[str, int]] = {}
    for achado in re.finditer(r"^(\d+)º período\s*\|(.+)$", str((resumo or {}).get("texto", "")), re.MULTILINE):
        numeros = [re.sub(r"\D", "", c) for c in achado.group(2).split("|")]
        if len(numeros) >= 4 and all(numeros[:4]):
            carga, aulas, complementares, total = (int(n) for n in numeros[:4])
            linhas[int(achado.group(1))] = {"carga": carga, "aulas": aulas, "complementares": complementares, "total": total}
    return linhas, resumo


def _periodos_citados(normalizada: str, total: int) -> list[int]:
    """Todos os períodos citados: "6º, 7º e 8º", "do 1º ao 8º", "cinco primeiros", "todos os períodos"."""
    # "período" escrito ou ordinal ("do 4º é pré-requisito do 6º?"); "2º semestre de 2026" é o semestre letivo
    if re.search(r"\b20\d{2}\b", normalizada) or not re.search(r"\b(periodos?|semestres?)\b|\b[1-9]\s*o(?![a-z])", normalizada):
        return []
    periodos: set[int] = set()
    intervalo = re.search(r"\bdo\s+([1-9])\s*o?\s*(?:periodo\s+)?(?:ao|a|ate o)\s+([1-9])\s*o?\b", normalizada)
    if intervalo:
        inicio, fim = sorted(int(n) for n in intervalo.groups())
        periodos.update(range(inicio, fim + 1))
    primeiros = re.search(rf"\b([1-9]|{'|'.join(_NUMEROS_POR_EXTENSO)})\s+(primeiros|ultimos)\s+(?:periodos|semestres)\b", normalizada)
    if primeiros:
        n = int(primeiros.group(1)) if primeiros.group(1).isdigit() else _NUMEROS_POR_EXTENSO[primeiros.group(1)]
        periodos.update(range(1, n + 1) if primeiros.group(2) == "primeiros" else range(total - n + 1, total + 1))
    if re.search(r"\btodos os (?:periodos|semestres)\b|\bcada periodo\b", normalizada) and total:
        periodos.update(range(1, total + 1))
    periodos.update(int(n) for n in re.findall(r"\b([1-9])\s*(?:o|°)(?![a-z])", normalizada))
    periodos.update(int(n) for n in re.findall(r"\b([1-9])\s*(?:periodo|semestre)\b", normalizada))
    periodos.update(int(n) for n in re.findall(r"\b(?:periodo|semestre)\s+([1-9])\b", normalizada))
    periodos.update(_ORDINAIS[o] for o in re.findall(rf"\b({'|'.join(_ORDINAIS)})\b", normalizada))
    return sorted(p for p in periodos if not total or 1 <= p <= total)


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


def _disciplinas_citadas(normalizada: str, disciplinas: list[Disciplina]) -> list[Disciplina]:
    """Todas as disciplinas citadas por código ou por nome, sem repetir a mesma disciplina."""
    por_codigo = {d.codigo.lower(): d for d in disciplinas}
    citadas = {por_codigo[t].codigo: por_codigo[t] for t in re.findall(r"[a-z]{1,3}\d{2,4}", normalizada) if t in por_codigo}
    tokens = _tokens_da_pergunta(normalizada)
    for disciplina in disciplinas:
        if any(_contem_sequencia(tokens, v) for v in _variantes_do_nome(disciplina.nome)):
            citadas.setdefault(disciplina.codigo, disciplina)
    # Um nome pode estar dentro de outro mais longo também citado: fica só o mais longo. A comparação é
    # por palavras ("i" e "ii" são palavras diferentes), não por texto.
    nomes = {c: _tokens(d.nome) for c, d in citadas.items()}
    return [d for c, d in citadas.items()
            if not any(o != c and len(nomes[o]) > len(nomes[c]) and _contem_sequencia(nomes[o], nomes[c])
                       for o in citadas)]


def _posicao_da_disciplina(normalizada: str, d: Disciplina) -> int:
    """Onde a disciplina aparece na pergunta (código ou primeira palavra do nome); -1 se não achar."""
    posicao = normalizada.find(d.codigo.lower())
    if posicao >= 0:
        return posicao
    for variante in _variantes_do_nome(d.nome):
        achado = re.search(r"\b" + r"\W+(?:\w+\W+){0,2}".join(map(re.escape, variante)), normalizada)
        if achado:
            return achado.start()
    return -1


def _rotulo_disciplina(d: Disciplina) -> str:
    return f"{d.codigo} — {d.nome}"


def _item_lista(d: Disciplina) -> str:
    detalhes = ", ".join(p for p in (d.horas, f"{d.creditos} créditos" if d.creditos else "") if p)
    return f"- {_rotulo_disciplina(d)}" + (f" ({detalhes})" if detalhes else "")


def _resposta_periodo(numero, normalizada, disciplinas, por_periodo, blocos):
    do_periodo = [d for d in disciplinas if d.periodo == numero]
    if not do_periodo or _RE_FORA_DO_ASSUNTO.search(normalizada):
        return None
    # Pré-requisito ou marcação dentro de um período não é pedido de lista do período.
    if _RE_REQUISITO.search(normalizada) or _RE_DEPENDENTES.search(normalizada) or _RE_MARCACAO.search(normalizada):
        return None

    if _RE_AULAS_SEMANAIS.search(normalizada):
        linhas, resumo = _resumo_por_periodo(blocos)
        if numero in linhas:
            return f"Na {_ROTULO}, o {numero}º período tem {linhas[numero]['aulas']} aulas semanais.", [resumo]
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

    # "quantas aulas semanais?" não é "quantas disciplinas?": a contagem exige o assunto disciplinas
    if _RE_QUANTIDADE.search(normalizada) and _RE_ASSUNTO_DISCIPLINAS.search(normalizada):
        lista = "\n".join(_item_lista(d) for d in do_periodo)
        return (
            f"O {numero}º período da {_ROTULO} tem {len(do_periodo)} componentes curriculares:\n{lista}",
            [por_periodo[numero]],
        )

    if _RE_ASSUNTO_DISCIPLINAS.search(normalizada):
        lista = "\n".join(_item_lista(d) for d in do_periodo)
        return f"Disciplinas do {numero}º período na {_ROTULO}:\n{lista}", [por_periodo[numero]]
    return None


def _pede_dependentes(normalizada: str, d: Disciplina) -> bool:
    """A pergunta quer as disciplinas que exigem d (e não os pré-requisitos de d)?

    "prende/libera/dependem de" sempre apontam para as dependentes. Nos demais, a ordem decide:
    "quais exigem N303?" (verbo antes) x "o que N535 exige?" (verbo depois); "quais têm N014 como
    pré-requisito?" (disciplina antes da expressão).
    """
    if re.search(r"\b(dependem|prende|prendem|libera|liberam|dependentes?)\b", normalizada):
        return True
    posicao = _posicao_da_disciplina(normalizada, d)
    exige = re.search(r"\bexigem?\b", normalizada)
    if exige and 0 <= exige.start() < posicao:
        return True
    como = re.search(r"\bcomo pre.?requisitos?\b", normalizada)
    return bool(como and 0 <= posicao < como.start())


_RE_CADEIA = re.compile(r"\b(sequencia|cadeia|caminho|trilha|todos os pre.?requisitos|ate chegar|ate o|ate a)\b")


def _resposta_cadeia(d, por_codigo, por_periodo):
    """Pré-requisitos dos pré-requisitos, do fim para o começo: a cadeia completa até d."""
    etapas, visitadas, fila = [], set(), [d]
    while fila:
        atual = fila.pop(0)
        if atual.codigo in visitadas or not atual.requisitos:
            continue
        visitadas.add(atual.codigo)
        exigidas = [por_codigo[r] for r in atual.requisitos if r in por_codigo]
        etapas.append((atual, exigidas))
        fila.extend(exigidas)
    linhas = [
        f"- Para cursar {_rotulo_disciplina(alvo)} ({alvo.periodo}º período): "
        + ", ".join(f"{x.codigo} ({x.periodo}º)" for x in exigidas)
        for alvo, exigidas in sorted(etapas, key=lambda par: par[0].periodo)
    ]
    envolvidas = {d.periodo} | {x.periodo for _, exigidas in etapas for x in exigidas}
    return (f"Na {_ROTULO}, a cadeia de pré-requisitos até {_rotulo_disciplina(d)} é:\n" + "\n".join(linhas),
            [por_periodo[n] for n in sorted(envolvidas) if n in por_periodo])


def _resposta_requisitos_entre_periodos(normalizada, disciplinas, por_periodo, periodos):
    """Disciplinas de um período que são pré-requisito de disciplinas de outro(s).

    O período mais antigo citado é o dos pré-requisitos. Com "tanto ... quanto", a disciplina precisa ser
    pré-requisito em todos os outros períodos citados.
    """
    origem, destinos = periodos[0], periodos[1:]
    pares: dict[str, list[Disciplina]] = {}
    for d in disciplinas:
        if d.periodo in destinos:
            for r in d.requisitos:
                pares.setdefault(r, []).append(d)
    por_codigo = {d.codigo: d for d in disciplinas}
    achadas = [(por_codigo[r], deps) for r, deps in pares.items() if r in por_codigo and por_codigo[r].periodo == origem]
    if re.search(r"\btanto\b", normalizada):
        achadas = [(r, deps) for r, deps in achadas if {x.periodo for x in deps} >= set(destinos)]
    blocos = [por_periodo[p] for p in periodos if p in por_periodo]
    destino_txt = " e ".join(f"{p}º" for p in destinos)
    if not achadas:
        return (f"Não. A {_ROTULO} não registra disciplina do {origem}º período como pré-requisito de disciplina "
                f"do {destino_txt} período.", blocos)
    linhas = [f"- {_rotulo_disciplina(r)} é pré-requisito de "
              + ", ".join(f"{_rotulo_disciplina(x)} ({x.periodo}º período)" for x in deps) for r, deps in achadas]
    return f"Sim. Na {_ROTULO}:\n" + "\n".join(linhas), blocos


def _resposta_disciplina(d, normalizada, disciplinas, por_periodo):
    por_codigo = {x.codigo: x for x in disciplinas}
    bloco = [por_periodo[d.periodo]]
    if _RE_FORA_DO_ASSUNTO.search(normalizada) or _RE_MARCACAO.search(normalizada):
        return None

    if _pede_dependentes(normalizada, d):
        dependentes = [x for x in disciplinas if d.codigo in x.requisitos]
        if not dependentes:
            return f"A {_ROTULO} não indica disciplinas que dependam de {_rotulo_disciplina(d)}.", bloco
        lista = "\n".join(f"- {_rotulo_disciplina(x)} ({x.periodo}º período)" for x in dependentes)
        blocos = [por_periodo[n] for n in dict.fromkeys(x.periodo for x in dependentes)]
        return f"Na {_ROTULO}, dependem de {_rotulo_disciplina(d)}:\n{lista}", blocos

    if _RE_REQUISITO.search(normalizada) or re.search(r"\bexigem?\b", normalizada):
        if not d.requisitos:
            return f"A {_ROTULO} não indica pré-requisito para {_rotulo_disciplina(d)}.", bloco
        if _RE_CADEIA.search(normalizada):
            return _resposta_cadeia(d, por_codigo, por_periodo)
        # o período de cada pré-requisito vai junto: "em quais períodos estão os pré-requisitos?"
        lista = "\n".join(
            f"- {_rotulo_disciplina(por_codigo[r])} ({por_codigo[r].periodo}º período)" if r in por_codigo else f"- {r}"
            for r in d.requisitos
        )
        return f"Na {_ROTULO}, os pré-requisitos de {_rotulo_disciplina(d)} são:\n{lista}", bloco

    if _RE_CREDITOS.search(normalizada) and d.creditos:
        return f"Na {_ROTULO}, {_rotulo_disciplina(d)} vale {d.creditos} créditos.", bloco
    if _RE_HORAS.search(normalizada) and d.horas:
        return f"Na {_ROTULO}, {_rotulo_disciplina(d)} tem carga horária de {d.horas}.", bloco
    if _RE_PERGUNTA_PERIODO.search(normalizada):
        return f"Na {_ROTULO}, {_rotulo_disciplina(d)} é do {d.periodo}º período.", bloco
    tem_codigo = re.search(r"\b[a-z]{1,3}\d{2,4}\b", normalizada)
    if (tem_codigo and _RE_PERGUNTA_CODIGO.search(normalizada)) or re.search(r"\bcodigo\b", normalizada):
        return f"Na {_ROTULO}, o código {d.codigo} corresponde a {d.nome} ({d.periodo}º período).", bloco
    return None


def _quantas(n: int, singular: str, plural: str) -> str:
    """"1 disciplina tem", "7 disciplinas têm"."""
    return f"{n} {singular if n == 1 else plural}"


def _lista_curta(itens: list[Disciplina], com_periodo: bool = True) -> str:
    return "\n".join(f"- {_rotulo_disciplina(d)}" + (f" ({d.periodo}º período)" if com_periodo else "") for d in itens)


def _resposta_marcacao(normalizada, disciplinas, por_periodo, periodos):
    """Disciplinas com a marcação (OL) do fluxograma: lista, contagem ou presença num período."""
    marcadas = [d for d in disciplinas if d.marcacao]
    if not marcadas:
        return None
    sem_legenda = "O documento não traz a legenda dessa marcação."
    if periodos:
        nos_periodos = [d for d in marcadas if d.periodo in periodos]
        nomes = " e ".join(f"{p}º" for p in periodos)
        blocos = [por_periodo[p] for p in periodos if p in por_periodo]
        if not nos_periodos:
            return f"Não. Na {_ROTULO}, nenhuma disciplina do {nomes} período tem a marcação (OL).", blocos
        quantas = _quantas(len(nos_periodos), "disciplina", "disciplinas")
        verbo = "tem" if len(nos_periodos) == 1 else "têm"
        return (f"Sim. Na {_ROTULO}, {quantas} do {nomes} período {verbo} a marcação (OL):\n"
                f"{_lista_curta(nos_periodos)}\n{sem_legenda}", blocos)
    blocos = [por_periodo[n] for n in dict.fromkeys(d.periodo for d in marcadas)]
    quantas = _quantas(len(marcadas), "disciplina tem", "disciplinas têm")
    return f"Na {_ROTULO}, {quantas} a marcação (OL):\n{_lista_curta(marcadas)}\n{sem_legenda}", blocos


def _resposta_por_periodos(normalizada, periodos, blocos):
    """Aulas semanais e carga de disciplinas comparadas, somadas ou ordenadas entre períodos."""
    aulas = bool(_RE_AULAS_SEMANAIS.search(normalizada))
    if not aulas and not _RE_HORAS.search(normalizada):
        return None
    linhas, resumo = _resumo_por_periodo(blocos)
    if not linhas:
        return None
    pede_complementares = re.search(r"\batividades? complementar", normalizada) and not re.search(
        r"\b(sem (?:contar )?(?:as )?|antes das? |excluindo (?:as )?)atividades? complementar", normalizada)
    campo = "aulas" if aulas else ("complementares" if pede_complementares else "carga")
    unidade = {"aulas": " aulas semanais", "complementares": " h de atividades complementares",
               "carga": " h de disciplinas (sem as atividades complementares)"}[campo]
    valor = {p: linhas[p][campo] for p in linhas}
    escolhidos = [p for p in periodos if p in valor] or sorted(valor)
    detalhe = "; ".join(f"{p}º: {valor[p]}" for p in escolhidos)

    if _RE_MAIOR_MENOR.search(normalizada) and len(periodos) != 1:
        maior = "menor" not in normalizada and "mais leve" not in normalizada
        alvo = (max if maior else min)(valor[p] for p in escolhidos)
        quais = [p for p in escolhidos if valor[p] == alvo]
        texto = " e ".join(f"{p}º" for p in quais)
        return (f"Na {_ROTULO}, a {'maior' if maior else 'menor'} carga é a do {texto} período, com {alvo}{unidade}. "
                f"Por período: {detalhe}.", [resumo])
    if len(periodos) < 2:
        return None
    if _RE_COMPARACAO.search(normalizada) and len(periodos) == 2:
        a, b = periodos
        diferenca = abs(valor[a] - valor[b])
        mais, menos = (a, b) if valor[a] >= valor[b] else (b, a)
        return (f"Na {_ROTULO}, o {mais}º período tem {valor[mais]}{unidade} e o {menos}º tem {valor[menos]}: "
                f"{valor[mais]} − {valor[menos]} = {diferenca} a mais no {mais}º período.", [resumo])
    if _RE_MESMA.search(normalizada):
        iguais = len({valor[p] for p in escolhidos}) == 1
        return (f"{'Sim' if iguais else 'Não'}. Na {_ROTULO}: {detalhe}{unidade}.", [resumo])
    if not _RE_SOMA.search(normalizada):  # "quantas aulas tem cada período?": um valor por período
        return f"Na {_ROTULO}, por período: {detalhe}{unidade}.", [resumo]
    soma = sum(valor[p] for p in escolhidos)
    conta = " + ".join(str(valor[p]) for p in escolhidos)
    contiguos = escolhidos == list(range(escolhidos[0], escolhidos[-1] + 1))
    quais = (f"do {escolhidos[0]}º ao {escolhidos[-1]}º período" if contiguos
             else "dos períodos " + ", ".join(f"{p}º" for p in escolhidos))
    return f"Na {_ROTULO}, a soma {quais} é {conta} = {soma}{unidade}.", [resumo]


# Pergunta sobre a ligação entre duas disciplinas: "preciso concluir X antes de Y?", "o que liga X a Y?"
_RE_RELACAO = re.compile(
    r"\b(pre.?requisitos?|requisitos?|antes|depois|liga|ligacao|relacao|obrigatori\w*|precis\w+ (?:ter )?"
    r"(?:concluir|concluido|cursar|cursado|fazer|feito)|(?:pra|para) (?:cursar|fazer)|depende|exige)\b"
)


def _resposta_relacao(citadas, por_periodo):
    """Se uma das duas disciplinas é pré-requisito da outra, segundo a coluna de pré-requisitos.

    Sem este cálculo, o modelo afirmou que N476 é pré-requisito de N239, o que a matriz não registra.
    """
    a, b = citadas
    blocos = [por_periodo[n] for n in dict.fromkeys((a.periodo, b.periodo))]
    for anterior, posterior in ((a, b), (b, a)):
        if anterior.codigo in posterior.requisitos:
            return (f"Na {_ROTULO}, {_rotulo_disciplina(anterior)} ({anterior.periodo}º período) é pré-requisito de "
                    f"{_rotulo_disciplina(posterior)} ({posterior.periodo}º período): é preciso cursar "
                    f"{anterior.codigo} antes de {posterior.codigo}.", blocos)
    return (f"A {_ROTULO} não registra pré-requisito entre {_rotulo_disciplina(a)} ({a.periodo}º período) e "
            f"{_rotulo_disciplina(b)} ({b.periodo}º período), em nenhum dos dois sentidos.", blocos)


_TIPOS_DE_COMPONENTE = {
    "estagio": (r"\bestagios?\b", "estagio", "Horas de Est"),
    "tcc": (r"\btccs?\b|\btrabalhos? de conclusao\b", "trabalho de conclusao", "Pesquisa/Projetos/TCC"),
    "optativa": (r"\boptativas?\b", "optativa", "Horas Optativas"),
}
_RE_COMPOSICAO = re.compile(
    r"\b(chega|chegar|chegam|compoe|compoem|composicao|compost[oa]s?|decompo\w*|formad[oa]s?|se dividem|divid\w+|"
    r"distribuem|de onde vem)\b"
)


def _linhas_da_totalizacao(blocos: list[dict]) -> tuple[list[tuple[str, int]], int | None, dict | None]:
    """Categorias do quadro de totalização em horas e o mínimo de horas cumpridas."""
    totais = next((b for b in blocos if str(b.get("secao", "")).startswith("Totalização")), None)
    categorias, total = [], None
    for linha in str((totais or {}).get("texto", "")).splitlines():
        achado = re.match(r"^[-*\s]*([^:=]+?)\s*[:=]\s*([\d.]+)\s*h", linha.strip())
        if not achado:
            continue
        horas = int(achado.group(2).replace(".", ""))
        if "cumpridas" in _normalizar(achado.group(1)):
            total = horas
        else:
            categorias.append((achado.group(1).strip(), horas))
    return categorias, total, totais


def _resposta_composicao_total(normalizada, blocos):
    """Como as horas mínimas do curso se formam: pelos períodos e pelo quadro de totalização."""
    if not (_RE_COMPOSICAO.search(normalizada) and _RE_HORAS.search(normalizada)):
        return None
    categorias, total, totais = _linhas_da_totalizacao(blocos)
    linhas, resumo = _resumo_por_periodo(blocos)
    if not total:
        return None
    partes, usados = [], [totais]
    if linhas:
        disciplinas_h = sum(v["carga"] for v in linhas.values())
        complementares_h = sum(v["complementares"] for v in linhas.values())
        if disciplinas_h + complementares_h == total:
            partes.append(f"Pelos períodos: {disciplinas_h} h de disciplinas (soma do 1º ao {max(linhas)}º período) + "
                          f"{complementares_h} h de atividades complementares = {total} h.")
            usados.append(resumo)
    com_horas = [(nome, h) for nome, h in categorias if h]
    if com_horas and sum(h for _, h in com_horas) == total:
        zeradas = [nome for nome, h in categorias if not h]
        texto = "Pelo quadro de totalização: " + " + ".join(f"{nome} {h}" for nome, h in com_horas) + f" = {total} h."
        if zeradas:
            texto += " Com 0 h: " + ", ".join(zeradas) + "."
        partes.append(texto)
    if not partes:
        return None
    return f"A {_ROTULO} exige no mínimo {total} horas.\n" + "\n".join(partes), usados


def _resposta_soma_por_tipo(normalizada, disciplinas, por_periodo, blocos):
    """Soma das horas dos componentes de um tipo (estágio, TCC, optativa), conferida com a totalização."""
    if not (_RE_SOMA.search(normalizada) and _RE_HORAS.search(normalizada)):
        return None
    tipo = next((t for t, (padrao, _, _) in _TIPOS_DE_COMPONENTE.items() if re.search(padrao, normalizada)), None)
    if not tipo:
        return None
    _, termo, rotulo_total = _TIPOS_DE_COMPONENTE[tipo]
    achadas = [d for d in disciplinas if termo in _normalizar(d.nome) and re.sub(r"\D", "", d.horas)]
    if not achadas:
        return None
    horas = [int(re.sub(r"\D", "", d.horas)) for d in achadas]
    texto = (f"Na {_ROTULO}: " + " + ".join(f"{h} ({d.codigo})" for h, d in zip(horas, achadas))
             + f" = {sum(horas)} horas.")
    categorias, _, totais = _linhas_da_totalizacao(blocos)
    usados = [por_periodo[n] for n in dict.fromkeys(d.periodo for d in achadas)]
    minimo = next((h for nome, h in categorias if _normalizar(rotulo_total) in _normalizar(nome)), None)
    if minimo is not None and minimo == sum(horas):
        texto += f" É o mesmo valor do mínimo indicado no quadro de totalização ({minimo} h)."
        usados.append(totais)
    return texto, usados


def _resposta_por_carga(normalizada, disciplinas, por_periodo):
    """Disciplinas agrupadas pela carga horária citada: "quais têm 180 horas?", "quantas têm 80 h?"."""
    cargas = [int(n) for n in re.findall(r"\b(\d{2,3})\s*h(?:oras)?\b", normalizada)]
    if not cargas or not _RE_ASSUNTO_DISCIPLINAS.search(normalizada):
        return None
    partes, usados = [], []
    for carga in dict.fromkeys(cargas):
        achadas = [d for d in disciplinas if re.sub(r"\D", "", d.horas) == str(carga)]
        usados += [por_periodo[d.periodo] for d in achadas]
        if not achadas:
            partes.append(f"Nenhum componente tem {carga} horas.")
        elif len(achadas) > 6:
            partes.append(f"{_quantas(len(achadas), 'componente tem', 'componentes têm')} {carga} horas.")
        else:
            partes.append(f"{_quantas(len(achadas), 'componente tem', 'componentes têm')} {carga} horas:\n"
                          f"{_lista_curta(achadas)}")
    blocos = list({id(b): b for b in usados}.values())
    return f"Na {_ROTULO}:\n" + "\n".join(partes), blocos


_RE_OUTRO_ASSUNTO_DE_HORAS = re.compile(
    r"\b(nivelamento|iniciacao|monitoria|extensao|certificados?|tratamento|material|formacao docente|"
    r"programa especial|pos|licenca|cumprir|biblioteca|periodo)\b"
)
_RE_PEDE_HORAS = re.compile(r"\b(quantas horas|carga horaria|total de horas|horas (?:tem|sao|preciso|precisa)|minimo de horas)\b")
_RE_DURACAO = re.compile(r"\b(quanto tempo|duracao|dura|quantos periodos|quantos semestres|quantos anos)\b")
# "nesse fluxograma", "no currículo", "da grade": formas de citar a própria matriz
_RE_CURSO_DA_MATRIZ = re.compile(
    r"\b(sistemas de informacao|meu curso|do curso|o curso|fluxograma|curriculo|grade|matriz|109an)\b"
)


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
    # Uma categoria da totalização (teóricas, práticas...) não é o total do curso.
    if not _RE_CURSO_DA_MATRIZ.search(normalizada) or re.search(
        r"\b(estagios?|complementar(?:es)?|optativas?|teoricas?|praticas?|pesquisa|projetos?)\b", normalizada
    ):
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

    if _RE_FORA_DO_ASSUNTO.search(normalizada):
        return None
    periodos = _periodos_citados(normalizada, len(por_periodo))
    citadas = _disciplinas_citadas(normalizada, disciplinas)
    resultado = None
    if _RE_MARCACAO.search(normalizada):
        resultado = _resposta_marcacao(normalizada, disciplinas, por_periodo, periodos)
    elif not citadas and (composicao := _resposta_composicao_total(normalizada, matriz)):
        resultado = composicao
    elif len(periodos) >= 2 and not citadas and (_RE_REQUISITO.search(normalizada) or _RE_DEPENDENTES.search(normalizada)):
        resultado = _resposta_requisitos_entre_periodos(normalizada, disciplinas, por_periodo, periodos)
    elif por_periodos := _resposta_por_periodos(normalizada, periodos, matriz):
        resultado = por_periodos
    elif not periodos and (por_carga := _resposta_por_carga(normalizada, disciplinas, por_periodo)):
        resultado = por_carga
    elif re.search(r"\bcodigo d[oa] (?:curriculo|matriz|grade)\b|\bqual (?:e )?o curriculo\b", normalizada):
        bloco = next((b for b in matriz if re.search(r"curr[ií]culo:\s*109an", str(b.get("texto", "")), re.I)), None)
        if bloco:
            resultado = "O fluxograma de Sistemas de Informação corresponde ao currículo 109AN.", [bloco]
    elif len(citadas) == 2 and _RE_RELACAO.search(normalizada):
        resultado = _resposta_relacao(citadas, por_periodo)
    elif len(periodos) > 1 or len(citadas) > 1:
        # Respostas prontas são para uma entidade só; comparar períodos ou relacionar disciplinas fica
        # com o RAG. Antes, "6º em relação ao 5º" recebia só a carga do 6º.
        resultado = None
    elif periodos:
        resultado = _resposta_periodo(periodos[0], normalizada, disciplinas, por_periodo, matriz)
    else:
        citada = citadas[0] if citadas else None
        if citada:
            resultado = _resposta_disciplina(citada, normalizada, disciplinas, por_periodo)
        elif re.search(r"\b(soma|somando|somad[oa]s?|juntando)\b", normalizada) and (
            soma_tipo := _resposta_soma_por_tipo(normalizada, disciplinas, por_periodo, matriz)
        ):
            resultado = soma_tipo  # "somando os estágios" pede a conta, não só o mínimo da totalização
        elif totais := _resposta_totais(normalizada, matriz, len(por_periodo)):
            resultado = totais
        elif soma_tipo := _resposta_soma_por_tipo(normalizada, disciplinas, por_periodo, matriz):
            resultado = soma_tipo
        elif (re.search(r"\bquant[oa]s\s+(?:\w+\s+){0,3}?(?:estagios?|tccs?)\b", normalizada)
              and not re.search(r"\bcomponentes curriculares\b|\bcontando\b", normalizada)
              and not _RE_HORAS.search(normalizada)):  # "quantas horas de estágio" pede horas, não a contagem
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
