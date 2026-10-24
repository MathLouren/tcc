"use strict";

const estado = { sessaoId: lerSessao(), enviando: false };
const $ = (seletor) => document.querySelector(seletor);

// ---------------------------------------------------------------- sessão (histórico da conversa)
function lerSessao() {
  try { return sessionStorage.getItem("sessao_id"); } catch { return null; }
}
function salvarSessao(id) {
  try { sessionStorage.setItem("sessao_id", id); } catch { /* armazenamento indisponível */ }
}
function limparSessao() {
  estado.sessaoId = null;
  try { sessionStorage.removeItem("sessao_id"); } catch { /* ignora */ }
}

// ---------------------------------------------------------------- utilitários de DOM
function criar(tag, classe, texto) {
  const elemento = document.createElement(tag);
  if (classe) elemento.className = classe;
  if (texto !== undefined) elemento.textContent = texto;
  return elemento;
}

function urlSegura(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

// Insere o texto preservando quebras de linha e transformando URLs em links (sem usar innerHTML).
function preencherTexto(destino, texto) {
  const partes = String(texto).split(/(https?:\/\/[^\s)]+)/g);
  partes.forEach((parte) => {
    if (urlSegura(parte)) {
      const link = criar("a", null, parte);
      link.href = parte;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      destino.appendChild(link);
    } else if (parte) {
      destino.appendChild(document.createTextNode(parte));
    }
  });
}

function rolarParaFim() {
  const area = $("#mensagens");
  area.scrollTop = area.scrollHeight;
}

function adicionarMensagem(tipo, texto) {
  const mensagem = criar("div", `msg ${tipo}`);
  if (tipo.startsWith("bot")) mensagem.appendChild(criar("div", "quem", "Assistente UNIVERSO"));
  const corpo = criar("div", "texto");
  preencherTexto(corpo, texto);
  mensagem.appendChild(corpo);
  $("#mensagens").appendChild(mensagem);
  rolarParaFim();
  return mensagem;
}

function formatarSimilaridade(valor) {
  return Number(valor).toFixed(2).replace(".", ",");
}

function formatarData(iso) {
  if (!iso) return "";
  const [ano, mes, dia] = String(iso).slice(0, 10).split("-");
  return dia && mes && ano ? `${dia}/${mes}/${ano}` : String(iso);
}

// ---------------------------------------------------------------- fontes e avaliação (RF04 e RF07)
function adicionarFontes(mensagem, fontes) {
  if (!Array.isArray(fontes) || fontes.length === 0) return;
  const bloco = criar("div", "fontes");
  bloco.appendChild(criar("strong", null, "Fontes consultadas"));
  const lista = criar("ul");
  fontes.forEach((fonte) => {
    const item = criar("li");
    const titulo = `${fonte.documento} – ${fonte.secao}`;
    if (urlSegura(fonte.url)) {
      const link = criar("a", null, titulo);
      link.href = fonte.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      item.appendChild(link);
    } else {
      item.appendChild(document.createTextNode(titulo));
    }
    item.appendChild(document.createTextNode(` (similaridade ${formatarSimilaridade(fonte.similaridade)})`));
    lista.appendChild(item);
  });
  bloco.appendChild(lista);
  mensagem.appendChild(bloco);
}

function adicionarAvaliacao(mensagem, interacaoId) {
  const caixa = criar("div", "avaliacao");
  caixa.appendChild(criar("span", null, "Esta resposta foi útil?"));
  const sim = criar("button", null, "👍 Sim");
  const nao = criar("button", null, "👎 Não");
  sim.type = "button";
  nao.type = "button";

  const enviarAvaliacao = async (util) => {
    sim.disabled = true;
    nao.disabled = true;
    try {
      const resposta = await fetch("/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ interacao_id: interacaoId, util }),
      });
      caixa.appendChild(criar("span", null, resposta.ok ? "Obrigado pela avaliação!" : "Não foi possível registrar."));
    } catch {
      caixa.appendChild(criar("span", null, "Não foi possível registrar."));
    }
  };
  sim.addEventListener("click", () => enviarAvaliacao(true));
  nao.addEventListener("click", () => enviarAvaliacao(false));
  caixa.append(sim, nao);
  mensagem.appendChild(caixa);
}

// ---------------------------------------------------------------- envio da pergunta (RF01)
async function enviarPergunta(texto) {
  const pergunta = String(texto || "").trim();
  if (!pergunta || estado.enviando) return;

  estado.enviando = true;
  $("#enviar").disabled = true;
  adicionarMensagem("user", pergunta);
  $("#pergunta").value = "";
  const aguardando = adicionarMensagem("bot digitando", "Consultando os documentos oficiais…");

  try {
    const resposta = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pergunta, sessao_id: estado.sessaoId }),
    });
    if (!resposta.ok) throw new Error(`HTTP ${resposta.status}`);
    const dados = await resposta.json();
    estado.sessaoId = dados.sessao_id;
    salvarSessao(dados.sessao_id);

    aguardando.remove();
    const mensagem = adicionarMensagem("bot", dados.resposta);
    if (dados.modo === "extrativo") {
      mensagem.appendChild(criar("div", "aviso", "Modelo de linguagem indisponível: exibindo trechos dos documentos."));
    }
    adicionarFontes(mensagem, dados.fontes);
    adicionarAvaliacao(mensagem, dados.id);
  } catch (erro) {
    aguardando.remove();
    adicionarMensagem("bot erro", "Não foi possível obter a resposta agora. Tente novamente em instantes.");
    console.error(erro);
  } finally {
    estado.enviando = false;
    $("#enviar").disabled = false;
    $("#pergunta").focus();
    rolarParaFim();
  }
}

// ---------------------------------------------------------------- estado do servidor e documentos
async function carregarStatus() {
  const status = $("#status");
  const lista = $("#documentos");
  try {
    const resposta = await fetch("/health");
    const dados = await resposta.json();
    const data = formatarData(dados.ultima_indexacao);
    status.textContent = data ? `Base atualizada: ${data}` : "Base não indexada";
    status.classList.toggle("alerta", !dados.llm.disponivel);
    status.title = dados.llm.disponivel
      ? `Modelo de linguagem: ${dados.llm.modelo}`
      : `Modelo ${dados.llm.modelo} indisponível: respostas em modo extrativo`;

    lista.replaceChildren();
    (dados.documentos || []).forEach((doc) => {
      const item = criar("li", null, doc.titulo);
      if (doc.atualizado_em) {
        item.appendChild(document.createTextNode(" "));
        item.appendChild(criar("small", null, `(${formatarData(doc.atualizado_em)})`));
      }
      lista.appendChild(item);
    });
    if (!lista.children.length) lista.appendChild(criar("li", null, "Nenhum documento indexado."));
  } catch {
    status.textContent = "Servidor indisponível";
    status.classList.add("erro");
    lista.replaceChildren(criar("li", null, "Não foi possível carregar."));
  }
}

// ---------------------------------------------------------------- eventos
document.addEventListener("DOMContentLoaded", () => {
  $("#formulario").addEventListener("submit", (evento) => {
    evento.preventDefault();
    enviarPergunta($("#pergunta").value);
  });

  document.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", () => enviarPergunta(chip.textContent));
  });

  $("#nova-conversa").addEventListener("click", () => {
    limparSessao();
    const area = $("#mensagens");
    while (area.children.length > 1) area.lastChild.remove();
    $("#pergunta").focus();
  });

  carregarStatus();
  $("#pergunta").focus();
});
