"use strict";

const $ = (seletor) => document.querySelector(seletor);

const POR_PAGINA = 25;
const LIMITE_LOGS = 5000;
const MODOS = {
  llm: { rotulo: "Gerada pelo modelo", classe: "selo-azul" },
  validado: { rotulo: "Validada", classe: "selo-verde" },
  citacao: { rotulo: "Citação do documento", classe: "selo-verde" },
  extrativo: { rotulo: "Trechos (sem modelo)", classe: "selo-ambar" },
  esclarecimento: { rotulo: "Esclarecimento", classe: "" },
  conversa: { rotulo: "Saudação", classe: "" },
  sem_resultado: { rotulo: "Sem resultado", classe: "selo-vermelho" },
};

const VEREDITOS = {
  correta: { rotulo: "Correta", classe: "selo-verde" },
  parcial: { rotulo: "Parcial", classe: "selo-ambar" },
  incorreta: { rotulo: "Incorreta", classe: "selo-vermelho" },
};

function lerPreferencia(chave, padrao) {
  try { return window.localStorage.getItem(chave) || padrao; } catch { return padrao; }
}

function gravarPreferencia(chave, valor) {
  try { window.localStorage.setItem(chave, valor); } catch { /* navegador sem armazenamento */ }
}

const estado = {
  interacoes: [],
  documentos: [],
  pagina: 1,
  documentoEmRemocao: null,
  periodo: lerPreferencia("painel-periodo", "30d"),
  painel: null,
  temporizador: null,
};

// ---------------------------------------------------------------- utilitários
function criar(tag, classe, texto) {
  const el = document.createElement(tag);
  if (classe) el.className = classe;
  if (texto !== undefined && texto !== null) el.textContent = texto;
  return el;
}

// Busca sem diferenciar maiúsculas nem acentos ("matricula" encontra "matrícula").
function normalizar(texto) {
  return String(texto || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
}

function formatarDataHora(iso) {
  const [data, hora] = String(iso || "").split("T");
  const [a, m, d] = (data || "").split("-");
  return d ? `${d}/${m}/${a} ${(hora || "").slice(0, 5)}`.trim() : "";
}

function formatarData(iso) {
  const [a, m, d] = String(iso || "").slice(0, 10).split("-");
  return d ? `${d}/${m}/${a}` : "";
}

function formatarTempo(ms) {
  const valor = Number(ms);
  if (!Number.isFinite(valor)) return "";
  if (valor < 1000) return `${Math.round(valor)} ms`;
  return `${(valor / 1000).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} s`;
}

function formatarTamanho(kb) {
  const valor = Number(kb) || 0;
  if (valor < 1024) return `${valor.toLocaleString("pt-BR", { maximumFractionDigits: 1 })} KB`;
  return `${(valor / 1024).toLocaleString("pt-BR", { maximumFractionDigits: 1 })} MB`;
}

function plural(n, singular, pluralTexto) {
  return `${n.toLocaleString("pt-BR")} ${n === 1 ? singular : pluralTexto}`;
}

function selo(texto, classe) {
  return criar("span", `selo ${classe || ""}`.trim(), texto);
}

function seloModo(modo) {
  const info = MODOS[modo] || { rotulo: modo || "Não informado", classe: "" };
  return selo(info.rotulo, info.classe);
}

function seloAvaliacao(avaliacao) {
  if (!avaliacao) return criar("span", "traco", "—");
  return avaliacao.util ? selo("Útil", "selo-verde") : selo("Não útil", "selo-vermelho");
}

// Saudações e pedidos de esclarecimento não são respostas a validar.
const SEM_VALIDACAO = ["conversa", "esclarecimento"];

function seloValidacao(revisao, modo) {
  if (!revisao && SEM_VALIDACAO.includes(modo)) return criar("span", "traco", "—");
  if (!revisao) return selo("Pendente");
  const info = VEREDITOS[revisao.veredito] || { rotulo: revisao.veredito, classe: "" };
  return selo(info.rotulo, info.classe);
}

function avisar(texto, erro = false) {
  const aviso = criar("div", `aviso${erro ? " aviso-erro" : ""}`, texto);
  $("#avisos").appendChild(aviso);
  window.setTimeout(() => aviso.remove(), erro ? 7000 : 4000);
}

function linhaEstado(corpo, colunas, mensagem, acao) {
  const tr = criar("tr");
  const td = criar("td", "estado");
  td.colSpan = colunas;
  td.appendChild(criar("div", null, mensagem));
  if (acao) {
    const botao = criar("button", "btn", acao.rotulo);
    botao.type = "button";
    botao.addEventListener("click", acao.executar);
    td.appendChild(botao);
  }
  tr.appendChild(td);
  corpo.replaceChildren(tr);
}

// ---------------------------------------------------------------- API
class ErroSessao extends Error {}

async function api(caminho, opcoes = {}) {
  const resposta = await fetch(caminho, { credentials: "same-origin", ...opcoes });
  if (resposta.status === 401 && caminho !== "/admin/login") {
    throw new ErroSessao("Sua sessão expirou. Entre novamente.");
  }
  if (!resposta.ok) {
    let detalhe = `Não foi possível concluir a operação (erro ${resposta.status}).`;
    try {
      const corpo = await resposta.json();
      if (typeof corpo.detail === "string") detalhe = corpo.detail;
      else if (Array.isArray(corpo.detail)) detalhe = corpo.detail.map((d) => d.msg).join("; ");
    } catch { /* resposta sem JSON */ }
    throw new Error(detalhe);
  }
  return resposta.json();
}

// Sessão expirada em qualquer ação volta ao login; os demais erros seguem para quem chamou.
function tratarSessao(erro) {
  if (!(erro instanceof ErroSessao)) return false;
  mostrarLogin(erro.message);
  return true;
}

// ---------------------------------------------------------------- telas
function mostrarLogin(mensagem) {
  document.querySelectorAll("dialog[open]").forEach((d) => d.close());
  $("#painel").hidden = true;
  $("#tela-login").hidden = false;
  const aviso = $("#login-aviso");
  aviso.textContent = mensagem || "";
  aviso.hidden = !mensagem;
  $("#email").focus();
}

function mostrarPainel(email) {
  $("#tela-login").hidden = true;
  $("#painel").hidden = false;
  $("#usuario").textContent = email || "";
  aplicarSecao();
  carregarPainel();
  carregarLogs();
  carregarDocumentos();
}

const SECOES = ["painel", "logs", "documentos"];

function secaoAtual() {
  const alvo = window.location.hash.replace("#", "");
  return SECOES.includes(alvo) ? alvo : "painel";
}

function aplicarSecao() {
  const atual = secaoAtual();
  SECOES.forEach((secao) => { $(`#secao-${secao}`).hidden = secao !== atual; });
  if (atual === "painel" && estado.painel) desenharGraficos();
  document.querySelectorAll(".nav-item").forEach((item) => {
    if (item.dataset.secao === atual) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
}

// ---------------------------------------------------------------- conversas
function renderizarKpis(dados) {
  const avaliadas = dados.avaliacoes.uteis + dados.avaliacoes.nao_uteis;
  const semResultado = (dados.por_modo || {}).sem_resultado || 0;
  const total = dados.total_interacoes;
  const indicadores = [
    ["Interações", total.toLocaleString("pt-BR"), ""],
    [
      "Respostas úteis",
      avaliadas ? `${Math.round((dados.avaliacoes.uteis / avaliadas) * 100)}%` : "—",
      avaliadas ? `de ${plural(avaliadas, "avaliação", "avaliações")}` : "Nenhuma avaliação ainda",
    ],
    [
      "Sem resultado",
      semResultado.toLocaleString("pt-BR"),
      total ? `${Math.round((semResultado / total) * 100)}% das perguntas` : "",
    ],
    ["Tempo médio", total ? formatarTempo(dados.tempo_medio_ms) : "—", "por resposta"],
  ];
  $("#kpis").replaceChildren(...indicadores.map(([rotulo, valor, nota]) => {
    const cartao = criar("div", "kpi");
    cartao.append(criar("div", "kpi-rotulo", rotulo), criar("div", "kpi-valor", valor));
    if (nota) cartao.appendChild(criar("div", "kpi-nota", nota));
    return cartao;
  }));
}

function preencherFiltroModo() {
  const select = $("#filtro-modo");
  const atual = select.value;
  const modos = [...new Set(estado.interacoes.map((r) => r.modo).filter(Boolean))];
  const opcoes = modos.map((modo) => {
    const opcao = criar("option", null, (MODOS[modo] || { rotulo: modo }).rotulo);
    opcao.value = modo;
    return opcao;
  });
  const todos = criar("option", null, "Todos");
  todos.value = "";
  select.replaceChildren(todos, ...opcoes);
  select.value = modos.includes(atual) ? atual : "";
}

function filtrarInteracoes() {
  const termo = normalizar($("#filtro-texto").value.trim());
  const modo = $("#filtro-modo").value;
  const avaliacao = $("#filtro-avaliacao").value;
  const validacao = $("#filtro-validacao").value;
  return estado.interacoes.filter((r) => {
    if (validacao === "pendente" && (r.revisao || SEM_VALIDACAO.includes(r.modo))) return false;
    if (validacao && validacao !== "pendente" && !(r.revisao && r.revisao.veredito === validacao)) return false;
    if (modo && r.modo !== modo) return false;
    if (avaliacao === "util" && !(r.avaliacao && r.avaliacao.util)) return false;
    if (avaliacao === "nao_util" && !(r.avaliacao && !r.avaliacao.util)) return false;
    if (avaliacao === "sem" && r.avaliacao) return false;
    return !termo || [r.nome, r.pergunta, r.resposta].some((t) => normalizar(t).includes(termo));
  });
}

function limparFiltros() {
  $("#filtro-texto").value = "";
  $("#filtro-modo").value = "";
  $("#filtro-avaliacao").value = "";
  $("#filtro-validacao").value = "";
  estado.pagina = 1;
  renderizarLogs();
}

function linhaInteracao(registro) {
  const tr = criar("tr", "linha-interacao");
  tr.appendChild(criar("td", "col-data", formatarDataHora(registro.data_hora)));

  const nome = criar("td", "col-nome");
  if (registro.nome) {
    const texto = criar("span", "nome-texto", registro.nome);
    texto.title = registro.nome;
    nome.appendChild(texto);
  } else {
    nome.appendChild(criar("span", "sem-nome", "Não informado"));
  }
  tr.appendChild(nome);

  const pergunta = criar("td");
  const abrir = criar("button", "abrir-detalhe", registro.pergunta);
  abrir.type = "button";
  pergunta.appendChild(abrir);
  tr.appendChild(pergunta);

  const modo = criar("td", "col-modo");
  modo.appendChild(seloModo(registro.modo));
  tr.appendChild(modo);

  const avaliacao = criar("td", "col-avaliacao");
  avaliacao.appendChild(seloAvaliacao(registro.avaliacao));
  tr.appendChild(avaliacao);

  const validacao = criar("td", "col-avaliacao");
  validacao.appendChild(seloValidacao(registro.revisao, registro.modo));
  tr.appendChild(validacao);

  tr.appendChild(criar("td", "col-tempo num", formatarTempo(registro.tempo_ms)));
  tr.addEventListener("click", () => abrirDetalhe(registro));
  return tr;
}

function renderizarLogs() {
  const corpo = $("#logs-corpo");
  const itens = filtrarInteracoes();
  const totalPaginas = Math.max(1, Math.ceil(itens.length / POR_PAGINA));
  estado.pagina = Math.min(estado.pagina, totalPaginas);

  if (!estado.interacoes.length) {
    linhaEstado(corpo, 7, "Ainda não há conversas registradas.");
  } else if (!itens.length) {
    linhaEstado(corpo, 7, "Nenhuma conversa corresponde aos filtros.", {
      rotulo: "Limpar filtros",
      executar: limparFiltros,
    });
  } else {
    const inicio = (estado.pagina - 1) * POR_PAGINA;
    corpo.replaceChildren(...itens.slice(inicio, inicio + POR_PAGINA).map(linhaInteracao));
  }

  const total = estado.interacoes.length;
  $("#logs-contagem").textContent = itens.length === total
    ? plural(total, "interação", "interações")
    : `${plural(itens.length, "interação encontrada", "interações encontradas")} de ${total.toLocaleString("pt-BR")}`;

  $("#paginacao").hidden = totalPaginas <= 1;
  $("#pagina-info").textContent = `Página ${estado.pagina} de ${totalPaginas}`;
  $("#pagina-anterior").disabled = estado.pagina <= 1;
  $("#pagina-proxima").disabled = estado.pagina >= totalPaginas;
}

async function carregarLogs() {
  const botao = $("#atualizar-logs");
  botao.disabled = true;
  if (!estado.interacoes.length) linhaEstado($("#logs-corpo"), 7, "Carregando conversas…");
  try {
    const dados = await api(`/admin/logs?limite=${LIMITE_LOGS}`);
    estado.interacoes = dados.interacoes || [];
    renderizarKpis(dados);
    preencherFiltroModo();
    renderizarLogs();
    if (dados.total_interacoes > estado.interacoes.length) {
      $("#logs-contagem").textContent += ` (exibindo as ${LIMITE_LOGS.toLocaleString("pt-BR")} mais recentes)`;
    }
  } catch (erro) {
    if (tratarSessao(erro)) return;
    linhaEstado($("#logs-corpo"), 7, erro.message, { rotulo: "Tentar novamente", executar: carregarLogs });
  } finally {
    botao.disabled = false;
  }
}

function bloco(titulo, conteudo) {
  const secao = criar("section", "bloco");
  secao.appendChild(criar("h3", null, titulo));
  secao.appendChild(conteudo);
  return secao;
}

function abrirDetalhe(registro) {
  $("#detalhe-titulo").textContent = registro.nome || "Aluno não informado";
  $("#detalhe-sub").textContent = formatarDataHora(registro.data_hora);

  const selos = criar("div", "detalhe-selos");
  selos.appendChild(seloModo(registro.modo));
  if (registro.avaliacao) selos.appendChild(seloAvaliacao(registro.avaliacao));
  if (registro.revisao || !SEM_VALIDACAO.includes(registro.modo)) selos.appendChild(seloValidacao(registro.revisao));
  selos.appendChild(selo(formatarTempo(registro.tempo_ms)));

  const partes = [
    selos,
    bloco("Pergunta", criar("div", "bloco-texto bloco-pergunta", registro.pergunta)),
    bloco("Resposta", criar("div", "bloco-texto", registro.resposta)),
  ];

  const comentario = registro.avaliacao && registro.avaliacao.comentario;
  if (comentario) partes.push(bloco("Comentário do aluno", criar("div", "bloco-texto", comentario)));

  if (Array.isArray(registro.trechos) && registro.trechos.length) {
    const lista = criar("ul", "lista-fontes");
    registro.trechos.forEach((trecho) => {
      const item = criar("li");
      const origem = criar("div", null, trecho.documento || trecho.arquivo || "Documento");
      if (trecho.secao) origem.appendChild(criar("small", null, trecho.secao));
      item.appendChild(origem);
      if (trecho.similaridade !== undefined) {
        const valor = Number(trecho.similaridade).toLocaleString("pt-BR", {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        });
        const similaridade = criar("span", "similaridade", valor);
        similaridade.title = "Similaridade com a pergunta";
        item.appendChild(similaridade);
      }
      lista.appendChild(item);
    });
    partes.push(bloco("Trechos recuperados", lista));
  }

  partes.push(bloco("Validação da equipe", formularioValidacao(registro)));
  if (registro.sessao_id) partes.push(criar("p", "sessao", `Sessão ${registro.sessao_id}`));
  $("#detalhe-conteudo").replaceChildren(...partes);
  $("#detalhe").showModal();
  $("#detalhe").scrollTop = 0;
}

// ---------------------------------------------------------------- validação humana
function formularioValidacao(registro) {
  const caixa = criar("div", "validacao");
  if (registro.revisao) {
    const atual = criar("p", "validacao-atual");
    atual.append(seloValidacao(registro.revisao), document.createTextNode(
      ` por ${registro.revisao.revisor || "equipe"} em ${formatarDataHora(registro.revisao.data_hora)}`));
    caixa.appendChild(atual);
    if (registro.revisao.comentario) caixa.appendChild(criar("p", "bloco-texto validacao-comentario", registro.revisao.comentario));
  } else if (SEM_VALIDACAO.includes(registro.modo)) {
    caixa.appendChild(criar("p", "subtitulo", "Saudações e pedidos de esclarecimento não precisam de validação."));
  }

  const opcoes = criar("div", "opcoes-veredito");
  opcoes.setAttribute("role", "radiogroup");
  opcoes.setAttribute("aria-label", "A resposta está");
  let escolhido = registro.revisao ? registro.revisao.veredito : "";
  const salvar = criar("button", "btn btn-primario", registro.revisao ? "Atualizar validação" : "Salvar validação");
  salvar.type = "button";
  salvar.disabled = !escolhido;
  const botoes = Object.entries(VEREDITOS).map(([valor, info]) => {
    const botao = criar("button", `btn opcao-veredito opcao-${valor}`, info.rotulo);
    botao.type = "button";
    botao.setAttribute("role", "radio");
    botao.setAttribute("aria-checked", String(valor === escolhido));
    botao.addEventListener("click", () => {
      escolhido = valor;
      botoes.forEach((b) => b.setAttribute("aria-checked", String(b === botao)));
      salvar.disabled = false;
    });
    return botao;
  });
  opcoes.append(...botoes);

  const comentario = criar("textarea");
  comentario.rows = 2;
  comentario.maxLength = 1000;
  comentario.placeholder = "O que está errado ou faltando (opcional)";
  const campoComentario = criar("label", "campo", "Comentário");
  campoComentario.appendChild(comentario);

  const revisor = criar("input");
  revisor.type = "text";
  revisor.maxLength = 80;
  revisor.placeholder = "Ex.: Secretaria – Ana";
  revisor.value = lerPreferencia("revisor-nome", "");
  const campoRevisor = criar("label", "campo", "Quem está validando");
  campoRevisor.appendChild(revisor);

  salvar.addEventListener("click", async () => {
    salvar.disabled = true;
    salvar.textContent = "Salvando…";
    gravarPreferencia("revisor-nome", revisor.value.trim());
    try {
      const { revisao } = await api("/admin/revisoes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          interacao_id: registro.id,
          veredito: escolhido,
          revisor: revisor.value.trim() || null,
          comentario: comentario.value.trim() || null,
        }),
      });
      registro.revisao = revisao;
      avisar("Validação registrada.");
      renderizarLogs();
      carregarPainel();
      abrirDetalhe(registro);
    } catch (erro) {
      if (tratarSessao(erro)) return;
      avisar(erro.message, true);
      salvar.disabled = false;
      salvar.textContent = "Salvar validação";
    }
  });

  const campos = criar("div", "validacao-campos");
  campos.append(campoComentario, campoRevisor);
  caixa.append(criar("p", "validacao-pergunta", "A resposta está correta segundo os documentos oficiais?"),
    opcoes, campos, salvar);
  return caixa;
}

async function abrirInteracao(id) {
  let registro = estado.interacoes.find((r) => r.id === id);
  if (!registro) {
    await carregarLogs();
    registro = estado.interacoes.find((r) => r.id === id);
  }
  if (registro) abrirDetalhe(registro);
  else avisar("Conversa não encontrada entre as mais recentes.", true);
}

// ---------------------------------------------------------------- painel: dados
const PERIODOS = {
  hoje: "hoje", "7d": "últimos 7 dias", "30d": "últimos 30 dias", "90d": "últimos 90 dias", tudo: "todo o período",
};
const SERIES_DIAS = [
  { chave: "respondidas", rotulo: "Respondidas", cor: "var(--serie-1)" },
  { chave: "sem_resultado", rotulo: "Sem resposta nos documentos", cor: "var(--serie-2)" },
  { chave: "modelo_indisponivel", rotulo: "Modelo indisponível", cor: "var(--serie-3)" },
];

function porcentagem(taxa) {
  if (taxa === null || taxa === undefined || Number.isNaN(taxa)) return "—";
  return `${(taxa * 100).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}%`;
}

function aplicarPeriodo() {
  document.querySelectorAll(".periodo").forEach((botao) => {
    botao.setAttribute("aria-checked", String(botao.dataset.periodo === estado.periodo));
  });
}

async function carregarPainel() {
  aplicarPeriodo();
  const conteudo = $("#painel-conteudo");
  conteudo.classList.add("recarregando");
  $("#atualizar-painel").disabled = true;
  try {
    estado.painel = await api(`/admin/dashboard?periodo=${estado.periodo}`);
    renderizarPainel();
  } catch (erro) {
    if (tratarSessao(erro)) return;
    avisar(erro.message, true);
  } finally {
    conteudo.classList.remove("recarregando");
    $("#atualizar-painel").disabled = false;
  }
}

// ---------------------------------------------------------------- painel: indicadores
function renderizarStatusModelo(d) {
  const ok = d.modelo.disponivel;
  const caixa = $("#status-modelo");
  caixa.className = `status-modelo ${ok ? "status-ok" : "status-falha"}`;
  const icone = criar("span", "status-icone", ok ? "✓" : "✕");
  icone.setAttribute("aria-hidden", "true");
  const texto = criar("div");
  const linha = criar("div");
  linha.append(
    criar("strong", null, ok ? "Modelo de linguagem disponível" : "Modelo de linguagem indisponível"),
    criar("span", "subtitulo", ` · ${d.modelo.nome} (${d.modelo.api === "ollama" ? "local, Ollama" : "API"})`),
  );
  const detalhes = [];
  if (d.ultima_interacao) detalhes.push(`Última pergunta em ${formatarDataHora(d.ultima_interacao)}`);
  detalhes.push(d.ultima_falha_modelo
    ? `última falha do modelo em ${formatarDataHora(d.ultima_falha_modelo)}`
    : "nenhuma falha do modelo no período");
  texto.append(linha, criar("div", "subtitulo", detalhes.join(" · ")));
  caixa.replaceChildren(icone, texto);
}

function renderizarIndicadores(d) {
  const avaliadas = d.alunos.uteis + d.alunos.nao_uteis;
  const validadas = d.validacao.correta + d.validacao.parcial + d.validacao.incorreta;
  const indicadores = [
    ["Perguntas", d.totais.perguntas.toLocaleString("pt-BR"),
      `${plural(d.totais.interacoes, "interação", "interações")} · ${plural(d.totais.alunos, "aluno", "alunos")}`],
    ["Taxa de resposta", porcentagem(d.desfechos.taxa_resposta),
      `${d.desfechos.sem_resultado.toLocaleString("pt-BR")} sem resposta · ${d.desfechos.modelo_indisponivel.toLocaleString("pt-BR")} sem modelo`],
    ["Aprovação dos alunos", porcentagem(d.alunos.taxa_util),
      avaliadas ? `de ${plural(avaliadas, "avaliação", "avaliações")}` : "Nenhuma avaliação no período"],
    ["Validadas como corretas", porcentagem(d.validacao.taxa_correta),
      `${plural(validadas, "validada", "validadas")} · ${plural(d.validacao.pendentes, "pendente", "pendentes")}`],
    ["Tempo mediano", d.tempo.mediano_ms === null ? "—" : formatarTempo(d.tempo.mediano_ms),
      [
        d.tempo.modelo_medio_ms === null ? "por pergunta" : `modelo: média de ${formatarTempo(d.tempo.modelo_medio_ms)}`,
        d.tempo.primeiro_token_mediano_ms == null ? "" : `1º token: ${formatarTempo(d.tempo.primeiro_token_mediano_ms)}`,
        d.tempo.respostas_pela_reserva ? `${d.tempo.respostas_pela_reserva} pela reserva` : "",
      ].filter(Boolean).join(" · ")],
  ];
  $("#painel-kpis").replaceChildren(...indicadores.map(([rotulo, valor, nota]) => {
    const cartao = criar("div", "kpi");
    cartao.append(criar("div", "kpi-rotulo", rotulo), criar("div", "kpi-valor", valor));
    if (nota) cartao.appendChild(criar("div", "kpi-nota", nota));
    return cartao;
  }));
}

// Lista de medidores: nome, barra e número em cada linha, barras alinhadas, uma cor só.
function listaMedidores(alvo, linhas, vazio) {
  const total = linhas.reduce((soma, l) => soma + l.valor, 0);
  if (!total) {
    $(alvo).replaceChildren(criar("p", "vazio", vazio));
    return;
  }
  const maior = Math.max(...linhas.map((l) => l.valor));
  $(alvo).replaceChildren(...linhas.map((linha) => {
    const item = criar("div", "medidor");
    const trilho = criar("div", "medidor-trilho");
    const barra = criar("span", "medidor-barra");
    barra.style.width = linha.valor ? `${(linha.valor / maior) * 100}%` : "0";
    trilho.appendChild(barra);
    const numero = criar("span", "medidor-numero");
    numero.append(criar("strong", null, linha.valor.toLocaleString("pt-BR")),
      document.createTextNode(` · ${porcentagem(linha.valor / total)}`));
    item.append(criar("span", "medidor-nome", linha.rotulo), trilho, numero);
    item.title = `${linha.rotulo}: ${linha.valor.toLocaleString("pt-BR")} (${porcentagem(linha.valor / total)})`;
    return item;
  }));
}

function renderizarListas(d) {
  const modos = Object.entries(d.por_modo || {})
    .map(([modo, valor]) => ({ rotulo: (MODOS[modo] || { rotulo: modo }).rotulo, valor }))
    .sort((a, b) => b.valor - a.valor);
  listaMedidores("#grafico-modos", modos, "Nenhuma interação no período.");
  listaMedidores("#medidores-alunos", [
    { rotulo: "Útil", valor: d.alunos.uteis },
    { rotulo: "Não útil", valor: d.alunos.nao_uteis },
  ], "Nenhum aluno avaliou respostas no período.");
  // Percentuais sobre as respostas já validadas, como o indicador do topo; as pendentes ficam à parte.
  listaMedidores("#medidores-validacao", [
    { rotulo: "Correta", valor: d.validacao.correta },
    { rotulo: "Parcial", valor: d.validacao.parcial },
    { rotulo: "Incorreta", valor: d.validacao.incorreta },
  ], "Nenhuma resposta validada no período.");
  if (d.validacao.pendentes) {
    $("#medidores-validacao").appendChild(criar("p", "medidor-rodape",
      `${plural(d.validacao.pendentes, "pergunta aguarda", "perguntas aguardam")} validação (lista abaixo).`));
  }
}

function renderizarTabelasPainel(d) {
  const revisar = $("#revisar-corpo");
  if (!d.para_revisar.length) {
    linhaEstado(revisar, 3, "Nenhuma resposta pendente de validação no período.");
  } else {
    revisar.replaceChildren(...d.para_revisar.map((item) => {
      const tr = criar("tr", "linha-interacao");
      tr.appendChild(criar("td", "col-data", formatarDataHora(item.data_hora)));
      const pergunta = criar("td");
      const abrir = criar("button", "abrir-detalhe", item.pergunta);
      abrir.type = "button";
      pergunta.appendChild(abrir);
      tr.appendChild(pergunta);
      tr.appendChild(criar("td", "col-motivo", item.motivo));
      tr.addEventListener("click", () => abrirInteracao(item.id));
      return tr;
    }));
  }
  const semResposta = $("#sem-resposta-corpo");
  if (!d.sem_resposta_frequentes.length) {
    linhaEstado(semResposta, 3, "Nenhuma pergunta ficou sem resposta no período.");
  } else {
    semResposta.replaceChildren(...d.sem_resposta_frequentes.map((item) => {
      const tr = criar("tr");
      tr.append(criar("td", null, item.pergunta), criar("td", "num", item.vezes.toLocaleString("pt-BR")),
        criar("td", "col-data", formatarDataHora(item.ultima)));
      return tr;
    }));
  }
}

function renderizarPainel() {
  const d = estado.painel;
  const [data, hora] = formatarDataHora(d.gerado_em).split(" ");
  const intervalo = d.periodo.inicio ? ` (desde ${formatarData(d.periodo.inicio)})` : "";
  $("#painel-atualizado").textContent = `Atualizado em ${data} às ${hora} · ${PERIODOS[d.periodo.codigo]}${intervalo}`;
  renderizarStatusModelo(d);
  renderizarIndicadores(d);
  renderizarListas(d);
  renderizarTabelasPainel(d);
  desenharGraficos();
}

// ---------------------------------------------------------------- painel: gráficos em SVG
const SVG = "http://www.w3.org/2000/svg";

function svg(tag, atributos = {}) {
  const el = document.createElementNS(SVG, tag);
  Object.entries(atributos).forEach(([nome, valor]) => el.setAttribute(nome, valor));
  return el;
}

// Coluna com a ponta de dados arredondada (4px) e a base reta.
function caminhoColuna(x, y, largura, altura, arredondar) {
  const r = arredondar ? Math.min(4, largura / 2, altura) : 0;
  return `M${x},${y + altura}V${y + r}Q${x},${y} ${x + r},${y}H${x + largura - r}Q${x + largura},${y} ${x + largura},${y + r}V${y + altura}Z`;
}

function escalaLimpa(maximo) {
  if (maximo <= 4) return { topo: Math.max(maximo, 1), passo: 1 };
  const bruto = maximo / 4;
  const base = 10 ** Math.floor(Math.log10(bruto));
  const passo = [1, 2, 5, 10].map((mult) => mult * base).find((p) => p >= bruto);
  return { topo: Math.ceil(maximo / passo) * passo, passo };
}

function mostrarDica(evento, titulo, linhas) {
  const dica = $("#dica");
  const cabeca = criar("div", "dica-titulo", titulo);
  const corpo = linhas.map(({ rotulo, valor, cor }) => {
    const linha = criar("div", "dica-linha");
    if (cor) {
      const chave = criar("span", "dica-chave");
      chave.style.background = cor;
      linha.appendChild(chave);
    }
    linha.append(criar("strong", null, valor), criar("span", null, rotulo));
    return linha;
  });
  dica.replaceChildren(cabeca, ...corpo);
  dica.hidden = false;
  const alvo = evento.currentTarget.getBoundingClientRect();
  const x = evento.clientX || alvo.left + alvo.width / 2;
  const y = evento.clientY || alvo.top;
  dica.style.left = `${Math.min(window.innerWidth - dica.offsetWidth - 8, Math.max(8, x + 14))}px`;
  dica.style.top = `${Math.max(8, y - dica.offsetHeight - 12)}px`;
}

function esconderDica() {
  $("#dica").hidden = true;
}

// Colunas (empilhadas quando há mais de uma série), uma por categoria do eixo X.
function graficoColunas(alvo, categorias, series, opcoes) {
  const caixa = $(alvo);
  if (caixa.dataset.tabela === "1") {
    desenharTabela(caixa, categorias, series, opcoes);
    return;
  }
  const largura = Math.max(caixa.clientWidth, 280);
  const altura = 220;
  const m = { topo: 16, direita: 8, base: 26, esquerda: 34 };
  const areaL = largura - m.esquerda - m.direita;
  const areaA = altura - m.topo - m.base;
  const totais = categorias.map((_, i) => series.reduce((soma, s) => soma + s.valores[i], 0));
  const maiorTotal = Math.max(...totais, 0);
  const { topo, passo } = escalaLimpa(maiorTotal);
  const banda = areaL / categorias.length;
  const coluna = Math.max(2, Math.min(24, banda * 0.72));
  const y = (v) => m.topo + areaA - (v / topo) * areaA;

  const raiz = svg("svg", { viewBox: `0 0 ${largura} ${altura}`, width: largura, height: altura, role: "img",
    "aria-label": opcoes.descricao });
  for (let v = 0; v <= topo; v += passo) {
    raiz.appendChild(svg("line", { x1: m.esquerda, x2: largura - m.direita, y1: y(v), y2: y(v), class: "grade-linha" }));
    const rotulo = svg("text", { x: m.esquerda - 6, y: y(v) + 4, class: "eixo-texto", "text-anchor": "end" });
    rotulo.textContent = v.toLocaleString("pt-BR");
    raiz.appendChild(rotulo);
  }
  const salto = Math.ceil(categorias.length / Math.max(1, Math.floor(areaL / 46)));
  let rotulouMaior = false;
  categorias.forEach((categoria, i) => {
    const xBanda = m.esquerda + i * banda;
    const x = xBanda + (banda - coluna) / 2;
    const grupo = svg("g", { class: "coluna", tabindex: "0" });
    grupo.appendChild(svg("rect", { x: xBanda, y: m.topo, width: banda, height: areaA, class: "coluna-alvo" }));
    let acumulado = 0;
    const visiveis = series.filter((s) => s.valores[i] > 0);
    visiveis.forEach((serie, k) => {
      const valor = serie.valores[i];
      const yTopo = y(acumulado + valor);
      const vao = k > 0 ? 2 : 0; // 2px de superfície entre segmentos empilhados
      const h = Math.max(0, y(acumulado) - yTopo - vao);
      grupo.appendChild(svg("path", { d: caminhoColuna(x, yTopo, coluna, h, k === visiveis.length - 1), fill: serie.cor }));
      acumulado += valor;
    });
    if (i % salto === 0) {
      const rotulo = svg("text", { x: xBanda + banda / 2, y: altura - 8, class: "eixo-texto", "text-anchor": "middle" });
      rotulo.textContent = opcoes.rotuloX(categoria);
      raiz.appendChild(rotulo);
    }
    if (opcoes.rotularMaior && !rotulouMaior && totais[i] === maiorTotal && maiorTotal > 0) {
      rotulouMaior = true;
      const valor = svg("text", { x: x + coluna / 2, y: y(totais[i]) - 4, class: "valor-texto", "text-anchor": "middle" });
      valor.textContent = totais[i].toLocaleString("pt-BR");
      raiz.appendChild(valor);
    }
    const linhas = series.map((s) => ({
      rotulo: s.rotulo, valor: s.valores[i].toLocaleString("pt-BR"), cor: series.length > 1 ? s.cor : null,
    }));
    const titulo = opcoes.tituloDica(categoria, totais[i]);
    ["pointermove", "focus"].forEach((tipo) => grupo.addEventListener(tipo, (ev) => mostrarDica(ev, titulo, linhas)));
    ["pointerleave", "blur"].forEach((tipo) => grupo.addEventListener(tipo, esconderDica));
    raiz.appendChild(grupo);
  });
  caixa.replaceChildren(raiz);
}

function desenharTabela(caixa, categorias, series, opcoes) {
  const tabela = criar("table", "tabela tabela-dados");
  const cabeca = criar("tr");
  cabeca.appendChild(criar("th", null, opcoes.rotuloColuna));
  series.forEach((s) => cabeca.appendChild(criar("th", "num", s.rotulo)));
  const thead = criar("thead");
  thead.appendChild(cabeca);
  const tbody = criar("tbody");
  categorias.forEach((categoria, i) => {
    const tr = criar("tr");
    tr.appendChild(criar("td", null, opcoes.rotuloTabela(categoria)));
    series.forEach((s) => tr.appendChild(criar("td", "num", s.valores[i].toLocaleString("pt-BR"))));
    tbody.appendChild(tr);
  });
  tabela.append(thead, tbody);
  const rolagem = criar("div", "tabela-rolagem");
  rolagem.appendChild(tabela);
  caixa.replaceChildren(rolagem);
}

function desenharGraficos() {
  const d = estado.painel;
  if (!d || $("#secao-painel").hidden) return;
  const dias = d.por_dia.map((dia) => dia.data);
  const series = SERIES_DIAS.map((s) => ({ ...s, valores: d.por_dia.map((dia) => dia[s.chave]) }));
  $("#legenda-dias").replaceChildren(...series.map((s) => {
    const item = criar("span", "legenda-item");
    const amostra = criar("span", "legenda-amostra");
    amostra.style.background = s.cor;
    const total = s.valores.reduce((soma, v) => soma + v, 0);
    item.append(amostra, document.createTextNode(`${s.rotulo} `), criar("strong", null, total.toLocaleString("pt-BR")));
    return item;
  }));
  if (!dias.length) {
    $("#grafico-dias").replaceChildren(criar("p", "vazio", "Nenhuma pergunta no período."));
  } else {
    graficoColunas("#grafico-dias", dias, series, {
      descricao: "Perguntas por dia, por desfecho",
      rotuloX: (data) => formatarData(data).slice(0, 5),
      rotuloTabela: formatarData,
      rotuloColuna: "Dia",
      tituloDica: (data, total) => `${formatarData(data)} · ${plural(total, "pergunta", "perguntas")}`,
    });
  }
  const horas = [...Array(24).keys()];
  const doisDigitos = (h) => String(h).padStart(2, "0");
  if (!d.por_hora.some((v) => v > 0)) {
    $("#grafico-horas").replaceChildren(criar("p", "vazio", "Nenhuma pergunta no período."));
  } else {
    graficoColunas("#grafico-horas", horas, [{ rotulo: "Perguntas", cor: "var(--serie-1)", valores: d.por_hora }], {
      descricao: "Perguntas por hora do dia",
      rotuloX: (h) => `${h}h`,
      rotuloTabela: (h) => `${doisDigitos(h)}h às ${doisDigitos(h)}h59`,
      rotuloColuna: "Hora",
      rotularMaior: true,
      tituloDica: (h, total) => `${doisDigitos(h)}h às ${doisDigitos(h)}h59 · ${plural(total, "pergunta", "perguntas")}`,
    });
  }
}

function alternarTabela(botao) {
  const caixa = document.getElementById(botao.dataset.alvo);
  const tabela = caixa.dataset.tabela !== "1";
  caixa.dataset.tabela = tabela ? "1" : "";
  botao.textContent = tabela ? "Ver gráfico" : "Ver tabela";
  desenharGraficos();
}

function ligarPainel() {
  document.querySelectorAll(".periodo").forEach((botao) => {
    botao.addEventListener("click", () => {
      estado.periodo = botao.dataset.periodo;
      gravarPreferencia("painel-periodo", estado.periodo);
      carregarPainel();
    });
  });
  document.querySelectorAll(".alternar-tabela").forEach((botao) => {
    botao.addEventListener("click", () => alternarTabela(botao));
  });
  $("#atualizar-painel").addEventListener("click", carregarPainel);
  window.addEventListener("scroll", esconderDica, { passive: true, capture: true });
  let redimensionar;
  window.addEventListener("resize", () => {
    window.clearTimeout(redimensionar);
    redimensionar = window.setTimeout(desenharGraficos, 150);
  });
  // Atualiza sozinho a cada minuto enquanto o painel está aberto e visível.
  estado.temporizador = window.setInterval(() => {
    if (!$("#painel").hidden && secaoAtual() === "painel" && !document.hidden) carregarPainel();
  }, 60000);
}

// ---------------------------------------------------------------- documentos
function linhaDocumento(doc) {
  const tr = criar("tr");

  const titulo = criar("td");
  titulo.append(criar("div", "doc-titulo", doc.titulo), criar("div", "doc-arquivo", doc.arquivo));
  if (doc.avisos) titulo.appendChild(selo(plural(doc.avisos, "aviso de conversão", "avisos de conversão"), "selo-ambar"));
  tr.appendChild(titulo);

  const categoria = criar("td", "col-categoria");
  categoria.appendChild(doc.categoria ? selo(doc.categoria) : criar("span", "traco", "—"));
  tr.appendChild(categoria);

  tr.appendChild(criar("td", "col-atualizado", formatarData(doc.atualizado_em) || "—"));
  const blocos = criar("td", "col-tamanho num", Number(doc.blocos || 0).toLocaleString("pt-BR"));
  if (!doc.blocos) blocos.title = "Nenhum bloco: o documento não é usado nas respostas.";
  tr.appendChild(blocos);
  tr.appendChild(criar("td", "col-tamanho num", formatarTamanho(doc.tamanho_kb)));

  const acao = criar("td", "col-acao");
  const ver = criar("button", "btn", "Ver");
  ver.type = "button";
  ver.setAttribute("aria-label", `Ver o conteúdo extraído de ${doc.titulo}`);
  ver.addEventListener("click", () => abrirDocumento(doc));
  acao.appendChild(ver);
  const remover = criar("button", "btn btn-texto-perigo", "Remover");
  remover.type = "button";
  remover.setAttribute("aria-label", `Remover ${doc.titulo}`);
  remover.addEventListener("click", () => pedirRemocao(doc));
  acao.appendChild(remover);
  tr.appendChild(acao);
  return tr;
}

function renderizarDocumentos() {
  const corpo = $("#docs-corpo");
  const termo = normalizar($("#filtro-docs").value.trim());
  const itens = estado.documentos.filter((d) =>
    !termo || [d.titulo, d.arquivo, d.categoria].some((t) => normalizar(t).includes(termo)));
  if (!estado.documentos.length) {
    linhaEstado(corpo, 6, "Nenhum documento na base. Envie um PDF para começar.");
  } else if (!itens.length) {
    linhaEstado(corpo, 6, "Nenhum documento corresponde à busca.");
  } else {
    corpo.replaceChildren(...itens.map(linhaDocumento));
  }
}

async function carregarDocumentos() {
  if (!estado.documentos.length) linhaEstado($("#docs-corpo"), 6, "Carregando documentos…");
  try {
    const { documentos } = await api("/admin/documentos");
    estado.documentos = documentos;
    renderizarDocumentos();
    const categorias = [...new Set(documentos.map((d) => d.categoria).filter(Boolean))].sort();
    $("#categorias").replaceChildren(...categorias.map((c) => {
      const opcao = criar("option");
      opcao.value = c;
      return opcao;
    }));
    carregarResumoBase();
  } catch (erro) {
    if (tratarSessao(erro)) return;
    linhaEstado($("#docs-corpo"), 6, erro.message, { rotulo: "Tentar novamente", executar: carregarDocumentos });
  }
}

async function carregarResumoBase() {
  try {
    const saude = await api("/health");
    const data = formatarDataHora(saude.ultima_indexacao);
    $("#docs-resumo").textContent = [
      plural(saude.n_documentos, "documento indexado", "documentos indexados"),
      plural(saude.n_blocos, "bloco", "blocos"),
      data ? `última indexação em ${data}` : "",
    ].filter(Boolean).join(" · ");
  } catch { /* o resumo é só informativo */ }
}

const FORMATOS = {
  markdown: "Markdown (.md)",
  blocos: "Blocos para IA (.jsonl)",
  json: "Estrutura Docling (.json)",
  pdf: "PDF original",
};

function resumoRelatorio(relatorio) {
  const itens = [
    ["Páginas", relatorio.paginas || "—"],
    ["Seções", relatorio.secoes],
    ["Blocos", relatorio.blocos],
    ["Com tabela", relatorio.blocos_com_tabela],
    ["Tokens", Number(relatorio.tokens || 0).toLocaleString("pt-BR")],
    ["Tokens por bloco", relatorio.tokens_por_bloco],
  ];
  const lista = criar("dl", "relatorio");
  itens.forEach(([rotulo, valor]) => {
    const item = criar("div");
    item.append(criar("dt", null, rotulo), criar("dd", null, String(valor)));
    lista.appendChild(item);
  });
  return lista;
}

function listaAvisos(avisos) {
  const lista = criar("ul", "lista-avisos");
  avisos.forEach((texto) => lista.appendChild(criar("li", null, texto)));
  return lista;
}

async function abrirDocumento(doc) {
  $("#detalhe-titulo").textContent = doc.titulo;
  $("#detalhe-sub").textContent = doc.arquivo;
  $("#detalhe-conteudo").replaceChildren(criar("p", "subtitulo", "Carregando o conteúdo extraído…"));
  $("#detalhe").showModal();
  $("#detalhe").scrollTop = 0;
  try {
    const dados = await api(`/admin/documentos/${encodeURIComponent(doc.arquivo)}/conteudo`);
    const partes = [bloco("Resultado da conversão", resumoRelatorio(dados.relatorio))];
    if (dados.relatorio.avisos.length) partes.push(bloco("Avisos", listaAvisos(dados.relatorio.avisos)));

    const downloads = criar("div", "downloads");
    dados.formatos.forEach((formato) => {
      const link = criar("a", "btn", FORMATOS[formato] || formato);
      link.href = `/admin/documentos/${encodeURIComponent(doc.arquivo)}/baixar?formato=${formato}`;
      link.setAttribute("download", "");
      downloads.appendChild(link);
    });
    partes.push(bloco("Baixar", downloads));

    const lista = criar("div", "lista-blocos");
    let secaoAtual = null;
    dados.blocos.forEach((item) => {
      if (item.secao !== secaoAtual) {
        secaoAtual = item.secao;
        lista.appendChild(criar("h4", "bloco-secao", item.secao));
      }
      const caixa = criar("details", "bloco-doc");
      const resumo = criar("summary", null, item.texto.slice(0, 110).replace(/\s+/g, " "));
      resumo.appendChild(criar("span", "similaridade", `${item.tokens} tokens`));
      caixa.appendChild(resumo);
      if (item.contexto) caixa.appendChild(criar("p", "bloco-contexto", `Contexto herdado: ${item.contexto}`));
      caixa.appendChild(criar("div", "bloco-texto", item.texto));
      lista.appendChild(caixa);
    });
    partes.push(bloco(`Blocos consultados pelo chatbot (${dados.blocos.length})`, lista));
    $("#detalhe-conteudo").replaceChildren(...partes);
  } catch (erro) {
    if (tratarSessao(erro)) return;
    $("#detalhe-conteudo").replaceChildren(criar("p", "alerta alerta-erro", erro.message));
  }
}

async function testarBusca(evento) {
  evento.preventDefault();
  const pergunta = $("#busca-pergunta").value.trim();
  const saida = $("#busca-resultado");
  if (!pergunta) {
    saida.replaceChildren(criar("p", "alerta alerta-erro", "Digite uma pergunta para testar."));
    return;
  }
  const botao = $("#busca-enviar");
  botao.disabled = true;
  botao.textContent = "Buscando…";
  try {
    const dados = await api("/admin/busca", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pergunta }),
    });
    const partes = [];
    if (dados.resposta_exata) {
      partes.push(criar("p", "alerta alerta-ok", "Respondida pelo código, sem o modelo (dado exato da grade):"));
      partes.push(criar("div", "bloco-texto", dados.resposta_exata));
    } else if (dados.esclarecimentos.length) {
      partes.push(criar("p", "alerta", `Antes de responder, o chatbot pergunta ao aluno: ${dados.esclarecimentos.join(", ")}.`));
    }
    if (!dados.trechos.length) {
      partes.push(criar("p", "alerta alerta-erro", "Nenhum trecho recuperado: o chatbot responderia que não localizou a informação."));
    }
    dados.trechos.forEach((trecho, posicao) => {
      const caixa = criar("details", "bloco-doc");
      const resumo = criar("summary", null, `${posicao + 1}. ${trecho.documento} – ${trecho.secao}`);
      resumo.appendChild(criar("span", "similaridade", Number(trecho.similaridade).toLocaleString("pt-BR", {
        minimumFractionDigits: 2, maximumFractionDigits: 2,
      })));
      caixa.append(resumo, criar("div", "bloco-texto", trecho.texto));
      partes.push(caixa);
    });
    saida.replaceChildren(...partes);
  } catch (erro) {
    if (tratarSessao(erro)) return;
    saida.replaceChildren(criar("p", "alerta alerta-erro", erro.message));
  } finally {
    botao.disabled = false;
    botao.textContent = "Buscar trechos";
  }
}

function pedirRemocao(doc) {
  estado.documentoEmRemocao = doc;
  $("#confirmar-texto").textContent = `"${doc.titulo}" (${doc.arquivo}) deixará de ser usado nas respostas.`;
  $("#confirmar-erro").hidden = true;
  $("#confirmar").showModal();
  $("#confirmar-cancelar").focus();
}

async function confirmarRemocao() {
  const doc = estado.documentoEmRemocao;
  if (!doc) return;
  const remover = $("#confirmar-remover");
  const cancelar = $("#confirmar-cancelar");
  remover.disabled = true;
  cancelar.disabled = true;
  remover.textContent = "Removendo e reindexando…";
  try {
    await api(`/admin/documentos/${encodeURIComponent(doc.arquivo)}`, { method: "DELETE" });
    estado.documentoEmRemocao = null;
    $("#confirmar").close();
    avisar(`"${doc.titulo}" foi removido da base.`);
    await carregarDocumentos();
  } catch (erro) {
    if (tratarSessao(erro)) return;
    const aviso = $("#confirmar-erro");
    aviso.textContent = erro.message;
    aviso.hidden = false;
  } finally {
    remover.disabled = false;
    cancelar.disabled = false;
    remover.textContent = "Remover documento";
  }
}

// ---------------------------------------------------------------- envio de PDF
const ZONA_TEXTO_INICIAL = $("#zona-texto").innerHTML;
const ZONA_DETALHE_INICIAL = $("#zona-detalhe").textContent;

const ESTADOS_FILA = {
  espera: { rotulo: "Na fila", classe: "" },
  enviando: { rotulo: "Convertendo…", classe: "selo-azul" },
  ok: { rotulo: "Indexado", classe: "selo-verde" },
  aviso: { rotulo: "Indexado com avisos", classe: "selo-ambar" },
  erro: { rotulo: "Falhou", classe: "selo-vermelho" },
};

function itemDaFila(arquivo, situacao = "espera", detalhe = "") {
  const item = criar("li", "fila-item");
  const texto = criar("div", "fila-texto");
  texto.append(criar("strong", null, arquivo.name), criar("small", null, detalhe || formatarTamanho(arquivo.size / 1024)));
  const info = ESTADOS_FILA[situacao];
  item.append(texto, selo(info.rotulo, info.classe));
  return item;
}

function renderizarFila(arquivos, situacoes = []) {
  const fila = $("#fila");
  fila.hidden = !arquivos.length;
  fila.replaceChildren(...arquivos.map((arquivo, i) => {
    const s = situacoes[i] || {};
    return itemDaFila(arquivo, s.situacao, s.detalhe);
  }));
}

function atualizarZona() {
  const arquivos = [...$("#up-arquivo").files];
  const zona = $("#zona");
  zona.classList.toggle("preenchida", arquivos.length > 0);
  // Título, data e URL descrevem um documento só; com vários arquivos, vale o nome de cada um.
  $("#up-titulo").disabled = arquivos.length > 1;
  $("#up-url").disabled = arquivos.length > 1;
  if (arquivos.length) {
    const total = arquivos.reduce((soma, a) => soma + a.size, 0);
    $("#zona-texto").replaceChildren(criar("strong", null,
      arquivos.length === 1 ? arquivos[0].name : plural(arquivos.length, "arquivo", "arquivos escolhidos")));
    $("#zona-detalhe").textContent = `${formatarTamanho(total / 1024)} · clique para trocar`;
  } else {
    $("#zona-texto").innerHTML = ZONA_TEXTO_INICIAL;
    $("#zona-detalhe").textContent = ZONA_DETALHE_INICIAL;
  }
  renderizarFila(arquivos.length > 1 ? arquivos : []);
}

function avisoUpload(texto, classe) {
  const aviso = $("#upload-aviso");
  aviso.textContent = texto || "";
  aviso.className = `alerta ${classe || ""}`.trim();
  aviso.setAttribute("role", classe === "alerta-erro" ? "alert" : "status");
  aviso.hidden = !texto;
}

function ehPdf(arquivo) {
  return arquivo.name.toLowerCase().endsWith(".pdf");
}

async function enviarPdf(evento) {
  evento.preventDefault();
  const arquivos = [...$("#up-arquivo").files];
  if (!arquivos.length) {
    avisoUpload("Escolha ao menos um arquivo PDF para enviar.", "alerta-erro");
    return;
  }
  if (!arquivos.every(ehPdf)) {
    avisoUpload("Só é possível enviar arquivos .pdf.", "alerta-erro");
    return;
  }
  if (!$("#up-url").checkValidity()) {
    avisoUpload("A URL da fonte deve começar com http:// ou https://.", "alerta-erro");
    $("#up-url").focus();
    return;
  }

  const unico = arquivos.length === 1;
  const campos = {
    titulo: unico ? $("#up-titulo").value : "",
    categoria: $("#up-categoria").value || "documento",
    url: unico ? $("#up-url").value : "",
    atualizado_em: $("#up-data").value,
  };
  const situacoes = arquivos.map(() => ({ situacao: "espera" }));

  const botao = $("#up-enviar");
  botao.disabled = true;
  botao.textContent = "Processando…";
  $("#upload-campos").disabled = true;
  $("#upload-progresso").hidden = false;
  avisoUpload("Convertendo e reindexando a base. Cada PDF pode levar alguns minutos; mantenha esta página aberta.");

  let falhas = 0;
  let comAvisos = 0;
  let ultimo = null;
  try {
    // Um arquivo por vez: a conversão usa muita memória e cada envio reindexa a base.
    for (let i = 0; i < arquivos.length; i += 1) {
      situacoes[i] = { situacao: "enviando" };
      renderizarFila(arquivos, situacoes);
      const dados = new FormData();
      dados.append("arquivo", arquivos[i]);
      Object.entries(campos).forEach(([chave, valor]) => dados.append(chave, valor));
      try {
        ultimo = await api("/admin/documentos", { method: "POST", body: dados });
        const r = ultimo.relatorio || { avisos: [] };
        const detalhe = `${plural(r.secoes || 0, "seção", "seções")} · ${plural(r.blocos || 0, "bloco", "blocos")}`;
        if (r.avisos.length) comAvisos += 1;
        situacoes[i] = { situacao: r.avisos.length ? "aviso" : "ok", detalhe: r.avisos[0] || detalhe };
      } catch (erro) {
        if (erro instanceof ErroSessao) throw erro;
        falhas += 1;
        situacoes[i] = { situacao: "erro", detalhe: erro.message };
      }
      renderizarFila(arquivos, situacoes);
    }
    const enviados = arquivos.length - falhas;
    $("#form-upload").reset();
    atualizarZona();
    renderizarFila(arquivos, situacoes);
    if (falhas) {
      avisoUpload(`${plural(enviados, "arquivo indexado", "arquivos indexados")} e ${plural(falhas, "falha", "falhas")}. Veja o motivo em cada item acima.`, "alerta-erro");
    } else {
      const base = ultimo ? ` A base agora tem ${plural(ultimo.n_blocos, "bloco", "blocos")}.` : "";
      const avisos = comAvisos ? ` ${plural(comAvisos, "documento tem", "documentos têm")} avisos de conversão: abra "Ver" para conferir.` : "";
      avisoUpload(`${plural(enviados, "arquivo indexado", "arquivos indexados")}.${base}${avisos}`, "alerta-ok");
    }
    await carregarDocumentos();
  } catch (erro) {
    if (tratarSessao(erro)) return;
    avisoUpload(erro.message, "alerta-erro");
  } finally {
    botao.disabled = false;
    botao.textContent = "Enviar e indexar";
    $("#upload-campos").disabled = false;
    $("#upload-progresso").hidden = true;
  }
}

function ligarArrastar() {
  const zona = $("#zona");
  ["dragenter", "dragover"].forEach((tipo) => zona.addEventListener(tipo, (ev) => {
    ev.preventDefault();
    zona.classList.add("arrastando");
  }));
  ["dragleave", "drop"].forEach((tipo) => zona.addEventListener(tipo, (ev) => {
    ev.preventDefault();
    zona.classList.remove("arrastando");
  }));
  zona.addEventListener("drop", (ev) => {
    if ($("#upload-campos").disabled) return;
    const arquivos = ev.dataTransfer.files;
    if (!arquivos.length) return;
    if (![...arquivos].every(ehPdf)) {
      avisoUpload("Só é possível enviar arquivos .pdf.", "alerta-erro");
      return;
    }
    $("#up-arquivo").files = arquivos;
    avisoUpload("");
    atualizarZona();
  });
}

// ---------------------------------------------------------------- eventos
function fecharAoClicarFora(dialogo) {
  dialogo.addEventListener("click", (ev) => {
    if (ev.target === dialogo && !$("#confirmar-remover").disabled) dialogo.close();
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  $("#form-login").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const aviso = $("#login-aviso");
    const botao = $("#entrar");
    aviso.hidden = true;
    botao.disabled = true;
    botao.textContent = "Entrando…";
    const email = $("#email").value;
    try {
      await api("/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, senha: $("#senha").value }),
      });
      $("#senha").value = "";
      mostrarPainel(email);
    } catch (erro) {
      aviso.textContent = erro.message;
      aviso.hidden = false;
      $("#senha").select();
    } finally {
      botao.disabled = false;
      botao.textContent = "Entrar";
    }
  });

  $("#sair").addEventListener("click", async () => {
    try {
      await api("/admin/logout", { method: "POST" });
    } catch { /* volta ao login de qualquer forma */ }
    estado.interacoes = [];
    estado.documentos = [];
    mostrarLogin();
  });

  window.addEventListener("hashchange", aplicarSecao);
  ligarPainel();

  ["#filtro-texto", "#filtro-modo", "#filtro-avaliacao", "#filtro-validacao"].forEach((seletor) => {
    $(seletor).addEventListener("input", () => {
      estado.pagina = 1;
      renderizarLogs();
    });
  });
  $("#atualizar-logs").addEventListener("click", carregarLogs);
  $("#pagina-anterior").addEventListener("click", () => {
    estado.pagina -= 1;
    renderizarLogs();
  });
  $("#pagina-proxima").addEventListener("click", () => {
    estado.pagina += 1;
    renderizarLogs();
  });

  $("#detalhe-fechar").addEventListener("click", () => $("#detalhe").close());
  fecharAoClicarFora($("#detalhe"));
  fecharAoClicarFora($("#confirmar"));
  $("#confirmar-cancelar").addEventListener("click", () => $("#confirmar").close());
  $("#confirmar-remover").addEventListener("click", confirmarRemocao);
  // Esc não fecha a confirmação enquanto a remoção está em andamento.
  $("#confirmar").addEventListener("cancel", (ev) => {
    if ($("#confirmar-remover").disabled) ev.preventDefault();
  });

  $("#filtro-docs").addEventListener("input", renderizarDocumentos);
  $("#form-upload").addEventListener("submit", enviarPdf);
  $("#form-busca").addEventListener("submit", testarBusca);
  $("#up-arquivo").addEventListener("change", () => {
    avisoUpload("");
    atualizarZona();
  });
  ligarArrastar();

  try {
    const { email } = await api("/admin/sessao");
    mostrarPainel(email);
  } catch {
    mostrarLogin();
  }
});
