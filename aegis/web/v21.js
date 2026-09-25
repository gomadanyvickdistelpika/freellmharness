/* AEGIS v2.1 front end: agents, MCP catalogue, docked browser, About me,
   checkpoints, boost settings. Uses helpers from app.js and v2.js. */

state.myAgents = [];

/* ------------------------------------------------------------- agents -- */

async function loadAgentsData() {
  try {
    const data = await api('/api/my-agents');
    state.myAgents = data.agents || [];
    state.agentPresets = data.presets || [];
    state.toolFamilies = data.tool_families || [];
  } catch { state.myAgents = []; }
  const opts = '<option value="">Super Agent (auto)</option>' + state.myAgents.map(a =>
    `<option value="${esc(a.key)}">${esc((a.icon ? a.icon + ' ' : '') + a.name)}</option>`).join('');
  const sel = $('#agentSelect');
  if (sel) { const prev = sel.value; sel.innerHTML = opts; sel.value = prev; }
  const task = $('#taskAgent');
  if (task) task.innerHTML = '<option value="">Super Agent (auto)</option>' + state.myAgents.map(a =>
    `<option value="${esc(a.key)}">${esc(a.name)}</option>`).join('');
  const tp = $('#taskProject');
  if (tp) tp.innerHTML = '<option value="">No project</option>' + (state.projectList || []).map(p =>
    `<option value="${esc(p.key)}">${esc(p.name)}</option>`).join('');
}

async function loadAgentsTab() {
  await loadAgentsData();
  $('#agentList').innerHTML = state.myAgents.length ? state.myAgents.map(a => `
    <div class="card"><div class="card-head">
      <div style="min-width:0">
        <div class="card-title">${esc(a.icon || '')} ${esc(a.name)}
          <span class="badge grey">boost ${esc(a.boost)}</span>
          ${a.route ? `<span class="badge grey">${esc(a.route)}</span>` : ''}</div>
        <div class="card-sub">${esc(a.description || '')}</div>
        <div class="pills">${(a.skills || []).map(s => `<span class="pill">${esc(s)}</span>`).join('')}
          ${(a.tools || []).length ? (a.tools || []).map(t => `<span class="pill mono">${esc(t)}</span>`).join('')
            : '<span class="pill mono">all tools</span>'}</div>
      </div>
      <div class="card-actions">
        <button class="sm primary" data-ag-chat="${esc(a.key)}">Chat</button>
        <button class="sm" data-ag-edit="${esc(a.key)}">Edit</button>
        <button class="sm danger" data-ag-del="${esc(a.key)}">×</button>
      </div></div></div>`).join('')
    : '<div class="empty">No agents yet — add a ready-made one below.</div>';

  const have = new Set(state.myAgents.map(a => a.key));
  $('#agentPresets').innerHTML = (state.agentPresets || []).map(p =>
    `<button class="sm" data-ag-preset="${esc(p.key)}" ${have.has(p.key) ? 'disabled' : ''}
      title="${esc(p.description)}">${esc(p.icon || '')} ${esc(p.name)}</button>`).join('');

  $('#agTools').innerHTML = (state.toolFamilies || []).map(t =>
    `<label class="check"><input type="checkbox" value="${esc(t)}"> ${esc(t)}</label>`).join('');
  try {
    const sk = await api('/api/skills');
    $('#agSkills').innerHTML = (sk.skills || []).map(s =>
      `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join('');
  } catch { /* optional */ }
  try {
    const routes = (await api('/api/routes')).routes;
    $('#agRoute').innerHTML = '<option value="">Whatever the chat bar says</option>'
      + routes.map(r => `<option value="${esc(r.key)}">${esc(r.label)}</option>`).join('');
  } catch { /* optional */ }
  loadMcpCatalog();
  loadCheckpoints();
}

function resetAgentForm() {
  ['#agKey', '#agName', '#agIcon', '#agDesc', '#agInstr'].forEach(s => $(s).value = '');
  $('#agRoute').value = ''; $('#agBoost').value = 'auto';
  [...$('#agSkills').options].forEach(o => o.selected = false);
  $$('#agTools input').forEach(i => i.checked = false);
  $('#agSave').textContent = 'Create agent';
  $('#agFormTitle').textContent = 'New agent';
  $('#agCancel').hidden = true;
}

$('#agSave').addEventListener('click', async () => {
  const body = {
    key: $('#agKey').value, name: $('#agName').value.trim(), icon: $('#agIcon').value.trim(),
    description: $('#agDesc').value.trim(), instructions: $('#agInstr').value,
    skills: [...$('#agSkills').selectedOptions].map(o => o.value),
    tools: $$('#agTools input').filter(i => i.checked).map(i => i.value),
    route: $('#agRoute').value, boost: $('#agBoost').value,
  };
  if (!body.name) return toast('Give the agent a name', true);
  try {
    await api('/api/my-agents', { method: 'POST', body });
    toast(body.key ? 'Agent saved' : 'Agent created');
    resetAgentForm(); loadAgentsTab();
  } catch (err) { toast(err.message, true); }
});
$('#agCancel').addEventListener('click', resetAgentForm);

$('#agentsmcp').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const d = btn.dataset;
  if (d.agPreset) {
    await api('/api/my-agents', { method: 'POST', body: { preset: d.agPreset } });
    toast('Agent added'); loadAgentsTab();
  }
  if (d.agDel) {
    if (!confirm('Delete this agent?')) return;
    await api(`/api/my-agents/${d.agDel}`, { method: 'DELETE' });
    loadAgentsTab();
  }
  if (d.agEdit) {
    const a = state.myAgents.find(x => x.key === d.agEdit);
    if (!a) return;
    $('#agKey').value = a.key; $('#agName').value = a.name; $('#agIcon').value = a.icon || '';
    $('#agDesc').value = a.description || ''; $('#agInstr').value = a.instructions || '';
    $('#agRoute').value = a.route || ''; $('#agBoost').value = a.boost || 'auto';
    [...$('#agSkills').options].forEach(o => o.selected = (a.skills || []).includes(o.value));
    $$('#agTools input').forEach(i => i.checked = (a.tools || []).includes(i.value));
    $('#agSave').textContent = 'Save changes';
    $('#agFormTitle').textContent = `Edit ${a.name}`;
    $('#agCancel').hidden = false;
    $('#agName').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  if (d.agChat) {
    $$('nav button').find(b => b.dataset.tab === 'chat').click();
    newChat();
    $('#agentSelect').value = d.agChat;
    toast(`Chatting as ${state.myAgents.find(a => a.key === d.agChat)?.name}`);
  }
  if (d.mcpInstall) {
    const key = d.mcpInstall;
    const values = {};
    $$(`[data-mcp-field^="${key}|"]`).forEach(i => { values[i.dataset.mcpField.split('|')[1]] = i.value; });
    btn.disabled = true; btn.textContent = 'Installing…';
    try {
      const res = await api('/api/mcp/catalog/install', { method: 'POST', body: { key, values } });
      toast(res.connected ? `${key} connected` : (res.note || `${key} added`), !res.connected && !res.note);
      const tools = await api('/api/tools');
      $('#toolCount').textContent = tools.total;
    } catch (err) { toast(err.message, true); }
    loadMcpCatalog();
  }
  if (d.cpRestore) {
    if (!confirm('Restore this file to how it was before that change?')) return;
    try {
      const res = await api('/api/checkpoints/restore', { method: 'POST', body: { id: d.cpRestore } });
      toast(res.text); loadCheckpoints();
    } catch (err) { toast(err.message, true); }
  }
});

async function loadMcpCatalog() {
  let data;
  try { data = await api('/api/mcp/catalog'); } catch { return; }
  const pre = data.prerequisites || {};
  $('#mcpPrereq').innerHTML = `Node (npx): ${pre.node ? '<span class="badge green">found</span>'
    : '<span class="badge amber">missing</span>'} · uv (uvx): ${pre.uv ? '<span class="badge green">found</span>'
    : '<span class="badge amber">missing</span>'}`;
  $('#mcpCatalog').innerHTML = data.items.map(it => `
    <div class="card">
      <div class="card-title">${esc(it.label)} ${it.installed ? '<span class="badge green">installed</span>' : ''}</div>
      <div class="card-sub">${esc(it.description)}</div>
      ${(it.fields || []).map(f => `<div class="field"><label>${esc(f.label)}</label>
        <input type="${f.secret ? 'password' : 'text'}" data-mcp-field="${esc(it.key)}|${esc(f.name)}"
          placeholder="${esc(f.placeholder || '')}"></div>`).join('')}
      ${it.hint ? `<div class="hint">${esc(it.hint)}</div>` : ''}
      <button class="sm primary" data-mcp-install="${esc(it.key)}">${it.installed ? 'Reinstall' : 'Install'}</button>
    </div>`).join('');
}

async function loadCheckpoints() {
  let data;
  try { data = await api('/api/checkpoints'); } catch { return; }
  $('#checkpointList').innerHTML = data.items.length ? `<div class="card">${data.items.slice(0, 40).map(c => `
    <div class="srv-tool">
      <span class="badge grey">${new Date(c.time * 1000).toLocaleString()}</span>
      <code>${esc(c.path)}</code>
      <span class="d">${c.existed ? 'changed' : 'created'}</span>
      <button class="sm" data-cp-restore="${esc(c.id)}">Restore</button>
    </div>`).join('')}</div>` : '<div class="empty">No file changes yet.</div>';
}

/* ------------------------------------------------------ docked browser -- */

const dock = { timer: null, open: false, user: false };

function dockShow(show) {
  dock.open = show;
  $('#dock').hidden = !show;
  $('#browserToggle').classList.toggle('on', show);
  clearInterval(dock.timer);
  if (show) { dockRefresh(); dock.timer = setInterval(dockRefresh, 1500); }
}

async function dockRefresh() {
  if (!dock.open) return;
  try {
    const res = await fetch('/api/browser/view?t=' + Date.now());
    if (res.status === 200) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const img = $('#dkView');
      const old = img.src;
      img.src = url;
      img.style.display = 'block';
      $('#dkBlank').hidden = true;
      if (old.startsWith('blob:')) URL.revokeObjectURL(old);
    }
  } catch { /* the view is best-effort */ }
}

async function dockAct(body) {
  try {
    const st = await api('/api/browser/act', { method: 'POST', body });
    if (st.url && document.activeElement !== $('#dkUrl')) $('#dkUrl').value = st.url;
  } catch (err) { toast(err.message, true); }
  dockRefresh();
}

$('#browserToggle').addEventListener('click', () => dockShow(!dock.open));
$('#dkClose').addEventListener('click', () => dockShow(false));
$('#dkBack').addEventListener('click', () => dockAct({ action: 'back' }));
$('#dkReload').addEventListener('click', () => dockAct({ action: 'reload' }));
$('#dkUp').addEventListener('click', () => dockAct({ action: 'scroll', amount: -600 }));
$('#dkDown').addEventListener('click', () => dockAct({ action: 'scroll', amount: 600 }));
$('#dkGo').addEventListener('click', async () => {
  const url = $('#dkUrl').value.trim();
  if (!url) return;
  try { await api('/api/browser/open', { method: 'POST', body: { url } }); }
  catch (err) { toast(err.message, true); }
  dockRefresh();
});
$('#dkUrl').addEventListener('keydown', e => { if (e.key === 'Enter') $('#dkGo').click(); });
$('#dkView').addEventListener('click', e => {
  const r = e.target.getBoundingClientRect();
  dockAct({ action: 'click_xy', x: (e.clientX - r.left) / r.width, y: (e.clientY - r.top) / r.height });
});
$('#dkType').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  const text = $('#dkType').value;
  $('#dkType').value = '';
  (async () => {
    if (text) await dockAct({ action: 'text', text });
    if (e.shiftKey || !text) await dockAct({ action: 'key', key: 'Enter' });
  })();
});
$('#dkTake').addEventListener('click', async () => {
  dock.user = !dock.user;
  await api('/api/browser/control', { method: 'POST', body: { user: dock.user } });
  $('#dkTake').textContent = dock.user ? 'Hand back to the agent' : 'Take over';
  $('#dkState').textContent = dock.user ? 'You have control — the agent waits'
                                        : 'Agent can use the browser';
  $('#dock').classList.toggle('user-control', dock.user);
});

/* Open the dock by itself when the agent starts browsing. */
const _applyEventV20 = applyEvent;
applyEvent = function (reply, evt) {
  _applyEventV20(reply, evt);
  if (evt.type === 'tool_call' && String(evt.name || '').startsWith('browser__') && !dock.open) {
    dockShow(true);
  }
};

/* ------------------------------------------------------------ about me -- */

async function loadMe(select) {
  let data;
  try { data = await api('/api/me'); } catch { return; }
  const tone = { public: 'green', personal: 'amber', private: 'red' };
  $('#meList').innerHTML = data.entries.map(e => `
    <div class="item ${e.name === select ? 'active' : ''}" data-me="${esc(e.name)}">
      <div class="t">${esc(e.title)}</div>
      <span class="badge ${tone[e.tier] || 'grey'}">${esc(e.tier)}</span>
      <span class="hint">${esc(e.name)}.md</span>
    </div>`).join('') + '<button class="sm" id="meNew" style="width:100%">+ New note</button>';
  $('#meNew').addEventListener('click', () => {
    $('#meName').value = ''; $('#meText').value = '---\ntitle: \ntier: private\ntopics: \n---\n';
    $$('#meList .item').forEach(i => i.classList.remove('active'));
    $('#meName').focus();
  });
  if (select) openMe(select);
  else if (data.entries[0] && !$('#meName').value) openMe(data.entries[0].name);
}

async function openMe(name) {
  const res = await api(`/api/me/${encodeURIComponent(name)}`);
  $('#meName').value = name;
  $('#meText').value = res.text;
  $$('#meList .item').forEach(i => i.classList.toggle('active', i.dataset.me === name));
}

$('#meList').addEventListener('click', e => {
  const item = e.target.closest('[data-me]');
  if (item) openMe(item.dataset.me);
});
$('#meSave').addEventListener('click', async () => {
  const name = $('#meName').value.trim();
  if (!name) return toast('Give the note a file name', true);
  try {
    const res = await api('/api/me', { method: 'POST', body: { name, text: $('#meText').value } });
    toast('Saved'); loadMe(res.entry.name);
  } catch (err) { toast(err.message, true); }
});
$('#meDel').addEventListener('click', async () => {
  const name = $('#meName').value.trim();
  if (!name || !confirm(`Delete the note "${name}"?`)) return;
  await api(`/api/me/${encodeURIComponent(name)}`, { method: 'DELETE' });
  $('#meName').value = ''; $('#meText').value = '';
  loadMe();
});
$('#meRestore').addEventListener('click', async () => {
  if (!confirm('Put the original notes back? Your edits to those files will be replaced.')) return;
  await api('/api/me', { method: 'POST', body: { restore_defaults: true } });
  toast('Original notes restored'); loadMe();
});

/* ------------------------------------------------------------ settings -- */

const _loadBehaviourV20 = loadBehaviour;
loadBehaviour = async function () {
  await _loadBehaviourV20();
  const s = (await api('/api/settings')).settings;
  $('#boostMode').value = s.boost_mode || 'auto';
  $('#textTools').checked = s.text_tools !== false;
  $('#maxUpload').value = s.max_upload_gb ?? 30;
};
$('#saveBehaviour').addEventListener('click', async () => {
  await api('/api/settings', { method: 'POST', body: {
    boost_mode: $('#boostMode').value, text_tools: $('#textTools').checked,
    max_upload_gb: Math.max(1, Number($('#maxUpload').value) || 30) } });
});

/* ---------------------------------------------------------------- tabs -- */

$$('nav button').forEach(btn => btn.addEventListener('click', () => {
  if (btn.dataset.tab === 'agentsmcp') loadAgentsTab();
  if (btn.dataset.tab === 'me') loadMe();
  if (btn.dataset.tab === 'filetasks') loadAgentsData();
}));

(async function bootV21() {
  await loadAgentsData();
})();

/* ---------------------------------------------------------------- voice -- */

const voiceState = { rec: null, chunks: [], handsFree: false, speak: false, busy: false };
try { voiceState.speak = localStorage.getItem('aegis.speak') === '1'; } catch { /* optional */ }
$('#speakToggle').textContent = voiceState.speak ? '🔊' : '🔈';

$('#speakToggle').addEventListener('click', () => {
  voiceState.speak = !voiceState.speak;
  try { localStorage.setItem('aegis.speak', voiceState.speak ? '1' : '0'); } catch { /* optional */ }
  $('#speakToggle').textContent = voiceState.speak ? '🔊' : '🔈';
  if (!voiceState.speak) window.speechSynthesis?.cancel();
  toast(voiceState.speak ? 'Replies will be read aloud' : 'Read-aloud off');
});

function plainForSpeech(md) {
  return String(md || '')
    .replace(/<think>[\s\S]*?<\/think>/g, '')
    .replace(/```[\s\S]*?```/g, ' (code shown on screen) ')
    .replace(/\[\[(media|file):[^\]]*\]\]/g, '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[#*_`>|~-]{1,}/g, ' ')
    .replace(/\s+/g, ' ').trim();
}

function speak(text) {
  if (!voiceState.speak || !window.speechSynthesis) return;
  const clean = plainForSpeech(text).slice(0, 4000);
  if (!clean) return;
  const french = (clean.match(/\b(le|la|les|des|est|pour|avec|vous|je|une|et)\b/gi) || []).length
    > (clean.match(/\b(the|is|and|you|for|with|to|of)\b/gi) || []).length;
  const u = new SpeechSynthesisUtterance(clean);
  u.lang = french ? 'fr-FR' : 'en-IE';
  const voice = speechSynthesis.getVoices().find(v => v.lang.startsWith(french ? 'fr' : 'en'));
  if (voice) u.voice = voice;
  speechSynthesis.cancel();
  speechSynthesis.speak(u);
  if (voiceState.handsFree) u.onend = () => setTimeout(() => startRecording(), 400);
}

/* Read the reply aloud when a run finishes. */
const _consumeStreamV20 = consumeStream;
consumeStream = async function (res, reply, controller) {
  await _consumeStreamV20(res, reply, controller);
  const text = (reply.parts || []).filter(p => p.kind === 'text').map(p => p.text).join('\n');
  speak(text);
};

async function startRecording() {
  if (voiceState.rec) return;
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
  catch { return browserRecognition(); }
  const rec = new MediaRecorder(stream);
  voiceState.rec = rec;
  voiceState.chunks = [];
  rec.ondataavailable = e => { if (e.data.size) voiceState.chunks.push(e.data); };
  rec.onstop = async () => {
    stream.getTracks().forEach(t => t.stop());
    voiceState.rec = null;
    $('#micBtn').classList.remove('rec');
    const blob = new Blob(voiceState.chunks, { type: rec.mimeType || 'audio/webm' });
    if (blob.size < 1500) return;
    $('#micBtn').textContent = '⏳';
    const form = new FormData();
    form.append('file', blob, 'speech.webm');
    try {
      const res = await fetch('/api/voice/transcribe', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'transcription failed');
      voiceGot(data.text);
    } catch (err) {
      toast('Cloud transcription unavailable — using the browser\'s speech recognition', true);
      browserRecognition();
    } finally { $('#micBtn').textContent = '🎤'; }
  };
  rec.start();
  $('#micBtn').classList.add('rec');
  // Hands-free: stop by itself after 30 s at most.
  setTimeout(() => { if (voiceState.rec === rec) rec.stop(); }, 30000);
}

function browserRecognition() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return toast('No speech recognition available — add a Groq key for voice', true);
  const r = new SR();
  r.lang = navigator.language || 'en-IE';
  r.interimResults = false;
  r.onresult = e => voiceGot(e.results[0][0].transcript);
  r.onerror = e => toast('Speech recognition: ' + e.error, true);
  r.start();
}

function voiceGot(text) {
  text = String(text || '').trim();
  if (!text) return;
  const input = $('#input');
  input.value = (input.value ? input.value + ' ' : '') + text;
  input.dispatchEvent(new Event('input'));
  if (voiceState.handsFree) handleSend();
}

$('#micBtn').addEventListener('click', () => {
  if (voiceState.rec) voiceState.rec.stop(); else startRecording();
});
$('#micBtn').addEventListener('contextmenu', e => {
  e.preventDefault();
  voiceState.handsFree = !voiceState.handsFree;
  $('#micBtn').classList.toggle('handsfree', voiceState.handsFree);
  if (voiceState.handsFree && !voiceState.speak) $('#speakToggle').click();
  toast(voiceState.handsFree ? 'Hands-free voice mode: talk, it sends, it answers aloud, it listens again'
                             : 'Hands-free voice mode off');
  if (voiceState.handsFree) startRecording();
});

/* ------------------------------------------------------- slash commands -- */

const SLASH = [
  ['/chat', 'plain chat with the Super Agent (clears the agent)'],
  ['/new', 'start a new chat'],
  ['/image', 'make an image: /image a lighthouse at dusk'],
  ['/music', 'make a song or a Suno pack'],
  ['/video', 'make a short video'],
  ['/voice', 'turn text into speech'],
  ['/search', 'research on the web with sources'],
  ['/code', 'switch to the Coder agent and send'],
  ['/agent', 'chat as an agent: /agent job-hunter'],
  ['/project', 'switch project: /project job hunt'],
  ['/browser', 'open the browser panel (optionally at a URL)'],
  ['/computer', 'do something on the desktop with computer use'],
  ['/schedule', 'create a scheduled task'],
  ['/boost', 'boost on | off | auto'],
  ['/talk', 'hands-free voice mode on/off'],
  ['/speak', 'read replies aloud on/off'],
  ['/undo', 'undo the last file change'],
  ['/files', 'show workspace files to download'],
  ['/help', 'list commands'],
];

function renderSlash() {
  const v = $('#input').value;
  const menu = $('#slashMenu');
  if (!v.startsWith('/') || v.includes(' ') && v.indexOf(' ') < v.length - 0 && !/^\/\S*$/.test(v)) {
    menu.hidden = true; return;
  }
  const hits = SLASH.filter(([c]) => c.startsWith(v.split(' ')[0]));
  if (!hits.length) { menu.hidden = true; return; }
  menu.innerHTML = hits.map(([c, d]) => `<div class="sl" data-sl="${c}"><b>${c}</b> <span>${esc(d)}</span></div>`).join('');
  menu.hidden = false;
}
$('#input').addEventListener('input', renderSlash);
$('#slashMenu').addEventListener('click', e => {
  const item = e.target.closest('[data-sl]');
  if (!item) return;
  $('#input').value = item.dataset.sl + ' ';
  $('#slashMenu').hidden = true;
  $('#input').focus();
});

function selectAgentByName(name) {
  const q = name.trim().toLowerCase();
  const hit = state.myAgents.find(a => a.key === q || a.name.toLowerCase().includes(q));
  if (hit) { $('#agentSelect').value = hit.key; toast(`Agent: ${hit.name}`); }
  else toast(`No agent matching "${name}"`, true);
  return hit;
}

/* Returns true when the command was fully handled; otherwise rewrites the input. */
async function runSlash() {
  const input = $('#input');
  const raw = input.value.trim();
  const [cmd, ...restParts] = raw.split(' ');
  const rest = restParts.join(' ').trim();
  $('#slashMenu').hidden = true;
  const say = text => { input.value = text; return false; };
  switch (cmd) {
    case '/help':
      toast(SLASH.map(([c]) => c).join('  '));
      input.value = ''; return true;
    case '/new': input.value = ''; newChat(); return true;
    case '/chat': $('#agentSelect').value = ''; return rest ? say(rest) : (input.value = '', true);
    case '/image': return say(`Use media__generate_image to create this image: ${rest}`);
    case '/music': return say(`Use media__generate_music to create this: ${rest}`);
    case '/video': return say(`Use media__generate_video to create this video: ${rest}`);
    case '/voice': return say(`Use media__generate_speech to read this aloud: ${rest}`);
    case '/search': return say(`Research this on the web, read the best sources and cite them: ${rest}`);
    case '/computer': return say(`Using computer use on my desktop (ask before acting): ${rest}`);
    case '/schedule': return say(`Create a scheduled task with project__schedule: ${rest}`);
    case '/code':
      selectAgentByName('coder');
      return rest ? say(rest) : (input.value = '', true);
    case '/agent':
      if (rest) selectAgentByName(rest.split(' ')[0]);
      input.value = rest.split(' ').slice(1).join(' ');
      return !input.value;
    case '/project': {
      const q = rest.toLowerCase();
      const p = (state.projectList || []).find(x => x.key === q || x.name.toLowerCase().includes(q));
      if (p) setProject(p.key); else toast(`No project matching "${rest}"`, true);
      input.value = ''; return true;
    }
    case '/browser':
      dockShow(true);
      if (rest) { $('#dkUrl').value = rest; $('#dkGo').click(); }
      input.value = ''; return true;
    case '/boost': {
      const v = { on: 'always', off: 'off', auto: '' }[rest || 'auto'];
      $('#boostSelect').value = v ?? '';
      toast(`Boost: ${rest || 'auto'}`); input.value = ''; return true;
    }
    case '/talk': input.value = ''; $('#micBtn').dispatchEvent(new MouseEvent('contextmenu')); return true;
    case '/speak': input.value = ''; $('#speakToggle').click(); return true;
    case '/files': input.value = ''; showFiles(); return true;
    case '/undo':
      input.value = '';
      try { const r = await api('/api/checkpoints/restore', { method: 'POST', body: {} }); toast(r.text); }
      catch (err) { toast(err.message, true); }
      return true;
    default: return false;
  }
}

async function handleSend() {
  if ($('#input').value.trim().startsWith('/')) {
    const handled = await runSlash();
    if (handled || !$('#input').value.trim()) return;
  }
  send();
}

/* Take over Enter and the Send button so slash commands run first. */
$('#input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && $('#input').value.trim().startsWith('/')) {
    e.preventDefault(); e.stopImmediatePropagation();
    handleSend();
  }
}, true);
$('#send').addEventListener('click', e => {
  if ($('#input').value.trim().startsWith('/')) {
    e.preventDefault(); e.stopImmediatePropagation();
    handleSend();
  }
}, true);

/* ------------------------------------------------------ workspace files -- */

async function showFiles() {
  const pop = $('#filesPop');
  if (!pop.hidden) { pop.hidden = true; return; }
  let data;
  try { data = await api('/api/workspace' + (state.project ? `?project=${encodeURIComponent(state.project)}` : '')); }
  catch (err) { return toast(err.message, true); }
  pop.innerHTML = `<div class="card-head"><div class="card-title">Workspace files</div>
      <button class="sm ghost" id="filesClose">✕</button></div>
    <div class="card-sub mono">${esc(data.root)}</div>
    <div class="files-list">${data.items.length ? data.items.slice(0, 80).map(f =>
      `<div class="srv-tool">${fileChip(f.path)}<span class="d">${esc(f.name)} · ${bytes(f.bytes)}</span></div>`).join('')
      : '<div class="empty" style="padding:1rem">Nothing yet. Files the agent writes, generates or you upload show here.</div>'}</div>`;
  pop.hidden = false;
  $('#filesClose').addEventListener('click', () => pop.hidden = true);
}
$('#filesBtn').addEventListener('click', showFiles);
