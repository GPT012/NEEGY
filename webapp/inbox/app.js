const STORAGE_KEY = "neegy_inbox_token";
const STORAGE_NAME = "neegy_inbox_name";

let authToken = localStorage.getItem(STORAGE_KEY) || "";
let agentName = localStorage.getItem(STORAGE_NAME) || "";
let activeConversationId = null;
let lastMessageId = 0;
let pollTimer = null;

const loginScreen = document.getElementById("login-screen");
const appScreen = document.getElementById("app-screen");
const loginError = document.getElementById("login-error");
const conversationList = document.getElementById("conversation-list");
const messagesEl = document.getElementById("messages");
const chatEmpty = document.getElementById("chat-empty");
const chatActive = document.getElementById("chat-active");
const chatTitle = document.getElementById("chat-title");
const chatSubtitle = document.getElementById("chat-subtitle");
const cannedBar = document.getElementById("canned-bar");
const replyForm = document.getElementById("reply-form");
const replyInput = document.getElementById("reply-input");

function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (authToken) headers.Authorization = `Bearer ${authToken}`;
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  return fetch(path, { ...options, headers });
}

function showLogin() {
  stopPolling();
  appScreen.classList.add("hidden");
  loginScreen.classList.remove("hidden");
}

function showApp() {
  loginScreen.classList.add("hidden");
  appScreen.classList.remove("hidden");
  document.getElementById("agent-label").textContent = agentName ? `— ${agentName}` : "";
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

async function refreshConversations() {
  const res = await api("/api/inbox/conversations");
  if (res.status === 401) {
    showLogin();
    return;
  }
  const data = await res.json();
  conversationList.innerHTML = "";
  for (const conv of data.conversations || []) {
    const li = document.createElement("li");
    if (conv.id === activeConversationId) li.classList.add("active");
    li.dataset.id = conv.id;
    const username = conv.client_username ? `@${conv.client_username}` : `#${conv.telegram_user_id}`;
    li.innerHTML = `
      <div class="conv-name">${escapeHtml(conv.client_name)}</div>
      <div class="muted">${escapeHtml(username)}</div>
      <div class="conv-preview">${escapeHtml(conv.last_preview || "")}</div>
    `;
    li.addEventListener("click", () => selectConversation(conv.id, conv.client_name, username));
    conversationList.appendChild(li);
  }
}

async function loadCanned() {
  const res = await api("/api/inbox/canned");
  if (!res.ok) return;
  const data = await res.json();
  cannedBar.innerHTML = "";
  for (const item of data.items || []) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = `/${item.shortcut}`;
    btn.addEventListener("click", () => {
      replyInput.value = item.content;
      replyInput.focus();
    });
    cannedBar.appendChild(btn);
  }
}

async function selectConversation(id, name, username) {
  activeConversationId = id;
  lastMessageId = 0;
  chatEmpty.classList.add("hidden");
  chatActive.classList.remove("hidden");
  chatTitle.textContent = name;
  chatSubtitle.textContent = username;
  messagesEl.innerHTML = "";
  await refreshConversations();
  await refreshMessages(true);
}

async function refreshMessages(scroll) {
  if (!activeConversationId) return;
  const res = await api(
    `/api/inbox/conversations/${activeConversationId}/messages?since_id=${lastMessageId}`
  );
  if (!res.ok) return;
  const data = await res.json();
  for (const msg of data.messages || []) {
    appendMessage(msg);
    lastMessageId = Math.max(lastMessageId, msg.id);
  }
  if (scroll) messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendMessage(msg) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${msg.direction}`;
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.textContent =
    msg.direction === "out"
      ? `Toi${msg.agent_name ? ` (${msg.agent_name})` : ""}`
      : "Cliente";
  const body = document.createElement("div");
  body.textContent = msg.content;
  wrap.appendChild(meta);
  wrap.appendChild(body);
  messagesEl.appendChild(wrap);
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

document.getElementById("login-btn").addEventListener("click", async () => {
  loginError.classList.add("hidden");
  const name = document.getElementById("login-name").value.trim();
  const token = document.getElementById("login-token").value.trim();
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

document.getElementById("logout-btn").addEventListener("click", () => {
  authToken = "";
  agentName = "";
  localStorage.removeItem(STORAGE_KEY);
  localStorage.removeItem(STORAGE_NAME);
  showLogin();
});

replyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!activeConversationId) return;
  const content = replyInput.value.trim();
  if (!content) return;
  const res = await api(`/api/inbox/conversations/${activeConversationId}/reply`, {
    method: "POST",
    body: JSON.stringify({ content }),
  });
  const data = await res.json();
  if (!res.ok) {
    alert(data.error || "Envoi impossible");
    return;
  }
  replyInput.value = "";
  await refreshMessages(true);
  await refreshConversations();
});

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
