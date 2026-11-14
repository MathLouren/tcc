"""Mede a busca sem o modelo de linguagem: em que posição aparece o trecho que contém a resposta.

Uso, na pasta do projeto e com o ambiente virtual ativo:
    python avaliacao/diagnostico_busca.py                 # índice do projeto
    python avaliacao/diagnostico_busca.py --indice PASTA  # índice de teste

Útil para separar erro de busca (o trecho certo não chega ao modelo) de erro de leitura do modelo.
Rápido: não chama o Ollama. Os textos do gabarito precisam existir literalmente nos documentos.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from app.config import Parametros, carregar_config  # noqa: E402
from app.embeddings import criar_embedder  # noqa: E402
from app.indexacao import carregar_indice  # noqa: E402
from app.rag import MotorRAG, _modalidade_ambigua  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

# (pergunta como o aluno escreve, trecho literal que contém a resposta)
GABARITO = [
    ("qual a media pra passar direto?", "igual ou superior a 7,0 (sete), o aluno será aprovado diretamente"),
    ("o que acontece se minha média ficar entre 4 e 7?", "deverá prestar a VS"),
    ("e se eu ficar com 5 de media?", "deverá prestar a VS"),
    ("tem segunda chamada da vs?", "2ª chamada para a Verificação Suplementar"),
    ("quantas faltas posso ter?", "frequência mínima de 75%"),
    ("qual a frequencia minima?", "frequência mínima de 75%"),
    ("quanto preciso tirar na vs pra passar?", "(MS + VS)/2"),
    ("qual a nota minima na vs?", "inferior a 5,0 (cinco) na VS"),
    ("com que media eu reprovo direto?", "Caso MS seja inferior a 4,0"),
    ("como faço pra pedir segunda chamada?", "requerimento para exame em 2ª chamada"),
    ("qual o prazo pra pedir segunda chamada?", "prazo máximo de 2 (dois) dias"),
    ("o que é AT?", "Avaliação por Trabalho (AT)"),
    ("o que é AF no ead?", "(AF)"),
    ("como funciona a média no ead?", "MP = (AP+AF+AT)"),
    ("com que media passo direto no ead?", "Média Parcial for igual ou superior a 6,0"),
    ("como tranco a matricula?", "trancamento é concedido"),
    ("posso trancar se ja estiver reprovado por falta?", "reprovado por frequência"),
    ("como peço revisao da minha prova?", "revisão da nota, no prazo de 5"),
    ("qual o endereço da universo em são gonçalo?", "Rua Lambari"),
    ("tem whatsapp pra falar com a faculdade?", "mensagem instantânea"),
    ("até quando tenho que entregar as atividades complementares?", "penúltimo período"),
    ("como faço pra ser monitor?", "Programa de Monitoria"),
    ("quais as formas de entrar na universo?", "Exame Nacional do Ensino Médio"),
    ("posso colar grau em outra data?", "colar grau em data distinta"),
    ("o que é dilação de prazo?", "dilação de prazo do curso"),
    ("quantas horas de atividade complementar eu preciso?", "Mínimo de Atividades Complementares"),
    ("o nivelamento conta como atividade complementar?", "nivelamento receberá até 20"),
]


def medir_termos(motor) -> None:
    """Condição necessária para acertar: os termos esperados precisam estar nos trechos enviados ao modelo.

    Perguntas respondidas pelo código (grade) ou que pedem esclarecimento ficam de fora da conta.
    """
    import json

    from validar_respostas import RESPONDIVEIS, carregar_gabarito, norm

    casos = [(p, g) for p, g, _ in RESPONDIVEIS]
    ampliado = RAIZ / "avaliacao" / "gabarito_ampliado.json"
    if ampliado.exists():
        casos += [(p, g) for p, g, _ in carregar_gabarito(str(ampliado))[0]]
    total = achados = tokens = 0
    faltas = []
    for pergunta, grupos in casos:
        if motor._resposta_factual_da_matriz(pergunta):
            continue
        consulta = pergunta
        if motor._campos_para_esclarecer(pergunta) or (
            _modalidade_ambigua(pergunta, motor.recuperar(pergunta), motor.indice.blocos)
        ):
            consulta = f"{pergunta} disciplinas presenciais"
        usados = motor.recuperar(consulta)
        texto = norm(" ".join(f"{b['secao']} {b.get('contexto', '')} {b.get('tabela', '')} {b['texto']}" for b in usados))
        total += 1
        tokens += sum(motor.embedder.contar_tokens(b["texto"]) for b in usados)
        if all(any(norm(t) in texto for t in g) for g in grupos):
            achados += 1
        else:
            faltas.append((pergunta, [b["secao"][:26] for b in usados]))
    print(f"{len(motor.indice.blocos)} blocos | top_k={motor.parametros.top_k} peso_lexical={motor.parametros.peso_lexical} | "
          f"termos esperados presentes nos trechos: {achados} de {total} | média de {tokens // max(total, 1)} tokens de trechos")
    for pergunta, secoes in faltas:
        print(f"  -- {pergunta} | {secoes}")


def main() -> None:
    analisador = argparse.ArgumentParser(description="Diagnóstico da busca, sem o modelo de linguagem.")
    analisador.add_argument("--indice", help="pasta com blocos.json, vetores.npy e info.json (padrão: a do projeto)")
    analisador.add_argument("--termos", action="store_true",
                            help="mede, nos gabaritos de validar_respostas.py e gabarito_ampliado.json, se os "
                                 "trechos enviados ao modelo contêm os termos esperados na resposta")
    analisador.add_argument("--top-k", type=int, help="trechos enviados ao modelo (padrão: parametros.json)")
    analisador.add_argument("--peso-lexical", type=float, help="peso da busca lexical (padrão: parametros.json)")
    args = analisador.parse_args()

    config = carregar_config()
    if args.indice:
        config = replace(config, indice_dir=Path(args.indice))
    carregado = carregar_indice(config)
    if carregado is None:
        sys.exit(f"Índice não encontrado ou gerado com outro modelo de embeddings em {config.indice_dir}.")
    indice, _ = carregado
    embedder = criar_embedder(config)
    parametros = Parametros.carregar(config.parametros_arquivo)
    if args.top_k:
        parametros = replace(parametros, top_k=args.top_k)
    if args.peso_lexical is not None:
        parametros = replace(parametros, peso_lexical=args.peso_lexical)
    motor = MotorRAG(indice, embedder, None, parametros)
    if args.termos:
        medir_termos(motor)
        return

    ausentes = [t for _, t in GABARITO if not any(t in b["texto"] for b in indice.blocos)]
    if ausentes:
        print("AVISO: textos do gabarito ausentes do índice (revise o gabarito):", ausentes)

    linhas = []
    for pergunta, alvo in GABARITO:
        resultado = indice.buscar(embedder.embed_consulta(pergunta), len(indice.blocos))
        posicao = next((n for n, (i, _) in enumerate(resultado, 1) if alvo in indice.blocos[i]["texto"]), None)
        usados = motor.recuperar(pergunta)
        chega = any(alvo in b["texto"] for b in usados)
        linhas.append((pergunta, posicao, chega, [b["secao"][:30] for b in usados]))

    def ate(k: int) -> int:
        return sum(1 for _, p, _, _ in linhas if p and p <= k)

    print(f"{len(indice.blocos)} blocos | resposta no 1º lugar: {ate(1)} | até o 4º: {ate(4)} | até o 8º: {ate(8)} | "
          f"chega ao modelo (com limiar, top_k={parametros.top_k} e reordenação): {sum(c for _, _, c, _ in linhas)} de {len(linhas)}")
    for pergunta, posicao, chega, secoes in linhas:
        print(f"  {'ok' if chega else '--'} pos {str(posicao):>4} | {pergunta} | {secoes}")


if __name__ == "__main__":
    main()
