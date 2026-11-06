"use strict";

const estado = { arquivos: [], csrfToken: null, promptOriginal: "" };
const $ = (seletor) => document.querySelector(seletor);
const extensoes = new Set(["pdf", "html", "htm", "md", "txt"]);

function escapeTexto(valor) {
  return String(valor ?? "").replace(/[&<>\"]/g, (caractere) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[caractere]));
}

function formatarTamanho(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1).replace(".", ",")} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1).replace(".", ",")} MB`;
}

function extensao(nome) { return String(nome).split(".").pop().toLowerCase(); }

function mostrarFeedback(mensagem, tipo) {
  const elemento = $("#feedback");
  elemento.textContent = mensagem;
  elemento.className = `feedback show ${tipo}`;
}

function mostrarMensagem(seletor, mensagem, tipo) {
  const elemento = $(seletor);
  elemento.textContent = mensagem;
  elemento.className = `feedback show ${tipo}`;
}

function mostrarLogin(mensagem = "") {
  estado.csrfToken = null;
  $("#admin-app").hidden = true;
  $("#sair-admin").hidden = true;
  $("#login-card").hidden = false;
  if (mensagem) mostrarMensagem("#login-feedback", mensagem, "error");
}

function mostrarDashboard(dados) {
  estado.csrfToken = dados.csrf_token;
  $("#login-card").hidden = true;
  $("#admin-app").hidden = false;
  $("#sair-admin").hidden = false;
  $("#login-senha").value = "";
  atualizarBotao();
}

async function fetchAdmin(url, opcoes = {}) {
  const cabecalhos = new Headers(opcoes.headers || {});
  if (estado.csrfToken && !["GET", "HEAD"].includes((opcoes.method || "GET").toUpperCase())) {
    cabecalhos.set("X-CSRF-Token", estado.csrfToken);
  }
  const resposta = await fetch(url, { ...opcoes, headers: cabecalhos, credentials: "same-origin" });
  if (resposta.status === 401) mostrarLogin("Sua sessão terminou. Entre novamente.");
  return resposta;
}

function atualizarPromptBotao() {
  const valor = $("#prompt-sistema").value;
  $("#prompt-contagem").textContent = `${valor.length} / 4000 caracteres`;
  $("#salvar-prompt").disabled = !estado.csrfToken || valor.trim().length < 20 || valor.length > 4000 || valor === estado.promptOriginal;
}

async function carregarPrompt() {
  try {
    const resposta = await fetchAdmin("/admin/config");
    const dados = await resposta.json().catch(() => ({}));
    if (!resposta.ok) throw new Error(dados.detail || "Não foi possível carregar o prompt.");
    estado.promptOriginal = dados.prompt_sistema || "";
    $("#prompt-sistema").value = estado.promptOriginal;
    atualizarPromptBotao();
  } catch (erro) {
    mostrarMensagem("#prompt-feedback", erro.message, "error");
  }
}

async function verificarSessao() {
  try {
    const resposta = await fetch("/admin/auth/session", { credentials: "same-origin" });
    if (!resposta.ok) {
      mostrarLogin(resposta.status === 503 ? "Configure ADMIN_EMAIL e ADMIN_PASSWORD no arquivo .env e reinicie o servidor." : "");
      return;
    }
    mostrarDashboard(await resposta.json());
    await Promise.all([carregarDocumentos(), carregarPrompt()]);
  } catch (erro) {
    mostrarLogin("Não foi possível verificar a sessão. Tente novamente.");
  }
}

function atualizarBotao() {
  $("#enviar-arquivos").disabled = estado.arquivos.length === 0 || !estado.csrfToken;
}

function renderizarFila() {
  const fila = $("#fila");
  const lista = $("#lista-arquivos");
  lista.replaceChildren();
  fila.hidden = estado.arquivos.length === 0;
  estado.arquivos.forEach((arquivo, indice) => {
    const item = document.createElement("div");
    item.className = "file-item";
    item.innerHTML = `<span class="file-type">${escapeTexto(extensao(arquivo.name).toUpperCase())}</span><span class="file-meta"><strong class="file-name">${escapeTexto(arquivo.name)}</strong><small class="file-size">${formatarTamanho(arquivo.size)}</small></span><button class="remove-file" type="button" data-indice="${indice}" aria-label="Remover ${escapeTexto(arquivo.name)}">×</button>`;
    lista.appendChild(item);
  });
  atualizarBotao();
}

function adicionarArquivos(arquivos) {
  const validos = Array.from(arquivos).filter((arquivo) => {
    const valido = extensoes.has(extensao(arquivo.name)) && arquivo.size <= 25 * 1024 * 1024;
    if (!valido) mostrarFeedback(`${arquivo.name}: use PDF, HTML, MD ou TXT com até 25 MB.`, "error");
    return valido;
  });
  const nomes = new Set(estado.arquivos.map((arquivo) => `${arquivo.name}:${arquivo.size}`));
  validos.forEach((arquivo) => { if (!nomes.has(`${arquivo.name}:${arquivo.size}`)) estado.arquivos.push(arquivo); });
  renderizarFila();
}

function formatarData(valor) {
  if (!valor) return "—";
  const data = new Date(valor.length === 10 ? `${valor}T12:00:00` : valor);
  return Number.isNaN(data.getTime()) ? valor : data.toLocaleDateString("pt-BR");
}

function nomeBackend(dados) {
  if (dados.vector_store === "qdrant") return "QDRANT";
  return String(dados.busca || "LOCAL").toUpperCase();
}

function atualizarBackend(dados) {
  const seletor = $("#vector-store");
  if (seletor && dados.vector_store) seletor.value = dados.vector_store;
  $("#stat-busca").textContent = nomeBackend(dados);
  $("#backend-feedback").textContent = dados.vector_store === "qdrant"
    ? "Usando a coleção Qdrant configurada no servidor."
    : `Usando índice local com ${String(dados.busca || "FAISS").toUpperCase()}.`;
}

async function removerDocumento(arquivo, titulo) {
  if (!window.confirm(`Remover “${titulo}”? O arquivo e os vetores deste documento serão apagados.`)) return;

  mostrarFeedback(`Removendo ${titulo}…`, "loading");
  try {
    const resposta = await fetchAdmin(`/admin/documentos/${encodeURIComponent(arquivo)}`, {
      method: "DELETE",
    });
    const corpo = await resposta.json().catch(() => ({}));
    if (!resposta.ok) throw new Error(corpo.detail || "Não foi possível remover o documento.");
    mostrarFeedback(`Documento removido e somente seus vetores foram excluídos: ${titulo}.`, "success");
    await carregarDocumentos();
  } catch (erro) {
    mostrarFeedback(erro.message, "error");
  }
}

async function reindexarDocumento(arquivo, titulo) {
  if (!window.confirm(`Reindexar “${titulo}”? Apenas os vetores deste documento serão atualizados.`)) return;

  mostrarFeedback(`Reindexando ${titulo}…`, "loading");
  try {
    const resposta = await fetchAdmin(`/admin/documentos/${encodeURIComponent(arquivo)}/reindexar`, {
      method: "POST",
    });
    const corpo = await resposta.json().catch(() => ({}));
    if (!resposta.ok) throw new Error(corpo.detail || "Não foi possível reindexar o documento.");
    mostrarFeedback(`Documento reindexado sem apagar os demais: ${titulo}. ${corpo.n_blocos} blocos na coleção.`, "success");
    await carregarDocumentos();
  } catch (erro) {
    mostrarFeedback(erro.message, "error");
  }
}

async function carregarDocumentos() {
  try {
    const resposta = await fetch("/health");
    if (!resposta.ok) throw new Error("Falha ao consultar a base");
    const dados = await resposta.json();
    $("#stat-documentos").textContent = dados.n_documentos ?? "—";
    $("#stat-blocos").textContent = dados.n_blocos ?? "—";
    $("#stat-atualizacao").textContent = formatarData(dados.ultima_indexacao);
    atualizarBackend(dados);
    const lista = $("#documentos");
    lista.replaceChildren();
    (dados.documentos || []).forEach((documento) => {
      const item = document.createElement("div");
      item.className = "document-item";
      const nome = String(documento.titulo || "Documento");
      const icone = document.createElement("span");
      icone.className = "doc-icon";
      icone.textContent = extensao(documento.arquivo || "doc").toUpperCase();
      const info = document.createElement("span");
      info.className = "doc-info";
      const titulo = document.createElement("strong");
      titulo.title = nome;
      titulo.textContent = nome;
      const detalhe = document.createElement("small");
      detalhe.textContent = `${documento.arquivo || "arquivo não informado"} · ${formatarData(documento.atualizado_em)}${documento.url ? " · fonte oficial" : ""}`;
      info.append(titulo, detalhe);
      const remover = document.createElement("button");
      remover.className = "delete-document";
      remover.type = "button";
      remover.textContent = "Remover";
      remover.title = `Remover ${nome}`;
      remover.addEventListener("click", () => removerDocumento(documento.arquivo, nome));
      const reindexar = document.createElement("button");
      reindexar.className = "reindex-document";
      reindexar.type = "button";
      reindexar.textContent = "Reindexar";
      reindexar.title = `Reindexar ${nome}`;
      reindexar.addEventListener("click", () => reindexarDocumento(documento.arquivo, nome));
      const acoes = document.createElement("span");
      acoes.className = "doc-actions";
      acoes.append(reindexar, remover);
      item.append(icone, info, acoes);
      lista.appendChild(item);
    });
    if (!lista.children.length) lista.innerHTML = '<div class="empty-state">Nenhum documento indexado.</div>';
  } catch (erro) {
    $("#documentos").innerHTML = '<div class="empty-state">Não foi possível carregar a lista agora.</div>';
    mostrarFeedback("Não foi possível consultar o estado da base.", "error");
    console.error(erro);
  }
}

async function enviarArquivo(arquivo, indice, total) {
  const dados = new FormData();
  dados.append("arquivo", arquivo, arquivo.name);
  const titulo = $("#titulo").value.trim();
  dados.append("titulo", total === 1 ? titulo : "");
  dados.append("categoria", $("#categoria").value.trim());
  dados.append("origem", $("#origem").value.trim());
  dados.append("url", $("#url").value.trim());
  dados.append("atualizado_em", $("#atualizado-em").value);
  const resposta = await fetchAdmin("/admin/upload", {
    method: "POST",
    body: dados,
  });
  const corpo = await resposta.json().catch(() => ({}));
  if (!resposta.ok) throw new Error(corpo.detail || `Falha ao enviar ${arquivo.name}`);
  mostrarFeedback(`Arquivo ${indice + 1} de ${total} indexado: ${arquivo.name}`, "loading");
  return corpo;
}

document.addEventListener("DOMContentLoaded", () => {
  const dropzone = $("#dropzone");
  const input = $("#arquivos");
  input.addEventListener("change", () => { adicionarArquivos(input.files); input.value = ""; });
  ["dragenter", "dragover"].forEach((evento) => dropzone.addEventListener(evento, (e) => { e.preventDefault(); dropzone.classList.add("is-over"); }));
  ["dragleave", "drop"].forEach((evento) => dropzone.addEventListener(evento, (e) => { e.preventDefault(); dropzone.classList.remove("is-over"); }));
  dropzone.addEventListener("drop", (e) => adicionarArquivos(e.dataTransfer.files));
  dropzone.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  $("#login-form").addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const botao = $("#entrar-admin");
    botao.disabled = true;
    mostrarMensagem("#login-feedback", "Entrando…", "loading");
    try {
      const resposta = await fetch("/admin/auth/login", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: $("#login-email").value.trim(), senha: $("#login-senha").value }),
      });
      const dados = await resposta.json().catch(() => ({}));
      if (!resposta.ok) throw new Error(dados.detail || "Não foi possível entrar.");
      mostrarDashboard(dados);
      await Promise.all([carregarDocumentos(), carregarPrompt()]);
    } catch (erro) {
      mostrarMensagem("#login-feedback", erro.message, "error");
    } finally {
      botao.disabled = false;
    }
  });
  $("#sair-admin").addEventListener("click", async () => {
    try {
      const resposta = await fetchAdmin("/admin/auth/logout", { method: "POST" });
      if (!resposta.ok) throw new Error("Não foi possível encerrar a sessão.");
      mostrarLogin();
      $("#login-feedback").className = "feedback";
    } catch (erro) {
      mostrarFeedback(erro.message, "error");
    }
  });
  $("#prompt-sistema").addEventListener("input", atualizarPromptBotao);
  $("#salvar-prompt").addEventListener("click", async () => {
    const botao = $("#salvar-prompt");
    botao.disabled = true;
    mostrarMensagem("#prompt-feedback", "Salvando prompt…", "loading");
    try {
      const resposta = await fetchAdmin("/admin/config", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt_sistema: $("#prompt-sistema").value }),
      });
      const dados = await resposta.json().catch(() => ({}));
      if (!resposta.ok) throw new Error(dados.detail || "Não foi possível salvar o prompt.");
      estado.promptOriginal = dados.prompt_sistema;
      mostrarMensagem("#prompt-feedback", "Prompt salvo. As próximas respostas já usarão estas instruções.", "success");
    } catch (erro) {
      mostrarMensagem("#prompt-feedback", erro.message, "error");
    } finally {
      atualizarPromptBotao();
    }
  });
  $("#lista-arquivos").addEventListener("click", (e) => {
    const botao = e.target.closest(".remove-file");
    if (!botao) return;
    estado.arquivos.splice(Number(botao.dataset.indice), 1);
    renderizarFila();
  });
  $("#limpar-fila").addEventListener("click", () => { estado.arquivos = []; renderizarFila(); });
  $("#atualizar-documentos").addEventListener("click", carregarDocumentos);
  $("#aplicar-backend").addEventListener("click", async () => {
    const backend = $("#vector-store").value;
    if (!window.confirm(`Trocar para ${backend === "qdrant" ? "Qdrant" : "o índice local"}? A base será reindexada.`)) return;
    const botao = $("#aplicar-backend");
    botao.disabled = true;
    mostrarFeedback("Alterando o backend e reindexando a base…", "loading");
    try {
      const resposta = await fetchAdmin("/admin/vector-store", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vector_store: backend }),
      });
      const corpo = await resposta.json().catch(() => ({}));
      if (!resposta.ok) throw new Error(corpo.detail || "Não foi possível alterar o backend.");
      atualizarBackend(corpo);
      mostrarFeedback(`Backend alterado para ${nomeBackend(corpo)} e base reindexada.`, "success");
      await carregarDocumentos();
    } catch (erro) {
      mostrarFeedback(erro.message, "error");
      await carregarDocumentos();
    } finally {
      botao.disabled = false;
    }
  });
  $("#upload-form").addEventListener("submit", async (evento) => {
    evento.preventDefault();
    const arquivos = [...estado.arquivos];
    const botao = $("#enviar-arquivos");
    botao.disabled = true;
    let concluidos = 0;
    try {
      for (let indice = 0; indice < arquivos.length; indice += 1) {
        mostrarFeedback(`Enviando ${indice + 1} de ${arquivos.length}…`, "loading");
        await enviarArquivo(arquivos[indice], indice, arquivos.length);
        concluidos += 1;
      }
      estado.arquivos = [];
      renderizarFila();
      mostrarFeedback(`${concluidos} arquivo(s) adicionado(s) e indexado(s) com sucesso.`, "success");
      await carregarDocumentos();
    } catch (erro) {
      mostrarFeedback(`${concluidos} arquivo(s) concluído(s). ${erro.message}`, "error");
    } finally {
      atualizarBotao();
    }
  });
  verificarSessao();
});
