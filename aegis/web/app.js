/* AEGIS front end. No framework, no build step - one file, plain DOM. */

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  hardware: null,
  providers: [],
  models: [],
  chats: [],
  chatId: '',            // '' means an unsaved new chat
  attachments: [],       // staged for the next message
  messages: [],
  streaming: null,       // AbortController while a reply is in flight
  eventCount: 0,
  fitCache: new Map(),
  pollTimer: null,
};

/* ------------------------------------------------------------------ utils */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  return data;
}

function toast(message, isError = false) {
  $$('.toast').forEach(t => t.remove());
  const el = document.createElement('div');
  el.className = 'toast' + (isError ? ' err' : '');
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isError ? 7000 : 3500);
}

const gb = n => (n / 1073741824);
function bytes(n) {
  if (!n) return '—';
  if (n >= 1073741824) return gb(n).toFixed(2) + ' GB';
  if (n >= 1048576) return (n / 1048576).toFixed(0) + ' MB';
  return (n / 1024).toFixed(0) + ' KB';
}
function duration(s) {
  if (s == null) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ------------------------------------------------------------------- tabs */

$$('nav button').forEach(btn => btn.addEventListener('click', () => {
  $$('nav button').forEach(b => b.classList.toggle('active', b === btn));
  $$('.pane').forEach(p => p.classList.toggle('active', p.id === btn.dataset.tab));
  if (btn.dataset.tab === 'models') loadModels();
  if (btn.dataset.tab === 'tools') { loadToolsTab(); loadDiscovered(); }
  if (btn.dataset.tab === 'brain') { loadBrain(); }
  if (btn.dataset.tab === 'filetasks') { loadFilesTab(); loadTasks(); }
  if (btn.dataset.tab === 'browser') loadBrowserTab(); else brStopLive();
  if (btn.dataset.tab === 'providers') {
    loadProviders(); loadKeys(); loadOAuth(); loadRoutes(); loadCustomProviders();
    loadBudgets();
  }
  if (btn.dataset.tab === 'settings') { loadSettings(); loadRunner(); }
}));

/* --------------------------------------------------------------- hardware */

async function loadHardware(refresh = false) {
  const hw = await api('/api/hardware' + (refresh ? '?refresh=true' : ''));
  state.hardware = hw;
  $('#hwchip').innerHTML =
    `<span><b>${esc(hw.accelerator)}</b>${hw.vram_total_gb ? ` · ${hw.vram_total_gb} GB VRAM` : ''}</span>
     <span><b>${hw.ram_available_gb}</b> / ${hw.ram_total_gb} GB RAM free</span>`;

  const box = $('#hwDetail');
  if (box) box.innerHTML = `
    <div class="card-sub">${esc(hw.cpu_name)} · ${hw.cpu_cores} cores / ${hw.cpu_threads} threads</div>
    <div class="pills">
      <span class="pill">RAM ${hw.ram_total_gb} GB (${hw.ram_available_gb} GB free)</span>
      <span class="pill">Disk ${hw.disk_free_gb} GB free</span>
      ${hw.gpus.map(g => `<span class="pill">${esc(g.name)}${
        g.integrated ? ' · shared memory' : ` · ${g.vram_total_gb} GB VRAM`}</span>`).join('')}
      ${hw.has_discrete_gpu ? '' : '<span class="pill">No discrete GPU — CPU inference</span>'}
    </div>`;
}

/* ------------------------------------------------------------------- chat */

async function loadProviderSelect() {
  const { providers } = await api('/api/providers');
  state.providers = providers;
  const sel = $('#providerSelect');
  const previous = sel.value;
  const direct = providers.filter(p => p.kind !== 'route');
  sel.innerHTML = direct.map(p =>
    `<option value="${p.key}" ${p.ready ? '' : 'data-notready="1"'}>${
      esc(p.label)}${p.ready ? '' : ' (not ready)'}</option>`).join('');
  const settings = await api('/api/settings');
  sel.value = previous || settings.settings.default_provider ||
              (direct.find(p => p.ready)?.key ?? direct[0]?.key);
  await loadRouteSelect();
  await loadModelSelect();
}

/* A route replaces the provider+model pair: it picks for you, in order. */
async function loadRouteSelect() {
  let data;
  try { data = await api('/api/routes'); } catch { return; }
  state.routes = data.routes;
  const sel = $('#routeSelect');
  const previous = sel.value;
  sel.innerHTML = '<option value="">Direct — pick a model</option>'
    + data.routes.map(r => {
        const state_ = r.ready
          ? (r.next ? ` → ${r.next.model}` : '')
          : ' — none available';
        return `<option value="route:${r.key}">${esc(r.label)}${esc(state_)}</option>`;
      }).join('');
  sel.value = previous
    || (data.default_route ? `route:${data.default_route}` : '');
  applyRouteMode();
}

function applyRouteMode() {
  const routed = Boolean($('#routeSelect').value);
  $('#providerSelect').disabled = routed;
  $('#modelSelect').disabled = routed;
  $('#providerSelect').style.opacity = routed ? 0.45 : 1;
  $('#modelSelect').style.opacity = routed ? 0.45 : 1;
}

$('#routeSelect').addEventListener('change', () => {
  applyRouteMode();
  if (!$('#routeSelect').value) loadModelSelect();
});

async function loadModelSelect() {
  const sel = $('#modelSelect');
  const key = $('#providerSelect').value;
  sel.innerHTML = '<option>…</option>';
  try {
    const { models } = await api(`/api/providers/${key}/models`);
    sel.innerHTML = models.length
      ? models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('')
      : '<option value="">no models</option>';
  } catch {
    sel.innerHTML = '<option value="">unavailable</option>';
  }
}

$('#providerSelect').addEventListener('change', loadModelSelect);
/* ---------------------------------------------------------- attachments */

async function ensureChat() {
  if (state.chatId) return state.chatId;
  const res = await api('/api/chats', { method: 'POST', body: {} });
  state.chatId = res.chat.id;
  loadChats();
  refreshChatHeader(res.chat);
  return state.chatId;
}

function renderAttachments() {
  const row = $('#attachRow');
  row.hidden = state.attachments.length === 0;
  row.innerHTML = state.attachments.map(a => `
    <span class="chip ${a.pending ? 'busy' : ''}">
      <span class="n">${esc(a.name)}</span>
      <span class="m">${a.pending ? (a.progress != null && a.progress < 1
          ? `uploading ${Math.round(a.progress * 100)}%` : 'reading…')
        : a.kind === 'large' ? `${bytes(a.bytes)} · stored, tools use it by path`
        : a.inline ? 'in message'
        : a.chars ? `${(a.chars / 1000).toFixed(0)}k chars · searchable`
        : a.kind === 'image' ? 'image' : esc(a.note || 'no text')}</span>
      ${a.pending ? '' : `<button data-drop-attach="${a.id}" title="Remove">×</button>`}
    </span>`).join('');
}

/* v2.1: each file is streamed to disk on its own request, with progress, so
   anything up to the limit (30 GB by default) uploads without filling memory. */
function putFile(chatId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/chats/${chatId}/upload?name=${encodeURIComponent(file.name)}&size=${file.size}`);
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch { /* keep {} */ }
      if (xhr.status >= 400 || data.ok === false) reject(new Error(data.error || `HTTP ${xhr.status}`));
      else resolve(data);
    };
    xhr.onerror = () => reject(new Error('connection lost'));
    xhr.send(file);
  });
}

async function uploadFiles(fileList) {
  const list = [...fileList];
  if (!list.length) return;
  const chatId = await ensureChat();
  let limit = null;
  try { limit = await api('/api/upload-limit'); } catch { /* checked server-side anyway */ }

  for (const file of list) {
    if (limit && file.size > limit.max_bytes) {
      toast(`${file.name} is ${bytes(file.size)} — over the ${bytes(limit.max_bytes)} limit`, true);
      continue;
    }
    if (limit && file.size > limit.free_bytes) {
      toast(`Not enough disk space for ${file.name} (${bytes(limit.free_bytes)} free)`, true);
      continue;
    }
    const chip = { name: file.name, pending: true, progress: 0 };
    state.attachments.push(chip);
    renderAttachments();
    let last = 0;
    try {
      const item = await putFile(chatId, file, p => {
        chip.progress = p;
        if (p - last > 0.01 || p === 1) { last = p; renderAttachments(); }
      });
      Object.assign(chip, item, { pending: false });
      if (item.kind === 'large') toast(`${file.name} stored (${bytes(file.size)}) — the tools will work on it by path`);
    } catch (err) {
      state.attachments = state.attachments.filter(a => a !== chip);
      toast(`Upload of ${file.name} failed: ${err.message}`, true);
    }
    renderAttachments();
  }
  const big = state.attachments.filter(a => !a.pending && !a.inline && a.chars);
  if (big.length) toast(`${big.length} large file(s) indexed — the model will search them, not read them whole`);
}

$('#attachBtn').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', e => {
  uploadFiles(e.target.files);
  e.target.value = '';
});
$('#attachRow').addEventListener('click', async e => {
  const btn = e.target.closest('[data-drop-attach]');
  if (!btn) return;
  const id = btn.dataset.dropAttach;
  await api(`/api/attachments/${id}`, { method: 'DELETE' });
  state.attachments = state.attachments.filter(a => a.id !== id);
  renderAttachments();
});

/* Drag files anywhere onto the composer. */
const composer = document.querySelector('.composer');
['dragenter', 'dragover'].forEach(evt =>
  composer.addEventListener(evt, e => {
    e.preventDefault(); composer.classList.add('dragging');
  }));
['dragleave', 'drop'].forEach(evt =>
  composer.addEventListener(evt, e => {
    e.preventDefault(); composer.classList.remove('dragging');
  }));
composer.addEventListener('drop', e => {
  if (e.dataTransfer?.files?.length) uploadFiles(e.dataTransfer.files);
});

/* ------------------------------------------------------- chat management */

async function loadChats() {
  const q = $('#chatSearch').value.trim();
  let data;
  try { data = await api('/api/chats' + (q ? `?q=${encodeURIComponent(q)}` : '')); }
  catch { return; }
  state.chats = data.chats;
  const shown = state.project
    ? data.chats.filter(c => c.project === state.project) : data.chats;
  const pname = k => (state.projectList || []).find(p => p.key === k)?.name || k;

  $('#chatList').innerHTML = shown.length ? shown.map(c => `
    <div class="chat-item ${c.id === state.chatId ? 'active' : ''}" data-chat="${c.id}">
      <div class="t">${c.running ? '<span class="live"></span>' : ''}${
        c.pinned ? '★ ' : ''}${esc(c.title || 'Untitled')}</div>
      <div class="m">${c.messages} msg${c.messages === 1 ? '' : 's'} · ${
        relTime(c.updated)}${c.project && !state.project
          ? ` · <span class="proj-tag">${esc(pname(c.project))}</span>` : ''}</div>
    </div>`).join('')
    : `<div class="empty" style="padding:1.5rem .5rem">${
        q ? 'Nothing matched.' : 'No chats yet.'}</div>`;

  try {
    const s = await api('/api/store');
    $('#storeStats').textContent =
      `${s.chats} chats · ${bytes(s.db_bytes)} · ${s.images} images (${
        bytes(s.image_bytes)}) · ${s.search}`;
  } catch { /* stats are cosmetic */ }
}

function relTime(ts) {
  const secs = Math.max(0, Date.now() / 1000 - ts);
  if (secs < 90) return 'just now';
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  if (secs < 604800) return `${Math.round(secs / 86400)}d ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

async function openChat(chatId) {
  if (state.streaming) { state.streaming.abort(); state.streaming = null; }
  state.chatId = chatId;
  state.attachments = [];
  renderAttachments();
  let data;
  try { data = await api(`/api/chats/${chatId}`); }
  catch (err) { return toast(err.message, true); }

  state.messages = data.messages.map(m => ({
    role: m.role, content: m.content, parts: m.parts || [],
    usage: m.usage, error: m.error || '',
  }));
  renderMessages();
  refreshChatHeader(data.chat);
  loadChats();

  if (data.chat.provider) $('#providerSelect').value = data.chat.provider;
  const owner = (state.chats.find(c => c.id === chatId) || {}).project || '';
  if (owner !== (state.project || '') && typeof setProject === 'function') {
    setProject(owner, { keepChat: true });
  }
  if (data.run?.running) reattach(chatId, 0);
}

function refreshChatHeader(chat) {
  chat = chat || state.chats.find(c => c.id === state.chatId);
  $('#chatTitle').textContent = chat?.title || 'Untitled';
  $('#pinChat').classList.toggle('on', Boolean(chat?.pinned));
  $('#pinChat').textContent = chat?.pinned ? '★' : '☆';
}

async function newChat() {
  if (state.streaming) { state.streaming.abort(); state.streaming = null; }
  state.chatId = '';
  state.messages = [];
  state.attachments = [];
  renderAttachments();
  renderMessages();
  $('#chatTitle').textContent = 'New chat';
  $('#pinChat').classList.remove('on');
  $('#input').focus();
  loadChats();
}

$('#newChat').addEventListener('click', newChat);
$('#chatList').addEventListener('click', e => {
  const item = e.target.closest('[data-chat]');
  if (item) openChat(item.dataset.chat);
});

let searchTimer;
$('#chatSearch').addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(loadChats, 220);
});

$('#collapseSide').addEventListener('click', () =>
  $('#sidebar').classList.toggle('hidden'));

$('#chatTitle').addEventListener('click', async () => {
  if (!state.chatId) return;
  const current = $('#chatTitle').textContent;
  const title = prompt('Rename this chat', current);
  if (title === null) return;
  await api(`/api/chats/${state.chatId}`, { method: 'PATCH', body: { title } });
  refreshChatHeader({ title, pinned: $('#pinChat').classList.contains('on') });
  loadChats();
});

$('#pinChat').addEventListener('click', async () => {
  if (!state.chatId) return;
  const pinned = !$('#pinChat').classList.contains('on');
  const res = await api(`/api/chats/${state.chatId}`,
                        { method: 'PATCH', body: { pinned } });
  refreshChatHeader(res.chat);
  loadChats();
});

$('#exportChat').addEventListener('click', () => {
  if (!state.chatId) return toast('Nothing to export yet', true);
  window.open(`/api/chats/${state.chatId}/export?format=md`, '_blank');
});

$('#deleteChat').addEventListener('click', async () => {
  if (!state.chatId) return;
  if (!confirm('Delete this chat and everything in it? This cannot be undone.')) return;
  await api(`/api/chats/${state.chatId}`, { method: 'DELETE' });
  toast('Deleted');
  newChat();
});

/* Click a tool screenshot to see it full size. */
$('#messages').addEventListener('click', e => {
  if (e.target.classList?.contains('shot')) e.target.classList.toggle('big');
});

function renderMessages() {
  const box = $('#messages');
  if (!state.messages.length) {
    box.innerHTML = '<div class="empty">Pick a provider and say something.<br>'
      + 'Chats are saved automatically — runs keep going if you close the window.</div>';
    return;
  }
  box.innerHTML = state.messages.map((m, i) => `
    <div class="msg ${m.role}">
      <div class="who">${m.role === 'user' ? 'YOU' : 'AI'}</div>
      <div class="body" id="body-${i}">${
        m.role === 'assistant' ? renderAssistant(m) : esc(m.content)}</div>
    </div>`).join('');
  box.scrollTop = box.scrollHeight;
}

/* An assistant turn is a list of parts rendered in the order they happened,
   so tool calls appear between the paragraphs that prompted them. */
function renderAssistant(m) {
  let html = '';
  for (const part of m.parts || []) {
    if (part.kind === 'text')   html += `<div class="md">${renderMarkdown(part.text)}</div>`;
    if (part.kind === 'notice') html += `<div class="notice">${esc(part.text)}</div>`;
    if (part.kind === 'meta')   html += `<div class="meta-chip">${esc(part.text)}</div>`;
    if (part.kind === 'draft')  html += `<details class="draft"><summary>first draft (before review)</summary><div class="md">${renderMarkdown(part.text)}</div></details>`;
    if (part.kind === 'review') html += `<details class="review"><summary class="meta-chip">✦ ${esc(part.text)}</summary><div class="md">${renderMarkdown(part.verdict || '')}</div></details>`;
    if (part.kind === 'step')   html += `<div class="trace-step">— step ${part.n} —</div>`;
    if (part.kind === 'tool')   html += renderTool(part);
    if (part.kind === 'approval') html += renderApproval(part);
  }
  if (m.error) html += `<div class="err">${esc(m.error)}</div>`;
  if (m.usage) html += `<div class="meta">${m.usage.input || 0} in · ${m.usage.output || 0} out</div>`;
  return html;
}

function renderTool(t) {
  const icon = t.done ? (t.ok ? '✓' : '✗') : '⋯';
  const risk = t.risk === 'write' ? ' <span class="badge amber">write</span>' : '';

  // Stored runs reference images by blob id; a live run only knows the count.
  let shots = '';
  if (t.blobs?.length) {
    shots = t.blobs.map(id =>
      `<img class="shot" src="/api/blobs/${esc(id)}" loading="lazy"
            alt="tool screenshot"
            onerror="this.outerHTML='<div class=&quot;shot-gone&quot;>image dropped to stay inside the chat image budget</div>'">`
    ).join('');
  } else if (t.images) {
    shots = `<div class="shot-gone">${t.images} image${
      t.images > 1 ? 's' : ''} returned</div>`;
  }

  return `<div class="trace"><div class="trace-item ${t.done && !t.ok ? 'bad' : ''}">
    <span>${icon}</span> <span class="tname">${esc(t.name)}</span>${risk}
    <div class="targs">${esc(t.argsText || '')}</div>
    ${t.result !== undefined ? (t.name === 'plan__update'
        ? `<div class="md plan">${renderMarkdown(String(t.result))}</div>`
        : `<div class="tres">${esc(String(t.result).replace(/\[\[(media|file):[^\]]*\]\]/g, '').trim())}</div>`) : ''}
    ${mediaFromResult(t.result)}
    ${shots}
  </div></div>`;
}

/* Generated media named in a tool result is shown right in the transcript. */
function mediaFromResult(text) {
  if (!text) return '';
  const found = [...String(text).matchAll(/\[\[media:([a-f0-9]{6,32}):([a-z0-9.+\/-]*)\]\]/gi)];
  const files = [...String(text).matchAll(/\[\[file:([^\]]+)\]\]/g)];
  return found.map(m => mediaTag(m[1], m[2])).join('')
    + files.map(m => fileChip(m[1])).join('');
}

/* Colour a unified diff (or show plain text) in the approval card. */
function diffHtml(text) {
  const lines = String(text).split('\n');
  if (!lines.some(l => l.startsWith('@@'))) return esc(text);
  return lines.map(l => {
    const cls = l.startsWith('+') && !l.startsWith('+++') ? 'add'
      : l.startsWith('-') && !l.startsWith('---') ? 'del' : l.startsWith('@@') ? 'hunk' : '';
    return `<span class="dl ${cls}">${esc(l) || ' '}</span>`;
  }).join('');
}

function renderApproval(a) {
  if (a.resolved) {
    return `<div class="approval resolved"><div class="atitle">${
      a.approved ? 'Approved' : 'Declined'}: ${esc(a.tool)}</div></div>`;
  }
  return `<div class="approval" id="ap-${a.id}">
    <div class="atitle">Approve <code>${esc(a.tool)}</code>?</div>
    <div class="card-sub">${esc(a.server)} · classified as a ${esc(a.risk)}</div>
    <div class="aargs">${diffHtml(a.summary || '')}</div>
    <div class="arow">
      <button class="sm primary" data-approve="${a.id}">Allow once</button>
      <button class="sm" data-approve="${a.id}" data-remember="1">Always allow this tool</button>
      <button class="sm danger" data-deny="${a.id}">Decline</button>
    </div>
  </div>`;
}

/* Fold one stream event into the assistant turn's parts list. */
function applyEvent(reply, evt) {
  const parts = reply.parts;
  const last = parts[parts.length - 1];

  switch (evt.type) {
    case 'delta':
      reply.content += evt.text;
      if (last && last.kind === 'text') last.text += evt.text;
      else parts.push({ kind: 'text', text: evt.text });
      break;
    case 'notice':
      parts.push({ kind: 'notice', text: evt.text });
      break;
    case 'route_pick':
    case 'agent_pick':
      parts.push({ kind: 'meta', text: evt.text });
      break;
    case 'boost_revision':
      parts.forEach(p => { if (p.kind === 'text') p.kind = 'draft'; });
      parts.push({ kind: 'review', text: evt.text, verdict: evt.verdict || '' });
      break;
    case 'step':
      parts.push({ kind: 'step', n: evt.n });
      break;
    case 'tool_call':
      parts.push({ kind: 'tool', id: evt.id, name: evt.name, risk: evt.risk,
                   server: evt.server, done: false,
                   argsText: JSON.stringify(evt.arguments || {}).slice(0, 300) });
      break;
    case 'tool_result': {
      const call = [...parts].reverse().find(p => p.kind === 'tool' && p.id === evt.id);
      if (call) Object.assign(call, { done: true, ok: evt.ok,
                                      result: evt.preview, images: evt.images });
      break;
    }
    case 'approval_request':
      parts.push({ kind: 'approval', id: evt.id, tool: evt.tool, server: evt.server,
                   risk: evt.risk, summary: evt.summary, resolved: false });
      break;
    case 'approval_resolved': {
      const open = [...parts].reverse().find(p => p.kind === 'approval' && !p.resolved);
      if (open) Object.assign(open, { resolved: true, approved: evt.approved });
      break;
    }
    case 'usage':
      reply.usage = evt;
      break;
    case 'error':
      reply.error = evt.text;
      break;
  }
}

/* Approve / decline, wherever the buttons are in the transcript. */
$('#messages').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const id = btn.dataset.approve || btn.dataset.deny;
  if (!id) return;
  const approved = Boolean(btn.dataset.approve);

  btn.closest('.arow')?.querySelectorAll('button').forEach(b => b.disabled = true);
  try {
    await api(`/api/approvals/${id}`, { method: 'POST', body: {
      approved, remember: Boolean(btn.dataset.remember) } });
  } catch (err) { toast(err.message, true); return; }

  for (const m of state.messages) {
    const part = (m.parts || []).find(p => p.kind === 'approval' && p.id === id);
    if (part) Object.assign(part, { resolved: true, approved });
  }
  renderMessages();
  if (btn.dataset.remember) loadToolsTab();
});

/* Consume an SSE stream into one assistant turn. Shared by a fresh send and by
   reattaching to a run that was already going. */
async function consumeStream(res, reply, controller) {
  const index = state.messages.indexOf(reply);
  const paint = () => {
    const el = $(`#body-${index}`);
    if (el) el.innerHTML = renderAssistant(reply);
    const box = $('#messages');
    if (box.scrollHeight - box.scrollTop - box.clientHeight < 200) {
      box.scrollTop = box.scrollHeight;
    }
  };

  state.streaming = controller;
  state.eventCount = state.eventCount || 0;
  $('#send').hidden = true;
  $('#stop').hidden = false;

  try {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split('\n\n');
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line.slice(5).trim()); } catch { continue; }
        state.eventCount++;
        applyEvent(reply, evt);
        paint();
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') reply.error = String(err.message || err);
    paint();
  } finally {
    state.streaming = null;
    $('#send').hidden = false;
    $('#stop').hidden = true;
    loadChats();
  }
}

async function send() {
  const input = $('#input');
  const text = input.value.trim();
  if ((!text && !state.attachments.length) || state.streaming) return;

  const attachmentIds = state.attachments.filter(a => a.id).map(a => a.id);
  const names = state.attachments.map(a => a.name);
  state.attachments = [];
  renderAttachments();

  state.messages.push({
    role: 'user',
    content: text + (names.length ? `\n\n[attached: ${names.join(', ')}]` : ''),
  });
  const reply = { role: 'assistant', content: '', parts: [], error: '', usage: null };
  state.messages.push(reply);
  input.value = '';
  input.style.height = 'auto';
  renderMessages();

  const controller = new AbortController();
  state.eventCount = 0;
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      body: JSON.stringify({
        chat_id: state.chatId || '',
        // A route overrides the provider/model pair entirely.
        provider: $('#routeSelect').value || $('#providerSelect').value,
        model: $('#routeSelect').value ? '' : $('#modelSelect').value,
        use_tools: $('#useTools').checked,
        project: state.project || '',
        agent: $('#agentSelect')?.value || '',
        boost: $('#boostSelect')?.value || '',
        attachment_ids: attachmentIds,
        text: text || '(see the attached files)',
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${res.status}`);
    }
    // The server creates the chat on the first message and names it here.
    const id = res.headers.get('X-Aegis-Chat-Id');
    if (id && id !== state.chatId) {
      state.chatId = id;
      loadChats();
      refreshChatHeader();
    }
    await consumeStream(res, reply, controller);
  } catch (err) {
    reply.error = String(err.message || err);
    renderMessages();
    state.streaming = null;
    $('#send').hidden = false;
    $('#stop').hidden = true;
  }
}

/* Rejoin a run that kept going while the window was closed. */
async function reattach(chatId, fromIndex) {
  const reply = { role: 'assistant', content: '', parts: [], error: '', usage: null };
  // The stored transcript already holds this turn's parts so far; replace the
  // last assistant turn rather than appending a second one.
  const last = state.messages[state.messages.length - 1];
  if (last && last.role === 'assistant') state.messages.pop();
  state.messages.push(reply);
  renderMessages();

  const controller = new AbortController();
  try {
    const res = await fetch(`/api/chats/${chatId}/stream?from_index=0`,
                            { signal: controller.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    toast('Rejoined a run that was still going');
    await consumeStream(res, reply, controller);
  } catch (err) {
    reply.error = String(err.message || err);
    renderMessages();
  }
}

$('#send').addEventListener('click', send);
$('#stop').addEventListener('click', async () => {
  state.streaming?.abort();
  if (state.chatId) await api(`/api/chats/${state.chatId}/stop`, { method: 'POST' });
});
$('#input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
$('#input').addEventListener('input', e => {
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 190) + 'px';
});

/* ----------------------------------------------------------------- models */

async function loadModels() {
  const list = $('#modelList');
  list.innerHTML = '<div class="empty"><span class="spinner"></span></div>';
  try {
    const { models } = await api('/api/models');
    state.models = models;
    list.innerHTML = models.map(m => modelCard(m)).join('') ||
      '<div class="empty">Nothing found. Is Ollama running?</div>';
    models.forEach(m => assessCard(m));
  } catch (err) {
    list.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
  pollDownloads();
}

function cardId(m) { return 'm-' + btoa(unescape(encodeURIComponent(m.id))).replace(/=/g, ''); }

function modelCard(m, opts = {}) {
  const id = cardId(m);
  const size = m.size_bytes
    ? bytes(m.size_bytes) + (m.size_estimated ? ' approx.' : '')
    : (m.source === 'hf' && !opts.isFile ? '' : 'size unknown');

  let actions = '';
  if (m.installed && m.source === 'aegis') {
    actions = `<button class="sm" data-run="${esc(m.path)}">Load</button>
               <button class="sm danger" data-del="${esc(m.path)}">Delete</button>`;
  } else if (m.installed) {
    actions = `<span class="badge grey">installed</span>`;
  } else if (m.source === 'ollama') {
    actions = `<button class="sm primary" data-pull="${esc(m.download_ref)}">Pull</button>`;
  } else if (m.source === 'hf' && opts.isFile) {
    actions = `<button class="sm primary" data-hf="${esc(m.download_ref)}">Download</button>`;
  } else if (m.source === 'hf') {
    actions = `<button class="sm" data-expand="${esc(m.download_ref)}">Show files</button>`;
  }

  return `<div class="card" id="${id}">
    <div class="card-head">
      <div style="min-width:0">
        <div class="card-title">${esc(m.name)}</div>
        <div class="card-sub">${esc(m.description || m.family || '')}</div>
        <div class="pills">
          <span class="pill">${esc(m.source)}</span>
          ${m.params ? `<span class="pill">${esc(m.params)}</span>` : ''}
          ${m.quant ? `<span class="pill mono">${esc(m.quant)}</span>` : ''}
          ${size ? `<span class="pill">${size}</span>` : ''}
        </div>
      </div>
      <div class="card-actions">${actions}</div>
    </div>
    <div class="fit" data-fit hidden></div>
    <div data-files></div>
  </div>`;
}

async function assessCard(m, opts = {}) {
  if (!m.size_bytes && !(m.source === 'hf' && opts.isFile)) return;
  const card = document.getElementById(cardId(m));
  if (!card) return;
  const slot = $('[data-fit]', card);
  slot.hidden = false;
  slot.innerHTML = '<span class="spinner"></span>';

  const body = { size_bytes: m.size_bytes };
  if (m.path) body.path = m.path;
  else if (m.source === 'hf' && opts.isFile) body.hf_ref = m.download_ref;

  try {
    const fit = await api('/api/assess', { method: 'POST', body });
    const b = fit.breakdown || {};
    const pct = b.budget_gb ? Math.min(100, (b.required_gb / b.budget_gb) * 100) : 0;
    slot.innerHTML = `
      <span class="badge ${fit.verdict}">${esc(fit.headline)}</span>
      <div class="meter ${fit.verdict}"><i style="width:${pct.toFixed(0)}%"></i></div>
      <div class="fit-detail">${esc(fit.detail)}</div>
      <div class="fit-bars">
        <span><b>weights</b> ${b.weights_gb ?? '?'} GB</span>
        <span><b>KV @ ${fit.context ?? '?'}</b> ${b.kv_cache_gb ?? '?'} GB</span>
        <span><b>overhead</b> ${b.overhead_gb ?? '?'} GB</span>
        <span><b>total</b> ${b.required_gb ?? '?'} GB of ${b.budget_gb ?? '?'} GB</span>
        ${fit.throughput_ceiling_tps
          ? `<span><b>ceiling</b> ~${fit.throughput_ceiling_tps} tok/s</span>` : ''}
        <span><b>KV source</b> ${esc(fit.kv_source)}</span>
      </div>`;
  } catch {
    slot.innerHTML = '<span class="badge grey">could not assess</span>';
  }
}

/* click delegation for every model action */
$('#models').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;

  if (btn.dataset.pull) {
    btn.disabled = true;
    await api('/api/downloads/ollama', { method: 'POST', body: { model: btn.dataset.pull } });
    toast(`Pulling ${btn.dataset.pull}`);
    pollDownloads();
  }
  if (btn.dataset.hf) {
    btn.disabled = true;
    await api('/api/downloads/hf', { method: 'POST', body: { ref: btn.dataset.hf } });
    toast('Download started');
    pollDownloads();
  }
  if (btn.dataset.cancel) {
    await api(`/api/downloads/${btn.dataset.cancel}/cancel`, { method: 'POST' });
    pollDownloads();
  }
  if (btn.dataset.del) {
    const res = await api('/api/models/local?path=' + encodeURIComponent(btn.dataset.del),
                          { method: 'DELETE' });
    toast(res.message, !res.ok);
    loadModels();
  }
  if (btn.dataset.run) {
    btn.disabled = true;
    toast('Starting llama-server…');
    const res = await api('/api/runner/start', { method: 'POST', body: { path: btn.dataset.run } });
    btn.disabled = false;
    toast(res.ok ? `Loaded ${res.model} on port ${res.port}` : res.error, !res.ok);
    if (res.ok) loadProviderSelect();
  }
  if (btn.dataset.expand) {
    const card = btn.closest('.card');
    const slot = $('[data-files]', card);
    if (slot.innerHTML) { slot.innerHTML = ''; btn.textContent = 'Show files'; return; }
    btn.textContent = 'Loading…';
    const { files } = await api('/api/models/files?repo=' + encodeURIComponent(btn.dataset.expand));
    btn.textContent = files.length ? 'Hide files' : 'No GGUF files';
    slot.innerHTML = files.map(f => modelCard(f, { isFile: true })).join('');
    files.forEach(f => assessCard(f, { isFile: true }));
  }
});

$('#hfSearchBtn').addEventListener('click', hfSearch);
$('#hfSearch').addEventListener('keydown', e => { if (e.key === 'Enter') hfSearch(); });
$('#refreshModels').addEventListener('click', () => { loadHardware(true); loadModels(); });

async function hfSearch() {
  const q = $('#hfSearch').value.trim();
  const box = $('#searchResults');
  if (!q) { box.hidden = true; return; }
  box.hidden = false;
  $('#searchList').innerHTML = '<div class="empty"><span class="spinner"></span></div>';
  try {
    const { models } = await api('/api/models/search?q=' + encodeURIComponent(q));
    $('#searchList').innerHTML = models.map(m => modelCard(m)).join('') ||
      '<div class="empty">Nothing on Hugging Face matched that.</div>';
  } catch (err) {
    $('#searchList').innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

/* --------------------------------------------------------------- downloads */

async function pollDownloads() {
  clearTimeout(state.pollTimer);
  let jobs = [];
  try { ({ jobs } = await api('/api/downloads')); } catch { return; }

  const box = $('#downloadsBox');
  box.hidden = jobs.length === 0;
  $('#downloadList').innerHTML = jobs.map(j => {
    const badge = { done: 'green', error: 'red', cancelled: 'grey' }[j.status] || 'amber';
    return `<div class="card">
      <div class="card-head">
        <div style="min-width:0">
          <div class="card-title">${esc(j.label)}</div>
          <div class="card-sub">${esc(j.message || j.error || j.status)}</div>
        </div>
        <div class="card-actions">
          <span class="badge ${badge}">${esc(j.status)}</span>
          ${['queued', 'downloading'].includes(j.status)
            ? `<button class="sm danger" data-cancel="${j.id}">Cancel</button>` : ''}
        </div>
      </div>
      ${j.total ? `<div class="progress">
        <div class="meter ${badge}"><i style="width:${j.percent}%"></i></div>
        <div class="row"><span>${bytes(j.done)} / ${bytes(j.total)} · ${j.percent}%</span>
        <span>${j.speed ? bytes(j.speed) + '/s' : ''} ${
          j.eta_seconds ? '· ' + duration(j.eta_seconds) + ' left' : ''}</span></div>
      </div>` : ''}
    </div>`;
  }).join('');

  if (jobs.some(j => ['queued', 'downloading', 'verifying'].includes(j.status))) {
    state.pollTimer = setTimeout(pollDownloads, 900);
  } else if (jobs.some(j => j.status === 'done')) {
    loadModels();
  }
}

$('#clearDownloads').addEventListener('click', async () => {
  await api('/api/downloads/clear', { method: 'POST' });
  pollDownloads();
});

/* --------------------------------------------------------------- providers */

async function loadProviders() {
  const { providers } = await api('/api/providers');
  state.providers = providers;
  $('#providerList').innerHTML = providers.map(p => `
    <div class="card">
      <div class="card-head">
        <div style="min-width:0">
          <div class="card-title">${esc(p.label)}</div>
          <div class="card-sub">${esc(p.blurb)}</div>
        </div>
        <div class="card-actions">
          <span class="badge ${p.ready ? 'green' : 'grey'}">${p.ready ? 'ready' : 'not ready'}</span>
        </div>
      </div>
      <div class="fit-detail">${esc(p.detail)}${
        p.hint ? `<br><span style="color:var(--dim)">${esc(p.hint)}</span>` : ''}</div>
      <div class="pills"><span class="pill">${esc(p.kind)}</span></div>
    </div>`).join('');
}

async function loadKeys() {
  const { keys, backend } = await api('/api/keys');
  $('#vaultBackend').textContent = backend;
  const labels = {
    openai_api_key: 'OpenAI API key',
    anthropic_api_key: 'Anthropic API key',
    hf_token: 'Hugging Face token (alternative to signing in)',
    brave_search_key: 'Brave Search API key (optional — web search works without it)',
    tavily_key: 'Tavily API key (optional — better research search)',
  };
  $('#keyList').innerHTML = keys.map(k => `
    <div class="card">
      <div class="field">
        <label>${esc(labels[k.name] || k.name)} ${
          k.set ? `<span class="badge green" style="margin-left:.4rem">${esc(k.masked)}</span>` : ''}</label>
        <div class="row-inline">
          <input type="password" data-key="${k.name}" placeholder="${
            k.set ? 'replace, or leave blank to keep' : 'paste key'}">
          <button class="sm" data-savekey="${k.name}">Save</button>
          ${k.set ? `<button class="sm danger" data-delkey="${k.name}">Remove</button>` : ''}
        </div>
      </div>
    </div>`).join('');
}

async function loadOAuth() {
  const { providers } = await api('/api/oauth');
  $('#oauthList').innerHTML = providers.map(p => `
    <div class="card">
      <div class="card-head">
        <div style="min-width:0">
          <div class="card-title">${esc(p.label)}</div>
          <div class="card-sub">${esc(p.blurb)}</div>
        </div>
        <div class="card-actions">
          <span class="badge ${p.signed_in ? (p.expired ? 'amber' : 'green') : 'grey'}">${
            p.signed_in ? (p.expired ? 'token expired' : 'signed in') : 'signed out'}</span>
        </div>
      </div>
      ${p.config_fields.map(f => `
        <div class="field">
          <label>${esc(f.label)}</label>
          <input type="text" data-oauth="${p.key}" data-field="${f.name}"
                 value="${esc(p.config[f.name] || '')}" placeholder="${esc(f.hint)}">
          ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ''}
        </div>`).join('')}
      <div class="field">
        <div class="hint">Redirect URI to register: <span class="pill mono">${esc(p.redirect_uri)}</span></div>
      </div>
      <div class="field row-inline">
        <button class="sm" data-oauthsave="${p.key}">Save settings</button>
        <button class="sm primary" data-oauthstart="${p.key}" ${
          p.missing.length ? 'disabled title="Fill in: ' + esc(p.missing.join(', ')) + '"' : ''}>
          ${p.signed_in ? 'Sign in again' : 'Sign in'}</button>
        ${p.signed_in ? `<button class="sm danger" data-oauthout="${p.key}">Sign out</button>` : ''}
      </div>
    </div>`).join('');
}

$('#providers').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;

  if (btn.dataset.savekey || btn.dataset.delkey) {
    const name = btn.dataset.savekey || btn.dataset.delkey;
    const value = btn.dataset.delkey ? '' : $(`input[data-key="${name}"]`).value.trim();
    if (btn.dataset.savekey && !value) return toast('Nothing to save', true);
    await api('/api/keys', { method: 'POST', body: { name, value } });
    toast(value ? 'Saved and encrypted' : 'Removed');
    loadKeys(); loadProviders(); loadProviderSelect();
  }
  if (btn.dataset.oauthsave) {
    const key = btn.dataset.oauthsave;
    const cfg = {};
    $$(`input[data-oauth="${key}"]`).forEach(i => cfg[i.dataset.field] = i.value.trim());
    await api(`/api/oauth/${key}/config`, { method: 'POST', body: cfg });
    toast('Saved'); loadOAuth();
  }
  if (btn.dataset.oauthstart) {
    try {
      await api(`/api/oauth/${btn.dataset.oauthstart}/start`, { method: 'POST' });
      toast('Opened your browser — come back when you are done, then hit Refresh.');
      setTimeout(loadOAuth, 6000);
    } catch (err) { toast(err.message, true); }
  }
  if (btn.dataset.oauthout) {
    await api(`/api/oauth/${btn.dataset.oauthout}/signout`, { method: 'POST' });
    toast('Signed out'); loadOAuth();
  }
});

/* ------------------------------------------------------------- browser tab */

let brTimer = null;

function brStopLive() {
  if (brTimer) { clearInterval(brTimer); brTimer = null; }
}

/* The view is polled rather than streamed. A page that is merely being read
   does not change, so a frame every second is plenty and costs nothing when
   the tab is not open - which is why the timer is cleared on the way out. */
function brStartLive() {
  brStopLive();
  if (!$('#brLive')?.checked) return;
  brTimer = setInterval(brRefreshView, 1000);
  brRefreshView();
}

async function brRefreshView() {
  if (!$('#browser').classList.contains('active')) return brStopLive();
  const img = $('#brView');
  try {
    const res = await fetch(`/api/browser/view?t=${Date.now()}`);
    if (res.status === 204) {
      img.style.display = 'none';
      $('#brBlank').style.display = '';
      return;
    }
    const blob = await res.blob();
    const next = URL.createObjectURL(blob);
    const previous = img.dataset.blob;
    img.src = next;
    img.style.display = '';
    $('#brBlank').style.display = 'none';
    // Revoke the frame we just replaced, not the one now showing - revoking
    // too early leaves a broken image on slower machines.
    if (previous) URL.revokeObjectURL(previous);
    img.dataset.blob = next;
  } catch { /* a dropped frame is not worth a toast */ }
}

function brRender(state) {
  if (!state || state.ok === false) {
    $('#brMode').textContent = 'not available';
    $('#brMode').className = 'badge red';
    $('#brElements').innerHTML =
      `<div class="empty">${esc(state?.error || 'Browser not available.')}</div>`;
    return;
  }
  $('#brMode').textContent = state.mode_label || state.mode || 'ready';
  $('#brMode').className = `badge ${state.mode === 'chrome' ? 'amber' : 'green'}`;
  $('#brCurrent').textContent = state.title
    ? `${state.title} — ${state.url}` : (state.url || '');
  if (document.activeElement !== $('#brUrl')) $('#brUrl').value = state.url || '';

  const els = state.elements || [];
  $('#brElements').innerHTML = els.length ? `<div class="card">${els.map(e => `
    <div class="srv-tool">
      <span class="pill mono">${e.ref}</span>
      <span class="badge grey">${esc(e.kind)}</span>
      <span class="d" style="min-width:0">${esc(e.label || '(no label)')}${
        e.href ? ` <span class="mono">→ ${esc(e.href.slice(0, 60))}</span>` : ''}</span>
      ${e.kind.startsWith('input') || e.kind === 'textarea'
        ? `<input type="text" data-br-field="${e.ref}" placeholder="type here…"
             style="max-width:12rem">
           <button class="sm" data-br-type="${e.ref}">Type + Enter</button>`
        : `<button class="sm" data-br-click="${e.ref}">Click</button>`}
    </div>`).join('')}</div>`
    : '<div class="empty">No interactive elements found on this page.</div>';

  $('#brText').textContent = (state.text || '')
    + (state.truncated ? '\n…[truncated]' : '');
}

async function loadBrowserTab() {
  try {
    const status = await api('/api/browser');
    if (!status.enabled || !status.available) {
      brRender({ ok: false,
                 error: status.hint || 'Browser use is switched off on the '
                                     + 'Tools & MCP tab.' });
      return;
    }
    $('#brMode').textContent = status.mode_label || 'not started';
    $('#brCurrent').textContent = status.url || '';
    if (status.live) {
      brRender(await api('/api/browser/page'));
      brStartLive();
    }
  } catch (err) { brRender({ ok: false, error: err.message }); }
}

async function brAct(body) {
  try {
    brRender(await api('/api/browser/act', { method: 'POST', body }));
    brStartLive();
  } catch (err) { toast(err.message, true); }
}

$('#brGo')?.addEventListener('click', async () => {
  const url = $('#brUrl').value.trim();
  if (!url) return;
  $('#brGo').disabled = true;
  try {
    brRender(await api('/api/browser/open', { method: 'POST', body: { url } }));
    brStartLive();
  } catch (err) { toast(err.message, true); }
  $('#brGo').disabled = false;
});

$('#brUrl')?.addEventListener('keydown', e => {
  if (e.key === 'Enter') $('#brGo').click();
});

$('#brBack')?.addEventListener('click', () => brAct({ action: 'back' }));
$('#brForward')?.addEventListener('click', () => brAct({ action: 'forward' }));
$('#brReload')?.addEventListener('click', () => brAct({ action: 'reload' }));
$('#brLive')?.addEventListener('change', e => {
  if (e.target.checked) brStartLive(); else brStopLive();
});

$('#browser')?.addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.dataset.brClick) {
    brAct({ action: 'click', ref: Number(btn.dataset.brClick) });
  }
  if (btn.dataset.brType) {
    const ref = Number(btn.dataset.brType);
    const field = $(`[data-br-field="${ref}"]`);
    brAct({ action: 'type', ref, text: field ? field.value : '', submit: true });
  }
});

/* ------------------------------------------------- routes & custom providers */

async function loadRoutes() {
  const data = await api('/api/routes');
  state.routes = data.routes;
  const direct = (state.providers || []).filter(p => p.kind !== 'route');

  $('#routeList').innerHTML = data.routes.length ? data.routes.map(r => {
    const rows = r.candidates.map((c, i) => {
      const resting = r.resting.find(x => x.provider === c.provider && x.model === c.model);
      const health = data.health[`${c.provider}::${c.model}`] || {};
      const excluded = r.excluded.find(x => x.provider === c.provider && x.model === c.model);
      const spent = (r.over_budget || []).find(
        x => x.provider === c.provider && x.model === c.model);
      let badge = '<span class="badge green">ready</span>';
      if (excluded) badge = `<span class="badge grey">${esc(excluded.why)}</span>`;
      else if (resting) badge = `<span class="badge amber">${
        esc(resting.health.last_kind || 'resting')} · ${resting.health.rest_remaining}s</span>`;
      // Out of allowance is not a failure, so it gets its own colour: nothing
      // is broken and there is nothing to investigate.
      else if (spent) badge = `<span class="badge amber">${
        esc(spent.verdict.why)} · ${duration(spent.verdict.retry_in)}</span>`;
      const paidish = excluded && excluded.why.includes('free-only');
      return `<div class="srv-tool">
        <span class="pill mono">${i + 1}</span>
        ${badge}
        <code>${esc(c.provider)}/${esc(c.model)}</code>
        <span class="d">${health.tps ? `~${health.tps} tok/s` : ''}${
          health.successes ? ` · ${health.successes} ok` : ''}${
          health.failures ? ` · ${health.failures} failed` : ''}</span>
        ${paidish ? `<button class="sm" data-free-tag="${
          esc(c.provider)}|${esc(c.model)}" title="Tell AEGIS this model is free"
          >mark free</button>` : ''}
        <button class="sm ghost" data-cand-up="${esc(r.key)}|${i}">↑</button>
        <button class="sm danger" data-cand-del="${esc(r.key)}|${i}">×</button>
      </div>`;
    }).join('');

    return `<div class="card">
      <div class="card-head">
        <div style="min-width:0">
          <div class="card-title">${esc(r.label)}
            ${r.allow_paid ? '' : '<span class="badge green">free only</span>'}</div>
          <div class="card-sub">${esc(r.description || '')}</div>
        </div>
        <div class="card-actions">
          <span class="badge ${r.ready ? 'green' : 'red'}">${
            r.ready ? `next: ${esc(r.next.model)}` : 'none available'}</span>
          <button class="sm danger" data-route-del="${esc(r.key)}">Delete</button>
        </div>
      </div>
      <div class="route-opts">
        <label class="check"><input type="checkbox" data-route-smart="${esc(r.key)}"
          ${r.smart !== false ? 'checked' : ''}> Smart — pick per request (code, images, long, quick)</label>
        <label>When spent, continue with
          <select data-route-then="${esc(r.key)}">
            <option value="">nothing — stop and say so</option>
            ${data.routes.filter(x => x.key !== r.key).map(x =>
              `<option value="${esc(x.key)}" ${r.then_route === x.key ? 'selected' : ''}>${
                esc(x.label)}${x.allow_paid ? ' (may cost money)' : ''}</option>`).join('')}
          </select></label>
      </div>
      ${rows || '<div class="fit-detail">No models yet — add one below.</div>'}
      <div class="field row-inline" style="margin-top:.6rem">
        <select data-cand-provider="${esc(r.key)}" style="max-width:11rem">
          ${direct.map(p => `<option value="${p.key}">${esc(p.label)}</option>`).join('')}
        </select>
        <input type="text" data-cand-model="${esc(r.key)}"
               placeholder="model id, e.g. qwen/qwen3-8b:free">
        <button class="sm primary" data-cand-add="${esc(r.key)}">Add model</button>
      </div>
    </div>`;
  }).join('') : '<div class="empty">No routes yet.</div>';

  const resting = Object.values(data.health).filter(h => h.resting).length;
  if (resting) {
    $('#routeList').insertAdjacentHTML('beforeend',
      `<div class="card"><div class="card-head">
        <div class="card-sub">${resting} model(s) resting after a failure.</div>
        <div class="card-actions">
          <button class="sm" id="reviveHealth">Try them all again now</button>
        </div></div></div>`);
    $('#reviveHealth').addEventListener('click', async () => {
      const res = await api('/api/routes/health',
                            { method: 'POST', body: { action: 'revive' } });
      toast(`Lifted ${res.revived} cooldown(s)`);
      loadRoutes(); loadRouteSelect();
    });
  }
}

/* ---------------------------------------------------------------- budgets */

function budgetBar(used, limit) {
  if (!limit) return '';
  const pct = Math.min(100, Math.round((used / limit) * 100));
  const tone = pct >= 100 ? 'red' : pct >= 80 ? 'amber' : 'green';
  return `<span class="badge ${tone}">${used.toLocaleString()} / ${
    limit.toLocaleString()} (${pct}%)</span>`;
}

async function loadBudgets() {
  const data = await api('/api/routes/budget');
  const rows = Object.entries(data.usage || {});

  const set = Object.entries(data.limits || {}).map(([key, lim]) => {
    const parts = ['rpm', 'rpd', 'tpd']
      .filter(f => lim[f]).map(f => `${lim[f].toLocaleString()} ${data.fields[f]}`);
    return `<div class="srv-tool">
      <span class="badge grey">yours</span>
      <code>${esc(key)}</code>
      <span class="d">${esc(parts.join(' · ')) || 'no limit'}</span>
      <button class="sm danger" data-bud-clear="${esc(key)}">×</button>
    </div>`;
  }).join('');

  const spend = rows.map(([key, u]) => {
    const lim = u.limits || {};
    const held = u.verdict && !u.verdict.ok;
    return `<div class="srv-tool">
      <span class="badge ${held ? 'amber' : 'green'}">${
        held ? esc(u.verdict.why) : 'in budget'}</span>
      <code>${esc(key)}</code>
      <span class="d">${u.requests} today${
        u.tokens ? ` · ${u.tokens.toLocaleString()} tokens` : ''}${
        u.per_minute ? ` · ${u.per_minute} this minute` : ''}</span>
      ${budgetBar(u.requests, lim.rpd)}
      ${budgetBar(u.tokens, lim.tpd)}
      ${lim.source === 'inferred'
        ? '<span class="badge grey" title="Read from the provider\'s own rate-limit headers">from the provider</span>'
        : ''}
      <button class="sm ghost" data-bud-reset="${esc(key)}"
        title="Set today's count back to zero">reset</button>
    </div>`;
  }).join('');

  $('#budgetList').innerHTML = (set || spend)
    ? `<div class="card">${set || ''}${spend || ''}</div>`
    : `<div class="empty">Nothing counted yet. Set an allowance above, or just
       start using a route — providers that report what's left are listened to
       on their own.</div>`;
}

async function saveRouteCandidates(key, candidates) {
  const route = state.routes.find(r => r.key === key);
  await api('/api/routes', { method: 'POST', body: {
    key, label: route.label, description: route.description,
    allow_paid: route.allow_paid, smart: route.smart !== false,
    then_route: route.then_route || '', candidates } });
  await loadRoutes();
  await loadRouteSelect();
}

async function loadCustomProviders() {
  const data = await api('/api/custom-providers');
  state.presets = data.presets;

  $('#presetPick').innerHTML = '<option value="">— choose —</option>'
    + data.presets.map(p => `<option value="${p.key}">${esc(p.label)}</option>`).join('');

  $('#customProviderList').innerHTML = data.providers.length
    ? data.providers.map(p => `
      <div class="card">
        <div class="card-head">
          <div style="min-width:0">
            <div class="card-title">${esc(p.label)} <span class="pill">${esc(p.key)}</span>
              ${p.free_tier ? '<span class="badge green">free plan</span>' : ''}
              ${p.key_count > 1 ? `<span class="badge grey">${p.key_count} keys, rotating</span>` : ''}</div>
            <div class="card-sub mono">${esc(p.base_url)}</div>
          </div>
          <div class="card-actions">
            <span class="badge ${p.has_key ? 'green' : 'grey'}">${
              p.has_key ? esc(p.masked) : 'no key'}</span>
            <button class="sm" data-cp-models="${esc(p.key)}">List models</button>
            <button class="sm danger" data-cp-del="${esc(p.key)}">Remove</button>
          </div>
        </div>
        <div class="srv-tools" data-cp-list="${esc(p.key)}"></div>
      </div>`).join('')
    : '<div class="empty">None added yet.</div>';
}

/* Add a model straight from a provider's catalogue into a route, rather than
   retyping ids like "openai/gpt-5.6-sol" by hand. */
$('#providers').addEventListener('change', async e => {
  const sel = e.target.closest('[data-add-model]');
  if (!sel || !sel.value) return;
  const [provider, model] = sel.dataset.addModel.split('|');
  const routeKey = sel.value;
  sel.value = '';
  const route = (state.routes || []).find(r => r.key === routeKey);
  if (!route) return;
  if (route.candidates.some(c => c.provider === provider && c.model === model)) {
    return toast('Already in that route');
  }
  await saveRouteCandidates(routeKey, [...route.candidates, { provider, model }]);
  const added = (state.routes.find(r => r.key === routeKey) || {}).candidates || [];
  const kept = added.some(c => c.provider === provider && c.model === model);
  toast(kept
    ? `Added ${model} to ${route.label}`
    : `${route.label} is free-only and ${model} is not marked free — not added`,
    !kept);
});

$('#presetPick').addEventListener('change', e => {
  const preset = (state.presets || []).find(p => p.key === e.target.value);
  if (!preset) { $('#presetNote').textContent = ''; return; }
  $('#cpKey').value = preset.key === 'custom' ? '' : preset.key;
  $('#cpLabel').value = preset.label === 'Something else' ? '' : preset.label;
  $('#cpUrl').value = preset.base_url;
  $('#cpModels').value = preset.models || '';
  $('#cpNoModels').checked = preset.has_models === 'no';
  $('#cpFreeTier').checked = preset.free_tier === 'yes';
  $('#presetNote').textContent = preset.note;
});

$('#cpSave').addEventListener('click', async () => {
  try {
    await api('/api/custom-providers', { method: 'POST', body: {
      key: $('#cpKey').value, label: $('#cpLabel').value,
      base_url: $('#cpUrl').value, api_key: $('#cpKeyValue').value,
      ...($('#cpExtraKeys').value.trim() ? { extra_keys: $('#cpExtraKeys').value } : {}),
      models: $('#cpModels').value,
      has_models_endpoint: !$('#cpNoModels').checked,
      free_tier: $('#cpFreeTier').checked,
      note: $('#presetNote').textContent } });
    ['#cpKey', '#cpLabel', '#cpUrl', '#cpKeyValue', '#cpModels', '#cpExtraKeys']
      .forEach(s => $(s).value = '');
    $('#cpNoModels').checked = false;
    $('#cpFreeTier').checked = false;
    toast('Provider saved');
    loadCustomProviders(); loadProviders(); loadProviderSelect();
  } catch (err) { toast(err.message, true); }
});

$('#autobuildRoute').addEventListener('click', async () => {
  const btn = $('#autobuildRoute');
  btn.disabled = true;
  btn.textContent = 'Asking each provider what it has…';
  try {
    const res = await api('/api/routes/autobuild',
                          { method: 'POST', body: { key: 'auto', per_provider: 6 } });
    toast(`Auto route rebuilt: ${res.cloud} hosted, ${res.local} local`);
    if (res.notes && res.notes.length) {
      $('#autobuildNote').textContent = res.notes.join(' · ');
    }
    loadRoutes(); loadRouteSelect();
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
  btn.textContent = 'Rebuild my free Auto route';
});

$('#budSave').addEventListener('click', async () => {
  const provider = $('#budProvider').value.trim();
  const model = $('#budModel').value.trim() || '*';
  if (!provider) return toast('Which provider?', true);
  try {
    await api('/api/routes/budget', { method: 'POST', body: {
      provider, model,
      rpm: Number($('#budRpm').value) || 0,
      rpd: Number($('#budRpd').value) || 0,
      tpd: Number($('#budTpd').value) || 0 } });
    ['#budProvider', '#budModel', '#budRpm', '#budRpd', '#budTpd']
      .forEach(s => $(s).value = '');
    toast(`Allowance saved for ${provider}/${model}`);
    loadBudgets(); loadRoutes();
  } catch (err) { toast(err.message, true); }
});

$('#addRoute').addEventListener('click', async () => {
  const key = $('#routeKey').value.trim();
  if (!key) return toast('Give it a short key', true);
  await api('/api/routes', { method: 'POST', body: {
    key, label: $('#routeLabel').value.trim() || key,
    allow_paid: !$('#routeFree').checked, candidates: [] } });
  $('#routeKey').value = ''; $('#routeLabel').value = ''; $('#routeFree').checked = false;
  toast('Route created');
  loadRoutes(); loadRouteSelect();
});

$('#providers').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;

  if (btn.dataset.routeDel) {
    if (!confirm(`Delete the route "${btn.dataset.routeDel}"?`)) return;
    await api(`/api/routes/${btn.dataset.routeDel}`, { method: 'DELETE' });
    loadRoutes(); loadRouteSelect();
  }
  if (btn.dataset.candAdd) {
    const key = btn.dataset.candAdd;
    const provider = $(`[data-cand-provider="${key}"]`).value;
    const model = $(`[data-cand-model="${key}"]`).value.trim();
    if (!model) return toast('Enter a model id', true);
    const route = state.routes.find(r => r.key === key);
    await saveRouteCandidates(key, [...route.candidates, { provider, model }]);
    toast('Added');
  }
  if (btn.dataset.candDel) {
    const [key, idx] = btn.dataset.candDel.split('|');
    const route = state.routes.find(r => r.key === key);
    const next = route.candidates.filter((_, i) => i !== Number(idx));
    await saveRouteCandidates(key, next);
  }
  if (btn.dataset.candUp) {
    const [key, idx] = btn.dataset.candUp.split('|');
    const i = Number(idx);
    if (i === 0) return;
    const route = state.routes.find(r => r.key === key);
    const next = [...route.candidates];
    [next[i - 1], next[i]] = [next[i], next[i - 1]];
    await saveRouteCandidates(key, next);
  }
  if (btn.dataset.freeTag) {
    const [provider, model] = btn.dataset.freeTag.split('|');
    await api('/api/routes/free-tag', { method: 'POST',
                                        body: { provider, model, free: true } });
    toast(`${model} marked free — it can now run on a free-only route`);
    loadRoutes(); loadRouteSelect();
  }
  if (btn.dataset.toggleFree) {
    const [provider, model, makeFree] = btn.dataset.toggleFree.split('|');
    const res = await api('/api/routes/free-tag', { method: 'POST',
      body: { provider, model, free: makeFree === '1' } });
    btn.textContent = res.free ? 'free' : 'paid';
    btn.dataset.toggleFree = `${provider}|${model}|${res.free ? '0' : '1'}`;
    btn.title = res.free ? 'Mark as paid' : 'Mark as free';
    loadRoutes();
  }
  if (btn.dataset.budReset) {
    const [provider, model] = btn.dataset.budReset.split('::');
    await api('/api/routes/budget', { method: 'POST',
      body: { action: 'reset', provider, model } });
    toast(`Today's count cleared for ${model}`);
    loadBudgets(); loadRoutes();
  }
  if (btn.dataset.budClear) {
    const [provider, model] = btn.dataset.budClear.split('::');
    await api('/api/routes/budget', { method: 'POST',
      body: { provider, model, rpm: 0, rpd: 0, tpd: 0 } });
    toast('Allowance removed');
    loadBudgets(); loadRoutes();
  }
  if (btn.dataset.cpDel) {
    if (!confirm(`Remove "${btn.dataset.cpDel}" and its saved key?`)) return;
    await api(`/api/custom-providers/${btn.dataset.cpDel}`, { method: 'DELETE' });
    toast('Removed');
    loadCustomProviders(); loadProviders(); loadProviderSelect();
  }
  if (btn.dataset.cpModels) {
    const key = btn.dataset.cpModels;
    const panel = $(`[data-cp-list="${key}"]`);
    if (panel.classList.contains('open')) {
      panel.classList.remove('open'); btn.textContent = 'List models'; return;
    }
    btn.textContent = 'Loading…';
    const res = await api(`/api/custom-providers/${key}/models`);
    btn.textContent = res.ok ? 'Hide models' : 'List models';
    if (!res.ok) return toast(res.error, true);
    const routeOpts = (state.routes || []).map(r =>
      `<option value="${esc(r.key)}">→ ${esc(r.label)}</option>`).join('');
    panel.innerHTML = `<div class="pills" style="margin:.4rem 0">
        <span class="pill">${res.count} models</span>
        <span class="pill">${res.free} marked free</span>
        ${res.priced ? '<span class="pill">prices published</span>'
                     : '<span class="pill">no prices published</span>'}</div>
      ${res.note ? `<div class="fit-detail">${esc(res.note)}</div>` : ''}`
      + res.models.slice(0, 150).map(m => `<div class="srv-tool">
          <button class="sm" data-toggle-free="${esc(key)}|${esc(m.id)}|${
            m.free ? '0' : '1'}" title="${m.free ? 'Mark as paid' : 'Mark as free'}"
            >${m.free ? 'free' : 'paid'}</button>
          <code>${esc(m.id)}</code>
          <span class="d">${m.context ? m.context.toLocaleString() + ' ctx' : ''}${
            m.price_per_token != null ? ` · ${m.price_per_token}/tok` : ''}</span>
          ${routeOpts ? `<select data-add-model="${esc(key)}|${esc(m.id)}"
             style="max-width:11rem"><option value="">add to route…</option>
             ${routeOpts}</select>` : ''}
        </div>`).join('');
    panel.classList.add('open');
    toast(res.priced
      ? `${res.free} of ${res.count} models are free on ${key}`
      : `${res.count} models on ${key} — no prices published, mark your free ones`);
  }
});

/* --------------------------------------------------- skills, agents, memory */

async function loadBrain() {
  const [sk, ag, vault] = await Promise.all([
    api('/api/skills'), api('/api/agents'), api('/api/vault'),
  ]);

  // Pending skills first — these are the ones the agent wrote itself.
  $('#pendingSkills').innerHTML = sk.pending.length ? `
    <div class="note"><b>${sk.pending.length} skill${
      sk.pending.length > 1 ? 's' : ''} the agent wrote and is waiting on.</b>
      Nothing here is loaded into any prompt until you approve it.</div>
    ${sk.pending.map(p => `
      <div class="card">
        <div class="card-head">
          <div style="min-width:0">
            <div class="card-title">${esc(p.name)}</div>
            <div class="card-sub">${esc(p.description)}</div>
          </div>
          <div class="card-actions">
            <span class="badge amber">pending</span>
            <button class="sm primary" data-approve-skill="${esc(p.name)}">Approve</button>
            <button class="sm danger" data-reject-skill="${esc(p.name)}">Reject</button>
          </div>
        </div>
        <div class="aargs" style="max-height:22rem">${esc(p.body)}</div>
      </div>`).join('')}` : '';

  $('#skillList').innerHTML = `
    <div class="pills" style="margin-bottom:.6rem">
      <span class="pill">${sk.counts.total} loadable</span>
      <span class="pill">${sk.counts.bundled} bundled</span>
      <span class="pill">${sk.counts.user} yours</span>
      ${sk.counts.pending ? `<span class="pill">${sk.counts.pending} pending</span>` : ''}
    </div>
    ${sk.skills.map(s => `
      <div class="card">
        <div class="card-head">
          <div style="min-width:0">
            <div class="card-title">${esc(s.name)}</div>
            <div class="card-sub">${esc(s.description)}</div>
          </div>
          <div class="card-actions">
            <span class="pill">${esc(s.origin)}</span>
            <button class="sm ghost" data-view-skill="${esc(s.name)}">View</button>
            ${s.origin === 'user'
              ? `<button class="sm danger" data-del-skill="${esc(s.name)}">Delete</button>`
              : ''}
          </div>
        </div>
        <div class="srv-tools" data-skill-body="${esc(s.name)}"></div>
      </div>`).join('')}`;

  $('#agentBox').innerHTML = `
    <div class="card-head">
      <div style="min-width:0">
        <div class="card-title">${ag.roles.length} roles available</div>
        <div class="card-sub">Base skill: ${esc(ag.base_skill)} ·
          ${ag.max_steps} step cap · nesting ${ag.nesting ? 'on' : 'off'}</div>
      </div>
      <div class="card-actions">
        <label class="switch"><input type="checkbox" id="agToggle"
          ${ag.enabled ? 'checked' : ''}> ${ag.enabled ? 'On' : 'Off'}</label>
      </div>
    </div>
    <div class="pills">${ag.roles.map(r =>
      `<span class="pill">${esc(r.name)}</span>`).join('')}</div>`;

  $('#agToggle')?.addEventListener('change', async e => {
    await api('/api/agents', { method: 'POST', body: { enabled: e.target.checked } });
    loadBrain();
  });

  $('#vaultPath').value = vault.path || '';
  renderVaultStats(vault);
  loadMemory();
}

function renderVaultStats(v) {
  const when = v.indexed_at ? relTime(v.indexed_at) : 'never';
  $('#vaultStats').innerHTML = v.configured
    ? `${v.exists ? '' : '<span style="color:var(--red)">Folder not found. </span>'}`
      + `${v.notes} notes indexed (${bytes(v.bytes)}) · last indexed ${when} · `
      + `search: ${v.search} · writes to vault: never`
    : 'No vault set. Point this at your Obsidian folder to let the agent search your notes.';
}

async function loadMemory() {
  const q = $('#memorySearch').value.trim();
  const data = await api('/api/memory' + (q ? `?q=${encodeURIComponent(q)}` : ''));
  $('#memoryList').innerHTML = data.facts.length ? data.facts.map(f => `
    <div class="card">
      <div class="card-head">
        <div style="min-width:0">
          <div class="card-sub" style="color:var(--text)">${esc(f.text)}</div>
          <div class="pills">
            ${f.tags ? `<span class="pill">${esc(f.tags)}</span>` : ''}
            <span class="pill">${esc(f.source)}</span>
            <span class="pill">${relTime(f.updated)}</span>
          </div>
        </div>
        <div class="card-actions">
          <button class="sm danger" data-forget="${f.id}">Forget</button>
        </div>
      </div>
    </div>`).join('')
    : `<div class="empty">${q ? 'Nothing matched.' : 'Nothing saved yet.'}</div>`;
}

$('#brain').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;

  if (btn.dataset.approveSkill) {
    await api(`/api/skills/${encodeURIComponent(btn.dataset.approveSkill)}/approve`,
              { method: 'POST' });
    toast(`Approved — ${btn.dataset.approveSkill} is now live`);
    loadBrain();
  }
  if (btn.dataset.rejectSkill) {
    await api(`/api/skills/${encodeURIComponent(btn.dataset.rejectSkill)}/reject`,
              { method: 'POST' });
    toast('Rejected and deleted');
    loadBrain();
  }
  if (btn.dataset.delSkill) {
    if (!confirm(`Delete the skill "${btn.dataset.delSkill}"?`)) return;
    const res = await api(`/api/skills/${encodeURIComponent(btn.dataset.delSkill)}`,
                          { method: 'DELETE' });
    toast(res.ok ? 'Deleted' : res.error, !res.ok);
    loadBrain();
  }
  if (btn.dataset.viewSkill) {
    const panel = $(`[data-skill-body="${btn.dataset.viewSkill}"]`);
    if (panel.classList.contains('open')) {
      panel.classList.remove('open'); btn.textContent = 'View'; return;
    }
    const res = await api(`/api/skills/${encodeURIComponent(btn.dataset.viewSkill)}`);
    panel.innerHTML = `<div class="aargs" style="max-height:26rem">${
      esc(res.skill.body)}</div>`;
    panel.classList.add('open');
    btn.textContent = 'Hide';
  }
  if (btn.dataset.forget) {
    await api(`/api/memory/${btn.dataset.forget}`, { method: 'DELETE' });
    loadMemory();
  }
});

$('#saveVault').addEventListener('click', async () => {
  const v = await api('/api/vault', { method: 'POST',
                                      body: { path: $('#vaultPath').value } });
  renderVaultStats(v);
  toast(v.exists ? 'Saved. Hit Reindex to build the index.'
                 : 'Saved, but that folder does not exist.', !v.exists);
});

$('#reindexVault').addEventListener('click', async () => {
  $('#vaultStats').innerHTML = '<span class="spinner"></span> indexing…';
  try {
    const res = await api('/api/vault/reindex', { method: 'POST', body: {} });
    if (!res.ok) return toast(res.error, true);
    toast(`Indexed ${res.total} notes in ${res.seconds}s`);
    renderVaultStats(res.stats);
  } catch (err) { toast(err.message, true); }
});

$('#memoryRefresh').addEventListener('click', loadMemory);
let memTimer;
$('#memorySearch').addEventListener('input', () => {
  clearTimeout(memTimer);
  memTimer = setTimeout(loadMemory, 220);
});
$('#memoryAdd').addEventListener('click', async () => {
  const text = $('#memoryNew').value.trim();
  if (!text) return;
  await api('/api/memory', { method: 'POST',
                             body: { text, tags: $('#memoryTags').value.trim() } });
  $('#memoryNew').value = ''; $('#memoryTags').value = '';
  loadMemory();
});

/* ------------------------------------------------------- files & tasks */

async function loadFilesTab() {
  const data = await api('/api/files');
  $('#folderList').innerHTML = data.folders.length
    ? data.folders.map(f => `
      <div class="card"><div class="card-head">
        <div style="min-width:0"><div class="card-sub mono">${esc(f)}</div></div>
        <div class="card-actions">
          <button class="sm danger" data-drop-folder="${esc(f)}">Disconnect</button>
        </div></div></div>`).join('')
    : `<div class="empty">No folders connected — the agent has no filesystem
        access at all right now.</div>`;

  $('#attachStats').innerHTML = `
    <div class="pills">
      <span class="pill">${data.attachments} files</span>
      <span class="pill">${bytes(data.bytes)}</span>
      <span class="pill">inline under ${(data.inline_limit / 1000).toFixed(0)}k chars</span>
      <span class="pill">search: ${esc(data.search)}</span>
      <span class="pill">${data.tool_count} file tools</span>
    </div>`;
}

async function loadTasks() {
  const [data, providersData] = await Promise.all([
    api('/api/tasks'), api('/api/providers'),
  ]);

  const sel = $('#taskProvider');
  if (!sel.options.length) {
    sel.innerHTML = '<option value="">Default route / provider</option>'
      + providersData.providers.map(p =>
          `<option value="${p.key}">${esc(p.label)}</option>`).join('');
  }

  $('#taskList').innerHTML = data.tasks.length ? data.tasks.map(t => `
    <div class="card">
      <div class="card-head">
        <div style="min-width:0">
          <div class="card-title">${esc(t.name)}
            ${t.enabled ? '' : '<span class="badge grey">paused</span>'}
            ${t.windows_task ? '<span class="badge green">runs when closed</span>'
                             : '<span class="badge amber">only while AEGIS is open</span>'}
            ${t.trusted ? '<span class="badge amber">trusted</span>' : ''}</div>
          <div class="card-sub">${esc(t.schedule_text)} · next ${esc(t.next_run_text)}</div>
        </div>
        <div class="card-actions">
          <button class="sm primary" data-run-task="${esc(t.key)}">Run now</button>
          <button class="sm" data-toggle-task="${esc(t.key)}|${t.enabled ? '0' : '1'}"
            >${t.enabled ? 'Pause' : 'Resume'}</button>
          <button class="sm" data-win-task="${esc(t.key)}|${t.windows_task ? '0' : '1'}"
            >${t.windows_task ? 'Unregister' : 'Register'}</button>
          <button class="sm danger" data-del-task="${esc(t.key)}">Delete</button>
        </div>
      </div>
      <div class="aargs">${esc(t.prompt)}</div>
      <div class="pills">
        <span class="pill mono">${esc(t.cron)}</span>
        ${t.provider ? `<span class="pill">${esc(t.provider)}</span>` : ''}
        <span class="pill">${t.runs} run${t.runs === 1 ? '' : 's'}</span>
        ${t.last_status ? `<span class="pill">${esc(t.last_status)}</span>` : ''}
      </div>
      ${t.last_chat_id ? `<button class="sm ghost" data-open-task-chat="${
        esc(t.last_chat_id)}" style="margin-top:.5rem">Open last result</button>` : ''}
      ${t.last_file ? `<div class="fit-detail mono">${esc(t.last_file)}</div>` : ''}
    </div>`).join('')
    : '<div class="empty">No scheduled tasks yet.</div>';

  if (!data.windows) {
    $('#taskWindows').checked = false;
    $('#taskWindows').disabled = true;
  }
}

async function previewCron() {
  const cron = $('#taskCron').value.trim();
  if (!cron) return;
  try {
    const res = await api(`/api/tasks/_/preview?cron=${encodeURIComponent(cron)}`);
    $('#cronPreview').textContent = res.ok
      ? `${res.text} — next: ${res.next.slice(0, 3).join(' · ')}`
      : res.error;
  } catch { /* preview is cosmetic */ }
}

$('#taskCron').addEventListener('input', () => {
  clearTimeout(state.cronTimer);
  state.cronTimer = setTimeout(previewCron, 250);
});
$('#taskPreset').addEventListener('change', e => {
  if (e.target.value) { $('#taskCron').value = e.target.value; previewCron(); }
});

$('#addFolder').addEventListener('click', async () => {
  const path = $('#folderPath').value.trim();
  if (!path) return;
  try {
    await api('/api/files/folders', { method: 'POST', body: { path } });
    $('#folderPath').value = '';
    toast('Connected');
    loadFilesTab();
  } catch (err) { toast(err.message, true); }
});

$('#addTask').addEventListener('click', async () => {
  const body = {
    name: $('#taskName').value.trim(),
    prompt: $('#taskPrompt').value.trim(),
    cron: $('#taskCron').value.trim(),
    provider: $('#taskProvider').value,
    output_dir: $('#taskOutput').value.trim(),
    trusted: $('#taskTrusted').checked,
    agent: $('#taskAgent') ? $('#taskAgent').value : '',
    project: $('#taskProject') ? $('#taskProject').value : '',
  };
  if (!body.name || !body.prompt) return toast('Name and prompt are required', true);
  try {
    const res = await api('/api/tasks', { method: 'POST', body });
    if ($('#taskWindows').checked && !$('#taskWindows').disabled) {
      const reg = await api(`/api/tasks/${res.key}/windows`,
                            { method: 'POST', body: {} }).catch(e => ({ error: e.message }));
      toast(reg.error ? `Created, but Windows registration failed: ${reg.error}`
                      : 'Created and registered with Windows', Boolean(reg.error));
    } else {
      toast('Task created');
    }
    ['#taskName', '#taskPrompt', '#taskOutput'].forEach(s => $(s).value = '');
    loadTasks();
  } catch (err) { toast(err.message, true); }
});

$('#filetasks').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;

  if (btn.dataset.dropFolder) {
    await api('/api/files/folders?path=' + encodeURIComponent(btn.dataset.dropFolder),
              { method: 'DELETE' });
    toast('Disconnected');
    loadFilesTab();
  }
  if (btn.dataset.runTask) {
    btn.disabled = true; btn.textContent = 'Running…';
    try {
      const res = await api(`/api/tasks/${btn.dataset.runTask}/run`, { method: 'POST' });
      toast(`Done in ${res.seconds}s — ${res.chars} characters written`);
    } catch (err) { toast(err.message, true); }
    loadTasks();
  }
  if (btn.dataset.toggleTask) {
    const [key, on] = btn.dataset.toggleTask.split('|');
    await api('/api/tasks', { method: 'POST', body: { key, enabled: on === '1' } });
    loadTasks();
  }
  if (btn.dataset.winTask) {
    const [key, register] = btn.dataset.winTask.split('|');
    try {
      const res = await api(`/api/tasks/${key}/windows`, { method: 'POST',
        body: register === '1' ? {} : { remove: true } });
      toast(register === '1'
        ? `Windows will run this ${res.schedule}` : 'Removed from Windows');
    } catch (err) { toast(err.message, true); }
    loadTasks();
  }
  if (btn.dataset.delTask) {
    if (!confirm(`Delete "${btn.dataset.delTask}" and its Windows entry?`)) return;
    await api(`/api/tasks/${btn.dataset.delTask}`, { method: 'DELETE' });
    toast('Deleted');
    loadTasks();
  }
  if (btn.dataset.openTaskChat) {
    $$('nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === 'chat'));
    $$('.pane').forEach(p => p.classList.toggle('active', p.id === 'chat'));
    openChat(btn.dataset.openTaskChat);
  }
});

/* ---------------------------------------------------------------- settings */

async function loadSettings() {
  const data = await api('/api/settings');
  const s = data.settings;
  $('#fitContext').value = String(s.fit_context ?? 4096);
  $('#ramHeadroom').value = s.ram_headroom_mb ?? 2048;
  $('#bandwidth').value = s.mem_bandwidth_gbs ?? 45;
  $('#modelsDir').textContent = data.models_dir;
  $('#version').textContent = 'v' + data.version;
}

$('#saveSettings').addEventListener('click', async () => {
  await api('/api/settings', { method: 'POST', body: {
    fit_context: Number($('#fitContext').value),
    ram_headroom_mb: Number($('#ramHeadroom').value),
    mem_bandwidth_gbs: Number($('#bandwidth').value),
  }});
  state.fitCache.clear();
  toast('Saved. Model verdicts will use the new numbers.');
  if ($('#modelList').children.length) loadModels();
});

async function loadRunner() {
  const r = await api('/api/runner');
  $('#runnerBox').innerHTML = `
    <div class="card-head">
      <div style="min-width:0">
        <div class="card-title">${r.available ? 'llama-server found' : 'llama-server not installed'}</div>
        <div class="card-sub">${esc(r.binary || r.hint)}</div>
      </div>
      <div class="card-actions">
        <span class="badge ${r.running ? 'green' : 'grey'}">${r.running ? 'running' : 'stopped'}</span>
        ${r.running ? '<button class="sm danger" id="stopRunner">Stop</button>' : ''}
      </div>
    </div>
    ${r.running ? `<div class="pills">
      <span class="pill">${esc(r.model)}</span>
      <span class="pill mono">port ${r.port}</span>
      <span class="pill">${duration(Math.round(r.uptime))} uptime</span></div>` : ''}`;
  $('#stopRunner')?.addEventListener('click', async () => {
    await api('/api/runner/stop', { method: 'POST' });
    loadRunner(); loadProviderSelect();
  });
}

/* ------------------------------------------------------------ tools & MCP */

async function loadToolsTab() {
  const [tools, servers, comp] = await Promise.all([
    api('/api/tools'), api('/api/mcp/servers'), api('/api/computer'),
  ]);

  $('#toolPolicy').value = tools.policy;
  const decisions = Object.entries(tools.decisions || {});
  $('#decisionCount').textContent = `${decisions.length} remembered`;
  $('#toolCount').textContent = tools.total;

  $('#computerBox').innerHTML = `
    <div class="card-head">
      <div style="min-width:0">
        <div class="card-title">${comp.available ? 'Available on this machine'
                                                 : 'Not available'}</div>
        <div class="card-sub">${esc(comp.detail)}${
          comp.hint ? ' — ' + esc(comp.hint) : ''}</div>
      </div>
      <div class="card-actions">
        <label class="switch"><input type="checkbox" id="compToggle"
          ${comp.enabled ? 'checked' : ''} ${comp.available ? '' : 'disabled'}>
          ${comp.enabled ? 'On' : 'Off'}</label>
      </div>
    </div>
    ${comp.screen.width ? `<div class="pills">
      <span class="pill">${comp.screen.width}×${comp.screen.height}</span>
      <span class="pill">${comp.tool_count} tools</span></div>` : ''}
    <div class="fit-detail">${esc(comp.note)} Mouse and keyboard actions are
      classified as writes, so they follow your approval policy. Slam the pointer
      into the top-left corner to abort a run.</div>`;

  $('#compToggle')?.addEventListener('change', async e => {
    await api('/api/computer', { method: 'POST', body: { enabled: e.target.checked } });
    loadToolsTab();
  });

  const br = await api('/api/browser');
  $('#browserBox').innerHTML = `
    <div class="card-head">
      <div style="min-width:0">
        <div class="card-title">${br.available ? 'Playwright available'
                                               : 'Playwright not installed'}</div>
        <div class="card-sub">Mode: ${esc(br.mode)}${
          br.url ? ' · ' + esc(br.url) : ''}</div>
      </div>
      <div class="card-actions">
        <label class="switch"><input type="checkbox" id="brToggle"
          ${br.enabled ? 'checked' : ''} ${br.available ? '' : 'disabled'}>
          ${br.enabled ? 'On' : 'Off'}</label>
        ${br.live ? '<button class="sm danger" id="brClose">Close</button>' : ''}
      </div>
    </div>
    <div class="field">
      <label for="brTarget">Which browser agents use</label>
      <select id="brTarget">
        <option value="aegis" ${br.target === 'aegis' ? 'selected' : ''}
          >AEGIS's own Chromium — separate from your Chrome</option>
        <option value="chrome" ${br.target === 'chrome' ? 'selected' : ''}
          >My real Chrome — inherits every session I'm signed into</option>
      </select>
      <div class="hint">${esc(br.hint || '')}
        AEGIS's own browser keeps its own logins in
        <span class="pill mono">${esc(br.profile_dir)}</span>, so a task can
        sign in without touching your Chrome. There is no automatic fallback
        either way — if the one you picked can't start, AEGIS says so rather
        than quietly using the other.
        ${br.target === 'chrome'
          ? ' Your Chrome must be started with --remote-debugging-port=9222.'
          : ''}</div>
    </div>
    <div class="pills"><span class="pill">${br.tool_count} tools</span>
      <span class="pill">${esc(br.mode_label)}</span>
      ${br.target === 'chrome' ? `<span class="pill mono">${esc(br.cdp_url)}</span>` : ''}
    </div>`;

  $('#brToggle')?.addEventListener('change', async e => {
    await api('/api/browser', { method: 'POST',
                                body: { browser_use_enabled: e.target.checked } });
    loadToolsTab();
  });
  $('#brTarget')?.addEventListener('change', async e => {
    await api('/api/browser', { method: 'POST',
                                body: { target: e.target.value } });
    if (e.target.value === 'chrome') {
      toast('Agents will now use your own Chrome. Start it with ' +
            '--remote-debugging-port=9222.');
    }
    loadToolsTab(); loadBrowserTab();
  });
  $('#brClose')?.addEventListener('click', async () => {
    await api('/api/browser/close', { method: 'POST' });
    loadToolsTab();
  });

  $('#mcpServers').innerHTML = servers.servers.length
    ? servers.servers.map(s => serverCard(s)).join('')
    : '<div class="empty">None configured yet. Import one below, or add it by hand.</div>';

  const cat = tools.tools || [];
  $('#toolCatalogue').innerHTML = cat.length ? `
    <div class="card">
      <div class="pills">
        <span class="pill">${tools.total} tools</span>
        <span class="pill">${tools.read} read</span>
        <span class="pill">${tools.write} write</span>
        ${tools.servers.map(s => `<span class="pill">${esc(s)}</span>`).join('')}
      </div>
      ${cat.map(t => `<div class="srv-tool">
        <span class="badge ${t.risk === 'read' ? 'green' : 'amber'}">${t.risk}</span>
        <code>${esc(t.name)}</code>
        <span class="d">${esc(t.description)}</span></div>`).join('')}
    </div>` : '<div class="empty">No servers connected, so no tools yet.</div>';
}

function serverCard(s) {
  const badge = s.connected ? 'green' : (s.error ? 'red' : 'grey');
  const label = s.connected ? 'connected' : (s.error ? 'failed' : 'off');
  return `<div class="card">
    <div class="card-head">
      <div style="min-width:0">
        <div class="card-title">${esc(s.name)}</div>
        <div class="card-sub">${esc(s.summary)}</div>
      </div>
      <div class="card-actions">
        <span class="badge ${badge}">${label}</span>
        <button class="sm ${s.connected ? '' : 'primary'}" data-toggle="${esc(s.name)}"
          data-on="${s.connected ? '0' : '1'}">${s.connected ? 'Disconnect' : 'Connect'}</button>
        <button class="sm danger" data-rmsrv="${esc(s.name)}">Remove</button>
      </div>
    </div>
    <div class="pills">
      <span class="pill">${esc(s.origin)}</span>
      <span class="pill">${esc(s.transport)}</span>
      ${s.server_info?.name ? `<span class="pill">${esc(s.server_info.name)} ${
        esc(s.server_info.version || '')}</span>` : ''}
      ${s.tools.length ? `<span class="pill">${s.tools.length} tools</span>` : ''}
    </div>
    ${s.error ? `<div class="fit-detail" style="color:var(--red)">${esc(s.error)}</div>` : ''}
    ${s.tools.length ? `<button class="sm ghost" data-srvtools="${esc(s.name)}"
        style="margin-top:.5rem">Show tools</button>
      <div class="srv-tools" data-tools-for="${esc(s.name)}">
        ${s.tools.map(t => `<div class="srv-tool">
          <span class="badge ${t.risk === 'read' ? 'green' : 'amber'}">${t.risk}</span>
          <code>${esc(t.raw_name)}</code>
          <span class="d">${esc(t.description)}</span></div>`).join('')}
      </div>` : ''}
  </div>`;
}

async function loadDiscovered() {
  const box = $('#mcpDiscovered');
  box.innerHTML = '<div class="empty"><span class="spinner"></span></div>';
  try {
    const report = await api('/api/mcp/discover');
    const usable = report.servers.filter(s => s.summary);
    const broken = report.servers.filter(s => !s.summary);
    box.innerHTML = (usable.length ? usable.map(s => `
      <div class="card">
        <div class="card-head">
          <label class="check" style="min-width:0">
            <input type="checkbox" data-disc="${esc(s.key)}">
            <span style="min-width:0">
              <span class="card-title">${esc(s.name)}</span>
              <span class="card-sub">${esc(s.summary)}</span>
            </span>
          </label>
          <div class="card-actions"><span class="pill">${esc(s.origin)}</span></div>
        </div>
        <div class="pills">
          <span class="pill">${esc(s.transport)}</span>
          ${s.env_keys.length ? `<span class="pill">env: ${
            s.env_keys.map(esc).join(', ')}</span>` : ''}
          ${s.note ? `<span class="pill">${esc(s.note)}</span>` : ''}
        </div>
      </div>`).join('') : '<div class="empty">Nothing found in the usual config files.</div>')
      + broken.map(s => `<div class="card"><div class="card-sub" style="color:var(--red)">
          ${esc(s.origin)}: ${esc(s.note)}</div></div>`).join('');
  } catch (err) {
    box.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

$('#tools').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;

  if (btn.dataset.toggle) {
    const name = btn.dataset.toggle;
    const on = btn.dataset.on === '1';
    btn.disabled = true;
    btn.textContent = on ? 'Connecting…' : 'Disconnecting…';
    const res = await api(`/api/mcp/servers/${encodeURIComponent(name)}/${
      on ? 'enable' : 'disable'}`, { method: 'POST' });
    if (on) toast(res.ok ? `${name}: ${res.tools} tools` : res.error, !res.ok);
    loadToolsTab();
  }
  if (btn.dataset.rmsrv) {
    await api(`/api/mcp/servers/${encodeURIComponent(btn.dataset.rmsrv)}`,
              { method: 'DELETE' });
    toast('Removed');
    loadToolsTab();
  }
  if (btn.dataset.srvtools) {
    const panel = $(`[data-tools-for="${btn.dataset.srvtools}"]`);
    panel.classList.toggle('open');
    btn.textContent = panel.classList.contains('open') ? 'Hide tools' : 'Show tools';
  }
});

$('#rescanMcp').addEventListener('click', loadDiscovered);

$('#importMcp').addEventListener('click', async () => {
  const keys = $$('input[data-disc]:checked').map(i => i.dataset.disc);
  if (!keys.length) return toast('Tick something to import first', true);
  const res = await api('/api/mcp/import', { method: 'POST', body: { keys } });
  toast(`Imported ${res.imported.length}. Connect them above.`);
  loadToolsTab();
});

$('#toolPolicy').addEventListener('change', async e => {
  await api('/api/tools/policy', { method: 'POST', body: { policy: e.target.value } });
  toast('Approval policy saved');
});

$('#forgetDecisions').addEventListener('click', async () => {
  await api('/api/tools/decisions', { method: 'POST', body: { clear: true } });
  toast('Remembered answers cleared');
  loadToolsTab();
});

$('#addServer').addEventListener('click', async () => {
  const name = $('#addName').value.trim();
  const command = $('#addCommand').value.trim();
  if (!name || !command) return toast('Name and command are both required', true);

  const spec = {};
  if (/^https?:\/\//i.test(command)) {
    spec.url = command;
  } else {
    spec.command = command;
    spec.args = $('#addArgs').value.split('\n').map(s => s.trim()).filter(Boolean);
    const env = {};
    for (const line of $('#addEnv').value.split('\n')) {
      const idx = line.indexOf('=');
      if (idx > 0) env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    }
    if (Object.keys(env).length) spec.env = env;
  }
  try {
    await api('/api/mcp/servers', { method: 'POST', body: { name, spec } });
    ['#addName', '#addCommand', '#addArgs', '#addEnv'].forEach(s => $(s).value = '');
    toast(`Added ${name}. Connect it above.`);
    loadToolsTab();
  } catch (err) { toast(err.message, true); }
});

/* -------------------------------------------------------------------- boot */

(async function boot() {
  try {
    await loadHardware();
    await loadProviderSelect();
    await loadSettings();
    const tools = await api('/api/tools');
    $('#toolCount').textContent = tools.total;

    await loadChats();
    // Reopen the most recent chat, preferring one that is still running.
    const live = state.chats.find(c => c.running);
    const resume = live || state.chats[0];
    if (resume) await openChat(resume.id);
    else newChat();
  } catch (err) {
    toast('Startup problem: ' + err.message, true);
  }
  setInterval(() => loadHardware().catch(() => {}), 20000);
  setInterval(() => { if (!state.streaming) loadChats().catch(() => {}); }, 15000);
})();
