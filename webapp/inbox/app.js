const STORAGE_KEY = "neegy_inbox_token";
const STORAGE_NAME = "neegy_inbox_name";

let authToken = localStorage.getItem(STORAGE_KEY) || "";
let agentName = localStorage.getItem(STORAGE_NAME) || "";
let activeConversationId = null;
let lastMessageId = 0;
let pollTimer = null;
let conversationsCache = [];
let searchQuery = "";
let lastRenderedDate = null;
let lastRenderedDirection = null;

const AVATAR_COLORS = [
  "#e17076", "#7bc862", "#65aadd", "#a695e7", "#ee7aae",
  "#6ec9cb", "#faa774", "#5c6bc0", "#f0a04b", "#4db6ac",
];

const $ = (id) => document.getElementById(id);

const loginScreen = $("login-screen");
const appScreen = $("app-screen");
const loginError = $("login-error");
const conversationList = $("conversation-list");
const convEmpty = $("conv-empty");
const messagesEl = $("messages");
const chatEmpty = $("chat-empty");
const chatActive = $("chat-active");
const chatTitle = $("chat-title");
const chatSubtitle = $("chat-subtitle");
const chatAvatar = $("chat-avatar");
const cannedBar = $("canned-bar");
const replyForm = $("reply-form");
const replyInput = $("reply-input");
const searchInput = $("search-input");

/* ---------- Helpers ---------- */

function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  return fetch(path, { ...options, headers });
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function initials(name) {
  const parts = String(name || "?").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  if (parts.length === 1) return parts[0].slice(0, 2);
  return parts[0][0] + parts[1][0];
}

function colorFor(key) {
  let hash = 0;
  const str = String(key || "");
  for (let i = 0; i < str.length; i++) hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

function setAvatar(el, name, key) {
  el.textContent = initials(name);
  el.style.background = colorFor(key ?? name);
}

function parseDate(value) {
  if (!value) return null;
  // Les timestamps arrivent sans timezone ("2026-08-30 20:41:00") → UTC.
  let v = String(value);
  if (v.includes(" ") && !v.includes("T")) v = v.replace(" ", "T");
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(v)) v += "Z";
  const d = new Date(v);
  return isNaN(d.getTime()) ? null : d;
}

function fmtTime(value) {
  const d = parseDate(value);
  if (!d) return "";
  return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
}

function fmtConvTime(value) {
  const d = parseDate(value);
  if (!d) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return "Hier";
  const days = (now - d) / 86400000;
  if (days < 7) return d.toLocaleDateString("fr-FR", { weekday: "short" });
  return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" });
}

function fmtDateSep(value) {
  const d = parseDate(value);
  if (!d) return "";
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return "Aujourd'hui";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (d.toDateString() === yesterday.toDateString()) return "Hier";
  return d.toLocaleDateString("fr-FR", { day: "numeric", month: "long", year: "numeric" });
}

function dayKey(value) {
  const d = parseDate(value);
  return d ? d.toDateString() : null;
}

/* ---------- Screens ---------- */

function showLogin() {
  stopPolling();
  appScreen.classList.add("hidden");
  loginScreen.classList.remove("hidden");
}

function showApp() {
  loginScreen.classList.add("hidden");
  appScreen.classList.remove("hidden");
  $("agent-name-label").textContent = agentName || "NEEGY";
  setAvatar($("agent-avatar"), agentName || "N", agentName || "N");
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    await refreshConversations();
    if (activeConversationId) await refreshMessages(false);
  }, 3000);
}

/* ---------- Auth ---------- */

async function login(name, token) {
  const res = await api("/api/inbox/login", {
    method: "POST",
    body: JSON.stringify({ name, token }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Connexion impossible");
  authToken = data.token;
  agentName = data.agent.name;
  localStorage.setItem(STORAGE_KEY, authToken);
  localStorage.setItem(STORAGE_NAME, agentName);
}

/* ---------- Conversations ---------- */

async function refreshConversations() {
  const res = await api("/api/inbox/conversations");
  if (res.status === 401) return showLogin();
  const data = await res.json();
  conversationsCache = data.conversations || [];
  renderConversations();
}

function renderConversations() {
  const q = searchQuery.trim().toLowerCase();
  const items = conversationsCache.filter((c) => {
    if (!q) return true;
    const hay = `${c.client_name || ""} ${c.client_username || ""} ${c.last_preview || ""}`.toLowerCase();
    return hay.includes(q);
  });

  conversationList.innerHTML = "";
  convEmpty.classList.toggle("hidden", items.length > 0);

  for (const conv of items) {
    const li = document.createElement("li");
    li.className = "conv-item" + (conv.id === activeConversationId ? " active" : "");
    li.dataset.id = conv.id;

    const av = document.createElement("span");
    av.className = "avatar";
    setAvatar(av, conv.client_name, conv.telegram_user_id);

    const body = document.createElement("div");
    body.className = "conv-body";
    const username = conv.client_username ? `@${conv.client_username}` : `#${conv.telegram_user_id}`;
    body.innerHTML = `
      <div class="conv-top">
        <span class="conv-name">${escapeHtml(conv.client_name || username)}</span>
        <span class="conv-time">${escapeHtml(fmtConvTime(conv.last_message_at))}</span>
      </div>
      <div class="conv-preview">${escapeHtml(conv.last_preview || "")}</div>
    `;

    li.appendChild(av);
    li.appendChild(body);
    li.addEventListener("click", () => selectConversation(conv));
    conversationList.appendChild(li);
  }
}

async function selectConversation(conv) {
  activeConversationId = conv.id;
  lastMessageId = 0;
  lastRenderedDate = null;
  lastRenderedDirection = null;
  chatEmpty.classList.add("hidden");
  chatActive.classList.remove("hidden");
  appScreen.classList.add("chat-open");

  const username = conv.client_username ? `@${conv.client_username}` : `#${conv.telegram_user_id}`;
  chatTitle.textContent = conv.client_name || username;
  chatSubtitle.textContent = username;
  setAvatar(chatAvatar, conv.client_name, conv.telegram_user_id);

  messagesEl.innerHTML = "";
  renderConversations();
  await refreshMessages(true);
  replyInput.focus();
}

/* ---------- Messages ---------- */

async function refreshMessages(scroll) {
  if (!activeConversationId) return;
  const res = await api(
    `/api/inbox/conversations/${activeConversationId}/messages?since_id=${lastMessageId}`
  );
  if (!res.ok) return;
  const data = await res.json();
  const msgs = data.messages || [];
  if (!msgs.length) return;

  const nearBottom =
    messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 120;

  for (const msg of msgs) {
    appendMessage(msg);
    lastMessageId = Math.max(lastMessageId, msg.id);
  }
  if (scroll || nearBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendMessage(msg) {
  const key = dayKey(msg.created_at);
  if (key && key !== lastRenderedDate) {
    const sep = document.createElement("div");
    sep.className = "date-sep";
    sep.textContent = fmtDateSep(msg.created_at);
    messagesEl.appendChild(sep);
    lastRenderedDate = key;
    lastRenderedDirection = null;
  }

  const wrap = document.createElement("div");
  const grouped = lastRenderedDirection === msg.direction;
  wrap.className =
    `msg ${msg.direction}` + (grouped ? ` same-${msg.direction}` : "");

  if (msg.direction === "out" && msg.agent_name) {
    const agent = document.createElement("span");
    agent.className = "msg-agent";
    agent.textContent = msg.agent_name;
    wrap.appendChild(agent);
  }

  const text = document.createElement("span");
  text.className = "msg-text";
  text.textContent = msg.content;
  wrap.appendChild(text);

  const time = document.createElement("span");
  time.className = "msg-time";
  time.textContent = fmtTime(msg.created_at);
  wrap.appendChild(time);

  messagesEl.appendChild(wrap);
  lastRenderedDirection = msg.direction;
}

/* ---------- Canned ---------- */

async function loadCanned() {
  const res = await api("/api/inbox/canned");
  if (!res.ok) return;
  const data = await res.json();
  cannedBar.innerHTML = "";
  for (const item of data.items || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = `/${item.shortcut}`;
    btn.title = item.content;
    btn.addEventListener("click", () => {
      replyInput.value = item.content;
      autoGrow();
      replyInput.focus();
    });
    cannedBar.appendChild(btn);
  }
}

/* ---------- Reply input ---------- */

function autoGrow() {
  replyInput.style.height = "auto";
  replyInput.style.height = Math.min(replyInput.scrollHeight, 140) + "px";
}

async function sendReply() {
  if (!activeConversationId) return;
  const content = replyInput.value.trim();
  if (!content) return;
  const res = await api(`/api/inbox/conversations/${activeConversationId}/reply`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    alert(data.error || "Envoi impossible");
    return;
  }
  replyInput.value = "";
  autoGrow();
  await refreshMessages(true);
  await refreshConversations();
}

/* ---------- Events ---------- */

$("login-btn").addEventListener("click", async () => {
  loginError.classList.add("hidden");
  const name = $("login-name").value.trim();
  const token = $("login-token").value.trim();
  try {
    await login(name, token);
    showApp();
    await loadCanned();
    await refreshConversations();
    startPolling();
  } catch (err) {
    loginError.textContent = err.message;
    loginError.classList.remove("hidden");
  }
});

$("login-token").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("login-btn").click();
});

$("logout-btn").addEventListener("click", () => {
  authToken = "";
  agentName = "";
  activeConversationId = null;
  localStorage.removeItem(STORAGE_KEY);
  localStorage.removeItem(STORAGE_NAME);
  showLogin();
});

$("back-btn").addEventListener("click", () => {
  appScreen.classList.remove("chat-open");
  activeConversationId = null;
  chatActive.classList.add("hidden");
  chatEmpty.classList.remove("hidden");
  renderConversations();
});

searchInput.addEventListener("input", () => {
  searchQuery = searchInput.value;
  renderConversations();
});

replyForm.addEventListener("submit", (e) => {
  e.preventDefault();
  sendReply();
});

replyInput.addEventListener("input", autoGrow);
replyInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendReply();
  }
});

/* ---------- Bootstrap ---------- */

(async function bootstrap() {
  if (authToken && agentName) {
    showApp();
    await loadCanned();
    await refreshConversations();
    startPolling();
  } else {
    showLogin();
  }
})();
