"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  me: null, config: null, files: [], first: "A", source: null, current: null,
  status: null, agents: {}, turns: {}, open: null, history: [],
};

const STATUS_LABEL = {
  running: "Working", agreed: "Agreed", no_consensus: "No agreement", stopped: "Stopped",
  budget: "Spending cap reached", error: "Error", interrupted: "Interrupted",
};
const OPTS_KEY = "fc-options";

// ---------- helpers ----------

function md(text) { return DOMPurify.sanitize(marked.parse(text || "")); }
function esc(s) { const d = document.createElement("div"); d.textContent = s == null ? "" : String(s); return d.innerHTML; }
function money(x) { x = x || 0; return "$" + x.toFixed(x < 1 ? 3 : 2); }
// The verdict is shown as a badge, so drop the raw "VERDICT: …" line.
function stripVerdict(t) { return (t || "").replace(/^\s*\**\s*VERDICT:.*$/gim, "").trimEnd(); }
function shortModel(id) { return (state.config && state.config.models[id] || id).replace("Claude ", ""); }

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 401 && !path.startsWith("/api/login") && !path.startsWith("/api/me")) {
    showLogin();
    throw new Error("Please sign in.");
  }
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw Object.assign(new Error((data && data.detail) || res.statusText), { status: res.status, data });
  return data;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

// ---------- start / sign in ----------

async function boot() {
  wire();
  try {
    state.me = await api("/api/me");
  } catch (e) {
    return showLogin(e.data && e.data.needs_setup);
  }
  await enterApp();
}

function showLogin(setup) {
  closeStream();
  $("app").hidden = true;
  $("login-view").hidden = false;
  state.setup = !!setup;
  $("login-title").textContent = setup ? "Create the admin account" : "Sign in";
  $("login-sub").textContent = setup ? "No accounts yet. This first one can add the others." : "Fact Check Chat";
  $("login-btn").textContent = setup ? "Create account" : "Sign in";
  $("login-pass").autocomplete = setup ? "new-password" : "current-password";
  $("login-user").focus();
}

async function onLogin(e) {
  e.preventDefault();
  $("login-error").textContent = "";
  $("login-btn").disabled = true;
  try {
    const body = { username: $("login-user").value.trim(), password: $("login-pass").value };
    state.me = await post(state.setup ? "/api/setup" : "/api/login", body);
    $("login-pass").value = "";
    await enterApp();
  } catch (err) {
    $("login-error").textContent = err.message;
  } finally {
    $("login-btn").disabled = false;
  }
}

async function enterApp() {
  $("login-view").hidden = true;
  $("app").hidden = false;
  $("user-name").textContent = state.me.username;
  $("user-initial").textContent = state.me.username.slice(0, 1).toUpperCase();
  for (const el of document.querySelectorAll(".admin-only")) el.hidden = !state.me.is_admin;
  state.config = await api("/api/config");
  fillOptions();
  await loadHistory();
  route();
}

// ---------- wiring ----------

function wire() {
  $("login-form").addEventListener("submit", onLogin);
  $("new-btn").addEventListener("click", () => { location.hash = ""; route(); });
  $("menu-btn").addEventListener("click", () => toggleSidebar(true));
  $("scrim").addEventListener("click", () => toggleSidebar(false));
  $("user-btn").addEventListener("click", (e) => { e.stopPropagation(); $("user-menu").hidden = !$("user-menu").hidden; });
  document.addEventListener("click", () => { $("user-menu").hidden = true; });
  for (const b of $("user-menu").querySelectorAll("[data-go]")) {
    b.addEventListener("click", () => { location.hash = b.dataset.go; });
  }
  $("logout-btn").addEventListener("click", async () => { await post("/api/logout"); location.hash = ""; showLogin(false); });
  $("opt-btn").addEventListener("click", () => { $("options").hidden = !$("options").hidden; });
  $("first").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.first = b.dataset.v;
    for (const x of $("first").children) x.classList.toggle("on", x === b);
    saveOptions();
  });
  for (const id of ["model_a", "model_b", "max_turns", "effort", "budget"]) $(id).addEventListener("change", saveOptions);
  $("file-input").addEventListener("change", onFiles);
  $("composer").addEventListener("submit", onSend);
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); }
  });
  $("input").addEventListener("input", autosize);
  $("stop-btn").addEventListener("click", onStop);
  $("key-form").addEventListener("submit", onSaveKey);
  $("key-remove").addEventListener("click", onRemoveKey);
  $("add-user-form").addEventListener("submit", onAddUser);
  $("pw-form").addEventListener("submit", onChangePassword);
  window.addEventListener("hashchange", route);
}

function toggleSidebar(open) {
  $("sidebar").classList.toggle("open", open);
  $("scrim").hidden = !open;
}

function autosize() {
  const t = $("input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 220) + "px";
}

// ---------- options ----------

function fillOptions() {
  const c = state.config;
  for (const sel of ["model_a", "model_b"]) {
    $(sel).innerHTML = Object.entries(c.models).map(([id, label]) => `<option value="${id}">${esc(label)}</option>`).join("");
  }
  $("effort").innerHTML = c.efforts.map((e) => `<option>${e}</option>`).join("");
  $("max_turns").max = c.max_turns_limit;
  $("budget").max = c.max_budget_limit;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(OPTS_KEY) || "{}"); } catch (_) {}
  const o = { ...c.defaults, ...saved };
  $("model_a").value = c.models[o.model_a] ? o.model_a : c.defaults.model_a;
  $("model_b").value = c.models[o.model_b] ? o.model_b : c.defaults.model_b;
  $("effort").value = o.effort;
  $("max_turns").value = o.max_turns;
  $("budget").value = o.budget_usd;
  state.first = o.first === "B" ? "B" : "A";
  for (const x of $("first").children) x.classList.toggle("on", x.dataset.v === state.first);
  optionsChip();
}

function readOptions() {
  return {
    model_a: $("model_a").value, model_b: $("model_b").value, first: state.first,
    max_turns: parseInt($("max_turns").value, 10) || state.config.defaults.max_turns,
    effort: $("effort").value,
    budget_usd: parseFloat($("budget").value) || state.config.defaults.budget_usd,
  };
}

function saveOptions() {
  try { localStorage.setItem(OPTS_KEY, JSON.stringify(readOptions())); } catch (_) {}
  optionsChip();
}

function optionsChip() {
  const o = readOptions();
  $("opt-btn").textContent = `⚙ ${shortModel(o.model_a)} ↔ ${shortModel(o.model_b)} · ${o.first} first`;
}

// ---------- routing ----------

function route() {
  toggleSidebar(false);
  $("user-menu").hidden = true;
  const h = location.hash.slice(1);
  if (h === "settings" && state.me.is_admin) return showPage("settings-view", "API key", loadKeyStatus);
  if (h === "users" && state.me.is_admin) return showPage("users-view", "Users", loadUsers);
  if (h === "account") return showPage("account-view", "Change password");
  if (h) return openChat(h);
  newChat();
}

function showPage(id, title, load) {
  closeStream();
  state.current = null;
  markActive();
  for (const v of ["chat-view", "settings-view", "users-view", "account-view"]) $(v).hidden = v !== id;
  $("composer-wrap").hidden = id !== "chat-view";
  setTop(title);
  if (load) load();
}

function setTop(title, status, cost) {
  $("top-title").textContent = title || "";
  $("d-status").hidden = !status;
  if (status) { $("d-status").className = "badge " + status; $("d-status").textContent = STATUS_LABEL[status] || status; }
  $("d-cost").textContent = cost == null ? "" : money(cost);
}

// ---------- history ----------

async function loadHistory() {
  state.history = await api("/api/debates");
  $("history").innerHTML = state.history.map((d) => `
    <li data-id="${d.id}" title="${esc(d.question)}"><span class="dot ${d.status}"></span><span class="h-q">${esc(d.question)}</span></li>`).join("")
    || '<li class="muted">No chats yet</li>';
  for (const li of $("history").querySelectorAll("li[data-id]")) {
    li.addEventListener("click", () => { location.hash = li.dataset.id; });
  }
  markActive();
}
function markActive() {
  for (const li of $("history").querySelectorAll("li[data-id]")) li.classList.toggle("active", li.dataset.id === state.current);
}

// ---------- new chat / composer ----------

async function newChat() {
  showPage("chat-view", "New chat");
  $("empty").hidden = false;
  $("messages").innerHTML = "";
  $("final").innerHTML = "";
  setComposer(null);
  $("input").focus();
  try {
    const s = await api("/api/settings");
    $("no-key").hidden = s.has_key;
    $("no-key").innerHTML = state.me.is_admin
      ? 'Add a Claude API key under <a href="#settings">your name → API key</a> first.'
      : "An admin needs to add a Claude API key before you can ask questions.";
  } catch (_) {}
}

function setComposer(status) {
  state.status = status;
  const running = status === "running";
  $("stop-btn").hidden = !running;
  $("opt-btn").hidden = running;
  $("file-input").parentElement.hidden = running;
  if (running) $("options").hidden = true;
  $("input").placeholder = running ? "Message both AIs (they'll see it on their next turn)…"
    : state.current ? "Ask a follow-up (they remember this chat)…" : "Ask a question…";
}

async function onSend(e) {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  $("composer-error").textContent = "";
  $("send-btn").disabled = true;
  try {
    if (state.status === "running" && state.current) {
      await post(`/api/debates/${state.current}/note`, { text });
    } else if (state.current) {
      // A follow-up in this chat: keep what's on screen and stream the new round.
      const id = state.current;
      await post(`/api/debates/${id}/followup`, { question: text, ...readOptions(), files: state.files });
      state.files = [];
      renderFiles();
      $("options").hidden = true;
      setTop($("top-title").textContent, "running", null);
      setComposer("running");
      listen(id, state.lastSeq);
      loadHistory();
    } else {
      const { id } = await post("/api/debates", { question: text, ...readOptions(), files: state.files });
      state.files = [];
      renderFiles();
      $("options").hidden = true;
      location.hash = id;
      loadHistory();
    }
    $("input").value = "";
    autosize();
  } catch (err) {
    $("composer-error").textContent = err.message;
  } finally {
    $("send-btn").disabled = false;
  }
}

async function onFiles(e) {
  for (const f of e.target.files) {
    const ref = { filename: f.name, uploading: true };
    state.files.push(ref);
    renderFiles();
    const fd = new FormData();
    fd.append("file", f);
    try {
      Object.assign(ref, await api("/api/upload", { method: "POST", body: fd }), { uploading: false });
    } catch (err) {
      state.files = state.files.filter((x) => x !== ref);
      $("composer-error").textContent = `${f.name}: ${err.message}`;
    }
    renderFiles();
  }
  e.target.value = "";
}

function renderFiles() {
  $("file-list").innerHTML = "";
  for (const ref of state.files) {
    const li = document.createElement("li");
    li.innerHTML = `📎 ${esc(ref.filename)}${ref.uploading ? " (uploading…)" : ""} <button type="button" title="Remove">×</button>`;
    li.querySelector("button").onclick = () => { state.files = state.files.filter((x) => x !== ref); renderFiles(); };
    $("file-list").appendChild(li);
  }
  $("send-btn").disabled = state.files.some((f) => f.uploading);
}

async function onStop() {
  if (!state.current) return;
  $("stop-btn").disabled = true;
  try { await post(`/api/debates/${state.current}/stop`); } catch (err) { $("composer-error").textContent = err.message; }
  $("stop-btn").disabled = false;
}

// ---------- a chat ----------

function closeStream() { if (state.source) { state.source.close(); state.source = null; } }

async function openChat(id) {
  showPage("chat-view");
  state.current = id;
  state.turns = {};
  state.open = null;
  markActive();
  $("empty").hidden = true;
  $("messages").innerHTML = "";
  $("final").innerHTML = "";
  let d;
  try { d = await api(`/api/debates/${id}`); } catch (_) {
    setTop("Not found");
    return;
  }
  setTop(d.question, d.status, d.cost_usd);
  setComposer(d.status);
  state.lastSeq = -1;
  listen(id, -1);
}

function listen(id, after) {
  closeStream();
  const src = new EventSource(`/api/debates/${id}/events?after=${after}`);
  state.source = src;
  src.onmessage = (m) => {
    if (state.current !== id) return;
    const ev = JSON.parse(m.data);
    if (typeof ev.seq === "number") {
      if (ev.seq <= state.lastSeq) return; // already shown
      state.lastSeq = ev.seq;
    }
    handle(ev);
    // A chat with follow-ups has several "end" events; the server says when it's done.
    if (ev.type === "replay_done") { src.close(); loadHistory(); }
  };
}

function nearBottom() {
  const c = $("chat-view");
  return c.scrollHeight - c.scrollTop - c.clientHeight < 250;
}
function scrollDown(force) {
  const c = $("chat-view");
  if (force || nearBottom()) c.scrollTop = c.scrollHeight;
}

function addRow(html) {
  const stick = nearBottom();
  $("messages").insertAdjacentHTML("beforeend", html);
  scrollDown(stick);
}

function handle(ev) {
  switch (ev.type) {
    case "start": case "followup": {
      state.agents = ev.agents;
      const files = (ev.files || []).map((f) => f.filename).join(", ");
      addRow(`<div class="row-me"><div class="bubble-me">${esc(ev.question)}${files ? `<div class="files-note">📎 ${esc(files)}</div>` : ""}</div></div>`);
      const a = ev.agents;
      addRow(`<div class="sys">${ev.type === "followup" ? "Follow-up · " : ""}Claude A is ${esc(a.A.label)} · Claude B is ${esc(a.B.label)} · ${esc(a[ev.order[0]].name)} answers first</div>`);
      break;
    }
    case "turn_start": startTurn(ev); break;
    case "delta": case "thinking": case "tool_call": case "tool_result": case "file": case "status": {
      const t = state.open && state.open.agent === ev.agent ? state.open : null;
      if (t) addStep(t, ev.type === "delta" || ev.type === "thinking" ? { kind: ev.type === "delta" ? "text" : "thinking", text: ev.text } : { ...ev, kind: ev.type });
      else if (ev.type === "status") addRow(`<div class="sys">${esc(ev.text)}</div>`);
      break;
    }
    case "turn_end": endTurn(ev); break;
    case "user_note":
      addRow(`<div class="row-me"><div class="bubble-me note">${esc(ev.text)}</div></div>`);
      break;
    case "end": {
      setTop($("top-title").textContent, ev.status, ev.total_cost);
      setComposer(ev.status);
      const good = ev.status === "agreed";
      addRow(`
        <div class="final-card ${good ? "" : "warn"}">
          <h3>${good ? "✓ They agree" : esc(STATUS_LABEL[ev.status] || ev.status)}</h3>
          <p class="small muted">${esc(ev.message)}</p>
          ${ev.final_answer ? `<div class="step-text">${md(ev.final_answer)}</div>` : ""}
        </div>`);
      break;
    }
  }
}

function startTurn(ev) {
  const key = `${ev.agent}-${ev.turn}`;
  if (state.turns[key]) return;
  const label = state.agents[ev.agent] ? state.agents[ev.agent].label : ev.model;
  const el = document.createElement("div");
  el.className = `turn ${ev.agent} typing`;
  el.innerHTML = `
    <div class="avatar ${ev.agent}">${ev.agent}</div>
    <div class="turn-main">
      <div class="turn-head"><strong class="${ev.agent}">${esc(ev.name)}</strong><span class="muted small">${esc(label)} · turn ${ev.turn}</span></div>
      ${ev.task ? `<div class="task">${esc(ev.task)}</div>` : ""}
      <div class="bubble"><div class="steps"></div><div class="files"></div><div class="srcs"></div></div>
      <div class="turn-foot"><span class="verdict"></span><span class="cost"></span></div>
    </div>`;
  const stick = nearBottom();
  $("messages").appendChild(el);
  const t = { agent: ev.agent, el, steps: [], live: true };
  state.turns[key] = t;
  state.open = t;
  scrollDown(stick);
  return t;
}

// Steps: text and thinking grow in place; tool use and files are added as rows.
function addStep(t, step) {
  const box = t.el.querySelector(".steps");
  const last = t.steps[t.steps.length - 1];
  if (step.kind === "text" || step.kind === "thinking") t.gotText = true;
  if ((step.kind === "text" || step.kind === "thinking") && last && last.kind === step.kind) {
    last.text += step.text;
    paintSoon(last);
    return;
  }
  if (step.kind === "tool_result") {
    // Attach the result to its call when there is one.
    const call = step.tool_use_id
      ? t.steps.find((s) => s.kind === "tool_call" && s.id === step.tool_use_id)
      : [...t.steps].reverse().find((s) => s.kind === "tool_call" && !s.result && sameTool(s.name, step.name));
    if (call && !call.result) {
      call.result = step;
      call.node.outerHTML = toolHtml(call);
      call.node = box.querySelector(`[data-i="${call.i}"]`);
      scrollDown();
      return;
    }
  }
  if (step.kind === "file") { addFile(t, step); return; }
  const s = { ...step, i: t.steps.length };
  t.steps.push(s);
  const holder = document.createElement("div");
  box.appendChild(holder);
  holder.outerHTML = stepHtml(s);
  s.node = box.querySelector(`[data-i="${s.i}"]`);
  if (s.kind === "text" || s.kind === "thinking") paint(s);
  scrollDown();
}

function sameTool(callName, resultName) {
  if (callName === resultName) return true;
  return resultName === "code" && /code_execution/.test(callName);
}

function stepHtml(s) {
  if (s.kind === "text") return `<div class="step-text" data-i="${s.i}"></div>`;
  if (s.kind === "thinking") return `<div class="step-thinking" data-i="${s.i}"><span class="lbl">💭 Thinking</span><span class="t"></span></div>`;
  if (s.kind === "tool_call") return toolHtml(s);
  if (s.kind === "tool_result") return toolHtml({ i: s.i, name: s.name, input: {}, result: s });
  if (s.kind === "status") return `<div class="step-status" data-i="${s.i}">${esc(s.text)}</div>`;
  return `<div data-i="${s.i}"></div>`;
}

function toolHtml(s) {
  const i = s.input || {};
  const r = s.result;
  let title, body = "";
  if (s.name === "web_search") {
    title = `🔎 Searched <b>${esc(i.query || "")}</b>`;
    if (r && r.results) {
      title += ` · ${r.results.length} results`;
      body = `<div class="links">${r.results.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title || x.url)}</a>`).join("")}</div>`;
    }
  } else if (s.name === "web_fetch") {
    const url = i.url || (r && r.url) || "";
    title = `🌐 Read <a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>`;
  } else if (s.name === "bash_code_execution" || s.name === "code_execution" || s.name === "code") {
    title = "💻 Ran code";
    if (i.command || i.code) body += `<pre>${esc(i.command || i.code)}</pre>`;
    if (r && !r.error) {
      const out = [r.stdout, r.stderr].filter(Boolean).join("\n");
      title += r.return_code ? ` · failed (exit ${r.return_code})` : "";
      body += out ? `<div class="muted small">Output</div><pre>${esc(out)}</pre>` : `<div class="muted small">No output</div>`;
    }
  } else if (s.name === "text_editor_code_execution") {
    title = `📝 ${esc(i.command || "edit")} <code>${esc(i.path || "")}</code>`;
    if (i.file_text) body = `<pre>${esc(i.file_text)}</pre>`;
  } else {
    title = `🔧 ${esc(s.name)}`;
  }
  if (r && r.error) title += ` · <span class="error">failed: ${esc(r.error)}</span>`;
  else if (!r && s.kind !== "tool_result") title += ' <span class="muted">…</span>';
  // Collapsed by default: one line per step, click to see the code or results.
  return body
    ? `<details class="step-tool" data-i="${s.i}"><summary>${title}</summary>${body}</details>`
    : `<div class="step-tool" data-i="${s.i}">${title}</div>`;
}

function paintSoon(s) {
  if (s.raf) return;
  s.raf = true;
  requestAnimationFrame(() => { s.raf = false; paint(s); scrollDown(); });
}
function paint(s) {
  if (!s.node) return;
  if (s.kind === "text") s.node.innerHTML = md(stripVerdict(s.text));
  else s.node.querySelector(".t").textContent = s.text;
}

function addFile(t, f) {
  const box = t.el.querySelector(".files");
  if (box.querySelector(`[data-file="${f.file_id}"]`)) return;
  box.insertAdjacentHTML("beforeend",
    `<a class="file-chip" data-file="${esc(f.file_id)}" href="/api/files/${encodeURIComponent(f.file_id)}">⬇ ${esc(f.filename)}</a>`);
}

function endTurn(ev) {
  const key = `${ev.agent}-${ev.turn}`;
  let t = state.turns[key];
  if (!t) t = startTurn({ ...ev, name: state.agents[ev.agent] ? state.agents[ev.agent].name : `Claude ${ev.agent}` });
  if (!t.gotText) {
    // A reopened chat: the live text isn't replayed, so rebuild the turn.
    if (ev.steps && ev.steps.length) {
      // Newer chats saved the whole process with the turn.
      t.steps = [];
      t.el.querySelector(".steps").innerHTML = "";
      for (const s of ev.steps) addStep(t, s);
    } else if (ev.text) {
      // Older chats saved only the reply: show it after the tool steps.
      addStep(t, { kind: "text", text: ev.text });
    }
  }
  t.el.classList.remove("typing");
  if (state.open === t) state.open = null;
  for (const f of ev.files || []) addFile(t, f);
  if (ev.sources && ev.sources.length) {
    t.el.querySelector(".srcs").innerHTML = `<details class="sources"><summary>${ev.sources.length} sources</summary>${ev.sources.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a>`).join("")}</details>`;
  }
  if (ev.error) t.el.querySelector(".steps").insertAdjacentHTML("beforeend", `<p class="error small">${esc(ev.error)}</p>`);
  const v = ev.verdict;
  const badge = v === "AGREE" ? '<span class="badge AGREE">✓ Agrees</span>'
    : v === "DISAGREE" ? '<span class="badge DISAGREE">✗ Disagrees</span>'
    : v === "PROPOSED" ? '<span class="badge">Proposed answer</span>' : "";
  t.el.querySelector(".verdict").innerHTML = badge;
  t.el.querySelector(".cost").textContent = ev.turn_cost ? money(ev.turn_cost) : "";
  setTop($("top-title").textContent, state.status, ev.total_cost);
  scrollDown();
}

// ---------- settings pages ----------

async function loadKeyStatus() {
  $("key-msg").textContent = "";
  const s = await api("/api/settings");
  const el = $("key-status");
  if (s.mock) el.innerHTML = '<span class="muted">Demo mode: no key needed.</span>';
  else if (s.source === "saved") el.innerHTML = `<span class="ok">✓ A key is saved (${esc(s.key_hint)}).</span> Paste a new one below to replace it.`;
  else if (s.source === "env") el.innerHTML = `<span class="ok">✓ Using the key from the server's settings (${esc(s.key_hint)}).</span> A key saved here takes priority.`;
  else el.innerHTML = '<span class="error">No key yet.</span>';
  $("key-remove").hidden = s.source !== "saved";
}

async function onSaveKey(e) {
  e.preventDefault();
  const key = $("api-key").value.trim();
  if (!key) return;
  $("key-save").disabled = true;
  $("key-msg").className = "muted";
  $("key-msg").textContent = "Checking the key with Anthropic…";
  try {
    await post("/api/settings/key", { anthropic_api_key: key });
    $("api-key").value = "";
    $("key-msg").className = "ok";
    $("key-msg").textContent = "Saved.";
    await loadKeyStatus();
    $("key-msg").className = "ok";
    $("key-msg").textContent = "Saved.";
  } catch (err) {
    $("key-msg").className = "error";
    $("key-msg").textContent = err.message;
  } finally {
    $("key-save").disabled = false;
  }
}

async function onRemoveKey() {
  if (!confirm("Remove the saved API key? Nobody can ask questions until a new one is added.")) return;
  await api("/api/settings/key", { method: "DELETE" });
  await loadKeyStatus();
}

async function loadUsers() {
  const users = await api("/api/users");
  $("users-body").innerHTML = users.map((u) => `
    <tr data-id="${u.id}">
      <td>${esc(u.username)}${u.is_admin ? ' <span class="badge">admin</span>' : ""}${u.id === state.me.id ? ' <span class="muted small">(you)</span>' : ""}</td>
      <td>${u.chats}</td><td>${money(u.spent_usd)}</td>
      <td><button data-act="reset">New password</button>${u.id === state.me.id ? "" : ' <button data-act="del" class="danger">Delete</button>'}</td>
    </tr>`).join("");
  for (const tr of $("users-body").querySelectorAll("tr")) {
    const id = tr.dataset.id;
    const name = tr.querySelector("td").textContent.split(" ")[0];
    tr.querySelector('[data-act="reset"]').onclick = async () => {
      const pw = prompt(`New password for ${name} (8+ characters):`);
      if (!pw) return;
      try { await post(`/api/users/${id}/password`, { password: pw }); msg("users-msg", `Password changed for ${name}.`, true); }
      catch (err) { msg("users-msg", err.message); }
    };
    const del = tr.querySelector('[data-act="del"]');
    if (del) del.onclick = async () => {
      if (!confirm(`Delete ${name}? Their chats stay saved but they can't sign in.`)) return;
      try { await api(`/api/users/${id}`, { method: "DELETE" }); loadUsers(); }
      catch (err) { msg("users-msg", err.message); }
    };
  }
}

function msg(id, text, ok) { $(id).className = ok ? "ok" : "error"; $(id).textContent = text; }

async function onAddUser(e) {
  e.preventDefault();
  try {
    await post("/api/users", { username: $("nu-name").value.trim(), password: $("nu-pass").value, is_admin: $("nu-admin").checked });
    msg("users-msg", `Added ${$("nu-name").value.trim()}. Give them their username and password.`, true);
    $("add-user-form").reset();
    loadUsers();
  } catch (err) {
    msg("users-msg", err.data && Array.isArray(err.data.detail) ? "Usernames can use letters, numbers and . _ @ - only." : err.message);
  }
}

async function onChangePassword(e) {
  e.preventDefault();
  try {
    await post("/api/me/password", { current_password: $("pw-cur").value, new_password: $("pw-new").value });
    $("pw-form").reset();
    msg("pw-msg", "Password changed. Other devices were signed out.", true);
  } catch (err) { msg("pw-msg", err.message); }
}

boot();
