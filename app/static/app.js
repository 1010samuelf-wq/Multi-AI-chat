"use strict";

const $ = (id) => document.getElementById(id);
const state = { config: null, files: [], first: "A", source: null, current: null, msgEls: {} };

const STATUS_LABEL = {
  running: "Running", agreed: "Agreed", no_consensus: "No agreement", stopped: "Stopped",
  budget: "Spending cap reached", error: "Error", interrupted: "Interrupted",
};

function md(text) {
  return DOMPurify.sanitize(marked.parse(text || ""), { ADD_ATTR: ["target"] });
}
// The verdict is shown as a badge, so drop the raw "VERDICT: …" line.
function stripVerdict(text) {
  return (text || "").replace(/^\s*\**\s*VERDICT:.*$/gim, "").trimEnd();
}
function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function money(x) { return "$" + (x || 0).toFixed(x < 1 ? 3 : 2); }

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.json();
}

// ---------- setup ----------

async function init() {
  state.config = await api("/api/config");
  const c = state.config;
  for (const sel of ["model_a", "model_b"]) {
    $(sel).innerHTML = Object.entries(c.models).map(([id, label]) => `<option value="${id}">${esc(label)}</option>`).join("");
    $(sel).value = c.defaults[sel];
  }
  $("effort").innerHTML = c.efforts.map((e) => `<option>${e}</option>`).join("");
  $("effort").value = c.defaults.effort;
  $("max_turns").value = c.defaults.max_turns;
  $("max_turns").max = c.max_turns_limit;
  $("budget").value = c.defaults.budget_usd;
  $("budget").max = c.max_budget_limit;
  $("mock-note").hidden = !c.mock;

  $("first").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    state.first = b.dataset.v;
    for (const x of $("first").children) x.classList.toggle("on", x === b);
  });
  $("file-input").addEventListener("change", onFiles);
  $("new-form").addEventListener("submit", onStart);
  $("new-btn").addEventListener("click", () => { location.hash = ""; });
  $("settings-btn").addEventListener("click", () => { location.hash = "settings"; });
  $("key-form").addEventListener("submit", onSaveKey);
  $("key-remove").addEventListener("click", onRemoveKey);
  $("menu-btn").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  $("stop-btn").addEventListener("click", onStop);
  $("note-form").addEventListener("submit", onNote);
  window.addEventListener("hashchange", route);
  await loadHistory();
  route();
}

function route() {
  $("sidebar").classList.remove("open");
  const id = location.hash.slice(1);
  if (id === "settings") showSettings();
  else if (id) openDebate(id);
  else showNew();
}

function showView(name) {
  for (const v of ["new-view", "debate-view", "settings-view"]) $(v).hidden = v !== name;
}

async function showNew() {
  closeStream();
  state.current = null;
  showView("new-view");
  markActive();
  try { $("no-key").hidden = (await api("/api/settings")).has_key; } catch (_) {}
}

async function showSettings() {
  closeStream();
  state.current = null;
  showView("settings-view");
  markActive();
  $("key-msg").textContent = "";
  await refreshKeyStatus();
}

async function refreshKeyStatus() {
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
    await api("/api/settings/key", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ anthropic_api_key: key }),
    });
    $("api-key").value = "";
    $("key-msg").className = "ok";
    $("key-msg").textContent = "Saved. You can ask a question now.";
    await refreshKeyStatus();
  } catch (err) {
    $("key-msg").className = "error";
    $("key-msg").textContent = err.message;
  } finally {
    $("key-save").disabled = false;
  }
}

async function onRemoveKey() {
  if (!confirm("Remove the saved API key?")) return;
  await api("/api/settings/key", { method: "DELETE" });
  $("key-msg").textContent = "";
  await refreshKeyStatus();
}

// ---------- history ----------

async function loadHistory() {
  const items = await api("/api/debates");
  $("history").innerHTML = items.map((d) => `
    <li data-id="${d.id}">
      <div class="h-q">${esc(d.question)}</div>
      <div class="h-meta">${STATUS_LABEL[d.status] || d.status} · ${money(d.cost_usd)}</div>
    </li>`).join("") || '<li class="muted">Nothing yet</li>';
  for (const li of $("history").querySelectorAll("li[data-id]")) {
    li.addEventListener("click", () => { location.hash = li.dataset.id; });
  }
  markActive();
}
function markActive() {
  for (const li of $("history").querySelectorAll("li[data-id]")) {
    li.classList.toggle("active", li.dataset.id === state.current);
  }
}

// ---------- new debate ----------

async function onFiles(e) {
  for (const f of e.target.files) {
    const li = document.createElement("li");
    li.textContent = `Uploading ${f.name}…`;
    $("file-list").appendChild(li);
    const fd = new FormData();
    fd.append("file", f);
    try {
      const ref = await api("/api/upload", { method: "POST", body: fd });
      state.files.push(ref);
      li.innerHTML = `📎 ${esc(ref.filename)} <button type="button" title="Remove">×</button>`;
      li.querySelector("button").onclick = () => {
        state.files = state.files.filter((x) => x !== ref);
        li.remove();
      };
    } catch (err) {
      li.innerHTML = `<span class="error">${esc(f.name)}: ${esc(err.message)}</span>`;
    }
  }
  e.target.value = "";
}

async function onStart(e) {
  e.preventDefault();
  $("form-error").textContent = "";
  $("start-btn").disabled = true;
  try {
    const body = {
      question: $("question").value,
      model_a: $("model_a").value,
      model_b: $("model_b").value,
      first: state.first,
      max_turns: parseInt($("max_turns").value, 10),
      effort: $("effort").value,
      budget_usd: parseFloat($("budget").value),
      files: state.files,
    };
    const { id } = await api("/api/debates", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    $("question").value = "";
    state.files = [];
    $("file-list").innerHTML = "";
    location.hash = id;
    loadHistory();
  } catch (err) {
    $("form-error").textContent = err.message;
  } finally {
    $("start-btn").disabled = false;
  }
}

// ---------- debate view ----------

function closeStream() {
  if (state.source) { state.source.close(); state.source = null; }
}

async function openDebate(id) {
  closeStream();
  state.current = id;
  state.msgEls = {};
  state.agents = {};
  markActive();
  showView("debate-view");
  $("messages").innerHTML = "";
  $("final").innerHTML = "";
  $("d-files").innerHTML = "";
  let d;
  try { d = await api(`/api/debates/${id}`); } catch (err) {
    $("d-question").textContent = "Not found.";
    return;
  }
  $("d-question").textContent = d.question;
  setStatus(d.status);
  $("d-cost").textContent = money(d.cost_usd);
  $("controls").hidden = d.status !== "running";

  const src = new EventSource(`/api/debates/${id}/events`);
  state.source = src;
  src.onmessage = (m) => {
    if (state.current !== id) return;
    const ev = JSON.parse(m.data);
    handle(ev);
    if (ev.type === "end" || ev.type === "replay_done") { src.close(); loadHistory(); }
  };
  src.onerror = () => { /* the browser reconnects on its own */ };
}

function setStatus(s) {
  $("d-status").className = "badge " + s;
  $("d-status").textContent = STATUS_LABEL[s] || s;
}

function scrollIfNear() {
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 300;
  if (nearBottom) window.scrollTo({ top: document.body.scrollHeight });
}

function msgKey(ev) { return `${ev.agent}-${ev.turn}`; }

function currentMsg(agent) {
  // The latest open bubble for this agent.
  return state.openMsg && state.openMsg.agent === agent ? state.openMsg : null;
}

function handle(ev) {
  switch (ev.type) {
    case "start": {
      state.agents = ev.agents;
      if (ev.files && ev.files.length) {
        $("d-files").textContent = "Files: " + ev.files.map((f) => f.filename).join(", ");
      }
      const firstName = ev.agents[ev.order[0]].name;
      sys(`${ev.agents.A.name}: ${ev.agents.A.label} · ${ev.agents.B.name}: ${ev.agents.B.label} · ${firstName} answers first`);
      break;
    }
    case "turn_start": {
      const el = document.createElement("div");
      el.className = `msg ${ev.agent} typing`;
      const label = state.agents[ev.agent] ? state.agents[ev.agent].label : ev.model;
      el.innerHTML = `
        <div class="who"><strong>${esc(ev.name)}</strong><span class="muted">${esc(label)} · turn ${ev.turn}</span><span class="verdict"></span></div>
        <details class="thinking" hidden><summary>Thinking</summary><div class="thinking-text"></div></details>
        <details class="activity" hidden><summary>Activity</summary><div class="acts"></div></details>
        <div class="body"></div>
        <div class="files"></div>
        <div class="small muted cost"></div>`;
      $("messages").appendChild(el);
      state.openMsg = { agent: ev.agent, el, text: "", raf: false };
      state.msgEls[msgKey(ev)] = state.openMsg;
      scrollIfNear();
      break;
    }
    case "delta": {
      const m = currentMsg(ev.agent);
      if (!m) break;
      m.text += ev.text;
      if (!m.raf) {
        m.raf = true;
        requestAnimationFrame(() => {
          m.raf = false;
          m.el.querySelector(".body").innerHTML = md(stripVerdict(m.text));
          scrollIfNear();
        });
      }
      break;
    }
    case "thinking": {
      const m = currentMsg(ev.agent);
      if (!m) break;
      const d = m.el.querySelector("details.thinking");
      d.hidden = false;
      d.querySelector(".thinking-text").textContent += ev.text;
      break;
    }
    case "tool_call": case "tool_result": case "file": case "status": {
      const m = currentMsg(ev.agent);
      if (!m) { if (ev.type === "status") sys(ev.text); break; }
      if (ev.type === "file") { addFile(m.el, ev); break; }
      const d = m.el.querySelector("details.activity");
      d.hidden = false;
      const row = document.createElement("div");
      row.className = "act";
      row.innerHTML = describe(ev);
      d.querySelector(".acts").appendChild(row);
      const n = d.querySelectorAll(".act").length;
      d.querySelector("summary").textContent = `Activity (${n})`;
      // Separate text written before and after a tool call.
      if (ev.type === "tool_result" && m.text && !m.text.endsWith("\n\n")) m.text += "\n\n";
      break;
    }
    case "turn_end": {
      const m = state.msgEls[msgKey(ev)];
      if (!m) break;
      m.el.classList.remove("typing");
      m.text = ev.text || m.text;
      m.el.querySelector(".body").innerHTML = md(stripVerdict(m.text)) || '<p class="muted">(no reply)</p>';
      if (ev.error) m.el.querySelector(".body").insertAdjacentHTML("beforeend", `<p class="error">${esc(ev.error)}</p>`);
      const v = ev.verdict;
      if (v && v !== "PROPOSED") m.el.querySelector(".verdict").innerHTML = `<span class="badge ${v}">${v === "AGREE" ? "✓ Agrees" : "✗ Disagrees"}</span>`;
      else if (v === "PROPOSED") m.el.querySelector(".verdict").innerHTML = `<span class="badge">Proposed answer</span>`;
      for (const f of ev.files || []) if (!m.el.querySelector(`[data-file="${f.file_id}"]`)) addFile(m.el, f);
      if (ev.sources && ev.sources.length) {
        const d = m.el.querySelector("details.activity");
        d.hidden = false;
        if (!d.querySelector(".srcs")) {
          d.querySelector(".acts").insertAdjacentHTML("beforeend",
            `<div class="act srcs"><b>Sources</b><br>${ev.sources.map((s) => `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a>`).join("<br>")}</div>`);
        }
      }
      m.el.querySelector(".cost").textContent = `This turn ${money(ev.turn_cost)}`;
      $("d-cost").textContent = money(ev.total_cost);
      state.openMsg = null;
      break;
    }
    case "user_note": {
      $("messages").insertAdjacentHTML("beforeend", `<div class="note"><span>You: ${esc(ev.text)}</span></div>`);
      scrollIfNear();
      break;
    }
    case "end": {
      setStatus(ev.status);
      $("controls").hidden = true;
      $("d-cost").textContent = money(ev.total_cost);
      if (state.openMsg) state.openMsg.el.classList.remove("typing");
      const good = ev.status === "agreed";
      $("final").innerHTML = `
        <div class="card ${good ? "" : "warn"}">
          <h3>${good ? "✓ Agreed answer" : esc(STATUS_LABEL[ev.status] || ev.status)}</h3>
          <p class="small">${esc(ev.message)}</p>
          ${ev.final_answer ? `<div class="body">${md(ev.final_answer)}</div>` : ""}
        </div>`;
      scrollIfNear();
      break;
    }
  }
}

function describe(ev) {
  if (ev.type === "status") return esc(ev.text);
  if (ev.type === "tool_call") {
    const i = ev.input || {};
    if (ev.name === "web_search") return `🔎 Searching: <b>${esc(i.query)}</b>`;
    if (ev.name === "web_fetch") return `🌐 Reading: ${esc(i.url)}`;
    if (ev.name === "bash_code_execution") return `💻 Running:<pre>${esc(i.command)}</pre>`;
    if (ev.name === "text_editor_code_execution") return `📝 ${esc(i.command)} file <code>${esc(i.path)}</code>`;
    if (ev.name === "code_execution") return `💻 Running code:<pre>${esc(i.code || JSON.stringify(i))}</pre>`;
    return `🔧 ${esc(ev.name)}`;
  }
  if (ev.error) return `⚠️ ${esc(ev.name)} failed: ${esc(ev.error)}`;
  if (ev.name === "web_search") {
    return `Found ${ev.results.length} results:<br>` + ev.results.slice(0, 8).map((r) =>
      `<a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.title || r.url)}</a>`).join("<br>");
  }
  if (ev.name === "web_fetch") return `Read <a href="${esc(ev.url)}" target="_blank" rel="noopener">${esc(ev.url)}</a>`;
  if (ev.name === "code") {
    const out = [ev.stdout, ev.stderr].filter(Boolean).join("\n");
    return `Output (exit ${ev.return_code})${out ? `<pre>${esc(out)}</pre>` : ""}`;
  }
  return esc(JSON.stringify(ev));
}

function addFile(el, f) {
  el.querySelector(".files").insertAdjacentHTML("beforeend",
    `<a data-file="${esc(f.file_id)}" href="/api/files/${encodeURIComponent(f.file_id)}">⬇ ${esc(f.filename)}</a>`);
}

function sys(text) {
  $("messages").insertAdjacentHTML("beforeend", `<div class="sys">${esc(text)}</div>`);
}

async function onStop() {
  if (!state.current) return;
  $("stop-btn").disabled = true;
  try { await api(`/api/debates/${state.current}/stop`, { method: "POST" }); } catch (err) { alert(err.message); }
  $("stop-btn").disabled = false;
}

async function onNote(e) {
  e.preventDefault();
  const text = $("note").value.trim();
  if (!text || !state.current) return;
  try {
    await api(`/api/debates/${state.current}/note`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
    });
    $("note").value = "";
  } catch (err) { alert(err.message); }
}

init();
