"use strict";

const estado = { sessaoId: lerSessao(), nome: lerNome(), enviando: false };
const $ = (seletor) => document.querySelector(seletor);

// ---------------------------------------------------------------- sessão (histórico da conversa)
function lerSessao() {
  try { return sessionStorage.getItem("sessao_id"); } catch { return null; }
}
function salvarSessao(id) {
  try { sessionStorage.setItem("sessao_id", id); } catch { /* armazenamento indisponível */ }
}
function lerNome() {
  try { return (sessionStorage.getItem("nome") || "").trim(); } catch { return ""; }
}
function salvarNome(nome) {
  try { sessionStorage.setItem("nome", nome); } catch { /* armazenamento indisponível */ }
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
async function perguntarSemStream(corpo) {
  const resposta = await fetch("/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corpo),
  });
  if (!resposta.ok) throw new Error(`HTTP ${resposta.status}`);
  return resposta.json();
}

// Usa /chat/stream (uma linha JSON por evento) para mostrar o texto enquanto o modelo escreve.
// Sem suporte a stream no navegador, ou com servidor antigo sem a rota, usa /chat.
async function perguntarAoServidor(corpo, aoReceberParte) {
  if (!window.ReadableStream || !window.TextDecoder) return perguntarSemStream(corpo);
  const resposta = await fetch("/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(corpo),
  });
  if (resposta.status === 404 || resposta.status === 405) return perguntarSemStream(corpo);
  if (!resposta.ok || !resposta.body) throw new Error(`HTTP ${resposta.status}`);

  const leitor = resposta.body.getReader();
  const decodificador = new TextDecoder();
  let pendente = "";
  let final = null;
  const tratarLinha = (linha) => {
    if (!linha.trim()) return;
    const evento = JSON.parse(linha);
    if (evento.tipo === "parcial") aoReceberParte(evento.texto);
    else if (evento.tipo === "final") final = evento;
    else if (evento.tipo === "erro") throw new Error(evento.mensagem || "erro no servidor");
  };
  for (;;) {
    const { value, done } = await leitor.read();
    if (done) break;
    pendente += decodificador.decode(value, { stream: true });
    const linhas = pendente.split("\n");
    pendente = linhas.pop();
    linhas.forEach(tratarLinha);
  }
  tratarLinha(pendente + decodificador.decode());
  if (!final) throw new Error("resposta incompleta do servidor");
  return final;
}

async function enviarPergunta(texto) {
  const pergunta = String(texto || "").trim();
  if (!pergunta || estado.enviando || !estado.nome) return;

  estado.enviando = true;
  $("#enviar").disabled = true;
  adicionarMensagem("user", pergunta);
  $("#pergunta").value = "";
  const aguardando = adicionarMensagem("bot digitando", "Consultando os documentos oficiais…");
  let segundosEspera = 0;
  const progresso = window.setInterval(() => {
    segundosEspera += 5;
    const detalhe = segundosEspera >= 15
      ? " O primeiro uso pode levar até dois minutos enquanto o modelo é carregado."
      : "";
    const textoAguardando = aguardando.querySelector(".texto");
    if (textoAguardando) textoAguardando.textContent = `Consultando os documentos oficiais… ${segundosEspera}s.${detalhe}`;
  }, 5000);

  // Mensagem que recebe o texto enquanto o modelo escreve; criada no primeiro pedaço.
  let mensagem = null;
  let textoParcial = "";
  const aoReceberParte = (parte) => {
    if (!mensagem) {
      window.clearInterval(progresso);
      aguardando.remove();
      mensagem = adicionarMensagem("bot escrevendo", "");
    }
    textoParcial += parte;
    mensagem.querySelector(".texto").textContent = textoParcial;
    rolarParaFim();
  };

  try {
    const dados = await perguntarAoServidor(
      { nome: estado.nome, pergunta, sessao_id: estado.sessaoId }, aoReceberParte,
    );
    estado.sessaoId = dados.sessao_id;
    salvarSessao(dados.sessao_id);

    aguardando.remove();
    // A resposta final é a definitiva: pode corrigir o texto parcial (ex.: recusa padronizada).
    if (mensagem) {
      mensagem.classList.remove("escrevendo");
      const corpo = mensagem.querySelector(".texto");
      corpo.replaceChildren();
      preencherTexto(corpo, dados.resposta);
    } else {
      mensagem = adicionarMensagem("bot", dados.resposta);
    }
    if (dados.modo === "extrativo") {
      mensagem.appendChild(criar("div", "aviso", "Modelo de linguagem indisponível: exibindo trechos dos documentos."));
    }
    adicionarFontes(mensagem, dados.fontes);
    if (dados.modo !== "esclarecimento" && dados.modo !== "conversa") adicionarAvaliacao(mensagem, dados.id);
  } catch (erro) {
    aguardando.remove();
    if (mensagem) mensagem.remove();
    // fetch só rejeita com TypeError quando o servidor não pôde ser alcançado.
    adicionarMensagem("bot erro", erro instanceof TypeError
      ? "Não consegui conectar ao servidor. Verifique se ele está em execução e tente novamente."
      : "O servidor não conseguiu processar a pergunta. Recarregue a página e tente novamente.");
    console.error(erro);
  } finally {
    window.clearInterval(progresso);
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

  const tela = $("#identificacao");
  const liberarChat = () => {
    tela.hidden = true;
    $("#pergunta").focus();
  };
  if (estado.nome) {
    liberarChat();
  } else {
    tela.hidden = false;
    $("#nome").focus();
  }
  $("#form-nome").addEventListener("submit", (evento) => {
    evento.preventDefault();
    const nome = $("#nome").value.split(/\s+/).join(" ").trim();
    if (!nome) { $("#nome").focus(); return; }
    estado.nome = nome;
    salvarNome(nome);
    liberarChat();
  });

  carregarStatus();
});
