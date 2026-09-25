/* AEGIS v2 front end: projects, Studio, quick setup, behaviour settings,
   smart-route options. Loaded after app.js and uses its helpers ($, api, esc,
   toast, state). */

state.project = '';
state.projectList = [];
state.studioKind = 'image';

/* ------------------------------------------------------------ projects -- */

async function loadProjectsData() {
  try {
    const data = await api('/api/projects');
    state.projectList = data.projects || [];
    state.generalWorkspace = data.general_workspace;
  } catch { state.projectList = []; }
  renderProjectSelects();
}

function renderProjectSelects() {
  const opts = '<option value="">No project</option>' + state.projectList.map(p =>
    `<option value="${esc(p.key)}">▸ ${esc(p.name)}</option>`).join('');
  const sel = $('#projectSelect');
  if (sel) { sel.innerHTML = opts; sel.value = state.project || ''; }
  const studio = $('#studioProject');
  if (studio) {
    const prev = studio.value;
    studio.innerHTML = '<option value="">Gallery only</option>' + state.projectList.map(p =>
      `<option value="${esc(p.key)}">Also save to ${esc(p.name)}</option>`).join('');
    studio.value = prev || state.project || '';
  }
}

/* Switch the chat view into (or out of) a project. */
function setProject(key, opts = {}) {
  state.project = key || '';
  const sel = $('#projectSelect');
  if (sel) sel.value = state.project;
  const proj = state.projectList.find(p => p.key === state.project);
  if (proj && proj.route) {
    const routeSel = $('#routeSelect');
    if ([...routeSel.options].some(o => o.value === `route:${proj.route}`)) {
      routeSel.value = `route:${proj.route}`;
      applyRouteMode();
    }
  }
  if (!opts.keepChat) newChat();
  else loadChats();
}

$('#projectSelect').addEventListener('change', async e => {
  const key = e.target.value;
  // Moving an existing chat into a project is a deliberate act; ask.
  if (state.chatId && key && confirm('Move this chat into the project too?\n'
      + '(Cancel starts a new chat in the project instead.)')) {
    await api(`/api/chats/${state.chatId}/project`, { method: 'POST', body: { project: key } });
    setProject(key, { keepChat: true });
    toast('Chat moved into the project');
    return;
  }
  setProject(key);
});

async function loadProjectsTab() {
  await loadProjectsData();
  const list = $('#projectList');
  list.innerHTML = state.projectList.length ? state.projectList.map(p => `
    <div class="card">
      <div class="card-head">
        <div style="min-width:0">
          <div class="card-title">${esc(p.name)} ${p.route ? `<span class="badge grey">${esc(p.route)}</span>` : ''}</div>
          <div class="card-sub">${esc(p.description || '')}</div>
        </div>
        <div class="card-actions">
          <span class="pill">${p.chats} chat${p.chats === 1 ? '' : 's'}</span>
          <button class="sm primary" data-proj-open="${esc(p.key)}">Open chat</button>
          <button class="sm" data-proj-notes="${esc(p.key)}">Notes</button>
          <button class="sm" data-proj-edit="${esc(p.key)}">Edit</button>
          <button class="sm danger" data-proj-del="${esc(p.key)}">Remove</button>
        </div>
      </div>
      <div class="fit-detail"><span class="pill mono">${esc(p.workspace)}</span>
        ${(p.skills || []).map(s => `<span class="badge green">${esc(s)}</span>`).join(' ')}
        ${p.knowledge?.length ? `<br>${p.knowledge.length} knowledge file(s): ${
          p.knowledge.slice(0, 6).map(esc).join(', ')}` : ''}</div>
    </div>`).join('')
    : '<div class="empty">No projects yet — create one below, or just tell the '
      + 'assistant "make this a project".</div>';

  // Route and skill pickers for the form.
  try {
    const routes = (await api('/api/routes')).routes;
    $('#projRoute').innerHTML = '<option value="">Use whatever the chat bar says</option>'
      + routes.map(r => `<option value="${esc(r.key)}">${esc(r.label)}</option>`).join('');
  } catch { /* optional */ }
  try {
    const sk = await api('/api/skills');
    const names = (sk.skills || []).filter(s => s.loadable !== false).map(s => s.name);
    $('#projSkills').innerHTML = names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('');
  } catch { /* optional */ }
}

function resetProjectForm() {
  ['#projKey', '#projName', '#projDesc', '#projInstr', '#projFolder'].forEach(s => $(s).value = '');
  $('#projRoute').value = '';
  [...$('#projSkills').options].forEach(o => o.selected = false);
  $('#projSave').textContent = 'Create project';
  $('#projFormTitle').textContent = 'New project';
  $('#projCancel').hidden = true;
}

$('#projSave').addEventListener('click', async () => {
  const body = {
    key: $('#projKey').value, name: $('#projName').value.trim(),
    description: $('#projDesc').value.trim(), instructions: $('#projInstr').value,
    folder: $('#projFolder').value.trim(), route: $('#projRoute').value,
    skills: [...$('#projSkills').selectedOptions].map(o => o.value),
  };
  if (!body.name) return toast('Give the project a name', true);
  try {
    const res = await api('/api/projects', { method: 'POST', body });
    toast(body.key ? 'Project saved' : `Project created — workspace ${res.project.workspace}`);
    resetProjectForm();
    loadProjectsTab();
  } catch (err) { toast(err.message, true); }
});
$('#projCancel').addEventListener('click', resetProjectForm);

$('#projects').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  const d = btn.dataset;
  if (d.projOpen) {
    $$('nav button').find(b => b.dataset.tab === 'chat').click();
    setProject(d.projOpen);
  }
  if (d.projEdit) {
    const p = state.projectList.find(x => x.key === d.projEdit);
    if (!p) return;
    $('#projKey').value = p.key; $('#projName').value = p.name;
    $('#projDesc').value = p.description || ''; $('#projInstr').value = p.instructions || '';
    $('#projFolder').value = p.folder || ''; $('#projRoute').value = p.route || '';
    [...$('#projSkills').options].forEach(o => o.selected = (p.skills || []).includes(o.value));
    $('#projSave').textContent = 'Save changes';
    $('#projFormTitle').textContent = `Edit ${p.name}`;
    $('#projCancel').hidden = false;
    $('#projName').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  if (d.projDel) {
    if (!confirm('Remove this project? Its chats stay, and its folder and files '
                 + 'are left on disk.')) return;
    await api(`/api/projects/${d.projDel}`, { method: 'DELETE' });
    if (state.project === d.projDel) state.project = '';
    loadProjectsTab();
  }
  if (d.projNotes) {
    const p = state.projectList.find(x => x.key === d.projNotes);
    const res = await api(`/api/projects/${d.projNotes}/notes`);
    $('#projNotes').value = res.text;
    $('#projNotes').dataset.key = d.projNotes;
    $('#projNotesName').textContent = p?.name || d.projNotes;
    $('#projNotesBox').hidden = false;
    $('#projNotesBox').scrollIntoView({ behavior: 'smooth' });
  }
});

$('#projNotesSave').addEventListener('click', async () => {
  const key = $('#projNotes').dataset.key;
  if (!key) return;
  await api(`/api/projects/${key}/notes`, { method: 'POST', body: { text: $('#projNotes').value } });
  toast('Notes saved');
});

/* -------------------------------------------------------------- studio -- */

function studioShowFields() {
  $$('[data-for]', $('#studio')).forEach(el => {
    el.hidden = el.dataset.for !== state.studioKind;
  });
  $$('#studioKind button').forEach(b => b.classList.toggle('on', b.dataset.kind === state.studioKind));
  const ph = {
    image: 'e.g. A lighthouse on a rocky coast at dusk, warm cinematic light, 35mm photo',
    speech: 'The text to read aloud…',
    music: 'e.g. Afro-pop, 118 BPM, bright guitars, joyful, about rainy city mornings',
    video: 'e.g. Slow drone shot over Ouagadougou at sunrise, then a busy market, warm colours',
  }[state.studioKind];
  $('#studioPrompt').placeholder = ph;
}

$('#studioKind').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  state.studioKind = b.dataset.kind;
  studioShowFields();
  loadGallery();
});

$('#studioGo').addEventListener('click', async () => {
  const prompt = $('#studioPrompt').value.trim();
  if (!prompt) return toast('Describe what to make', true);
  const kind = state.studioKind;
  const body = { kind, prompt, project: $('#studioProject').value };
  if (kind === 'image') {
    const [w, h] = $('#studioSize').value.split('x').map(Number);
    body.width = w; body.height = h;
  }
  if (kind === 'speech' && $('#studioVoice').value.trim()) body.voice = $('#studioVoice').value.trim();
  if (kind === 'music') {
    if ($('#studioLyrics').value.trim()) body.lyrics = $('#studioLyrics').value.trim();
    if (Number($('#studioDuration').value)) body.duration = Number($('#studioDuration').value);
  }
  if (kind === 'video' && $('#studioNarration').value.trim()) body.narration = $('#studioNarration').value.trim();

  const count = kind === 'image' ? Number($('#studioCount').value) : 1;
  const btn = $('#studioGo');
  btn.disabled = true;
  $('#studioNote').textContent = kind === 'video'
    ? 'Working — video can take a few minutes…' : 'Working…';
  let made = 0;
  const notes = [];
  for (let i = 0; i < count; i++) {
    try {
      const res = await api('/api/media/generate', { method: 'POST',
        body: count > 1 ? { ...body, seed: Math.floor(Math.random() * 1e6) } : body });
      made++;
      const it = res.item;
      notes.push(`via ${it.meta?.backend_label || it.backend}${
        it.tried?.length ? ` (skipped: ${it.tried.join('; ')})` : ''}`);
    } catch (err) { notes.push(err.message); }
  }
  btn.disabled = false;
  $('#studioNote').textContent = notes.join(' · ');
  if (made) { toast(`Made ${made} ${kind}${made > 1 ? 's' : ''}`); loadGallery(); loadStudioBackends(); }
  else toast('Nothing could be made — see the note under the prompt', true);
});

async function loadGallery() {
  let data;
  try { data = await api(`/api/media?kind=${state.studioKind}&limit=60`); }
  catch { return; }
  $('#studioGallery').innerHTML = data.items.length ? data.items.map(it => `
    <div class="gcard">
      ${mediaTag(it.id, it.mime)}
      <div class="gmeta" title="${esc(it.prompt)}">${esc(it.prompt.slice(0, 110))}</div>
      <div class="gfoot"><span class="pill">${esc(it.meta?.backend_label || it.backend)}</span>
        <button class="sm ghost" data-media-reuse="${esc(it.id)}">reuse prompt</button>
        <button class="sm danger" data-media-del="${esc(it.id)}">×</button></div>
    </div>`).join('') : '<div class="empty">Nothing here yet.</div>';
  state.galleryItems = data.items;
}

$('#studioGallery').addEventListener('click', async e => {
  const btn = e.target.closest('button');
  if (!btn) return;
  if (btn.dataset.mediaDel) {
    await api(`/api/media/${btn.dataset.mediaDel}`, { method: 'DELETE' });
    loadGallery();
  }
  if (btn.dataset.mediaReuse) {
    const it = (state.galleryItems || []).find(x => x.id === btn.dataset.mediaReuse);
    if (it) { $('#studioPrompt').value = it.prompt; $('#studioPrompt').focus(); }
  }
});

async function loadStudioBackends() {
  let data;
  try { data = await api('/api/media-backends'); } catch { return; }
  $('#studioPaid').checked = data.allow_paid;
  const kinds = { image: 'Images', speech: 'Voice', music: 'Music', video: 'Video' };
  $('#studioBackends').innerHTML = Object.entries(kinds).map(([k, label]) => {
    const rows = data.backends.filter(b => b.kind === k).map(b => {
      let badge = b.enabled === false ? '<span class="badge grey">off</span>'
        : !b.ready ? `<span class="badge grey">${esc(b.why)}</span>`
        : b.resting ? `<span class="badge amber">resting ${b.resting}s</span>`
        : '<span class="badge green">ready</span>';
      if (b.paid) badge += ' <span class="badge amber">paid</span>';
      return `<div class="srv-tool">
        <label class="check"><input type="checkbox" data-mb-toggle="${esc(b.id)}" ${b.enabled !== false ? 'checked' : ''}></label>
        ${badge}
        <code>${esc(b.label || b.id)}</code>
        <span class="d">${esc(b.method)}${b.provider ? ' · ' + esc(b.provider) : ''}${b.model ? ' · ' + esc(b.model) : ''}${
          b.last_error ? ' · last: ' + esc(b.last_error.slice(0, 80)) : ''}</span>
        <button class="sm ghost" data-mb-up="${esc(b.id)}">↑</button>
      </div>`;
    }).join('');
    return `<div class="card"><div class="card-title">${label}</div>${rows || '<div class="fit-detail">none</div>'}</div>`;
  }).join('');
  state.mediaBackends = data.backends.map(({ ready, why, resting, last_error, ...rest }) => rest);
  $('#studioJson').value = JSON.stringify(state.mediaBackends, null, 2);
}

async function saveBackends(list) {
  await api('/api/media-backends', { method: 'POST', body: { backends: list } });
  loadStudioBackends();
}

$('#studioBackends').addEventListener('change', e => {
  const id = e.target.dataset.mbToggle;
  if (!id) return;
  saveBackends(state.mediaBackends.map(b => b.id === id ? { ...b, enabled: e.target.checked } : b));
});
$('#studioBackends').addEventListener('click', e => {
  const id = e.target.closest('button')?.dataset.mbUp;
  if (!id) return;
  const list = [...state.mediaBackends];
  const i = list.findIndex(b => b.id === id);
  const kind = list[i].kind;
  let j = i - 1;
  while (j >= 0 && list[j].kind !== kind) j--;
  if (j < 0) return;
  [list[j], list[i]] = [list[i], list[j]];
  saveBackends(list);
});
$('#studioPaid').addEventListener('change', async e => {
  await api('/api/media-backends', { method: 'POST', body: { allow_paid: e.target.checked } });
  toast(e.target.checked ? 'Studio may now use paid backends' : 'Studio is free-only');
  loadStudioBackends();
});
$('#studioRevive').addEventListener('click', async () => {
  await api('/api/media-backends', { method: 'POST', body: { revive: true } });
  toast('Resting backends will be tried again'); loadStudioBackends();
});
$('#studioReset').addEventListener('click', async () => {
  if (!confirm('Restore the default backend list?')) return;
  await api('/api/media-backends', { method: 'POST', body: { reset: true } });
  loadStudioBackends();
});
$('#studioJsonSave').addEventListener('click', async () => {
  let list;
  try { list = JSON.parse($('#studioJson').value); } catch (err) { return toast('Not valid JSON: ' + err.message, true); }
  if (!Array.isArray(list)) return toast('Expected a list of backends', true);
  await saveBackends(list);
  toast('Backends saved');
});

/* --------------------------------------------------------- quick setup -- */

async function loadQuickSetup() {
  let st;
  try { st = await api('/api/setup/status'); } catch { return; }
  const box = $('#quickSetup');
  const links = {
    openrouter: 'openrouter.ai/keys', xkiro: 'xkiro.com', ninerouter: 'localhost:20128 (after running 9router)',
    pollinations: 'enter.pollinations.ai', gemini: 'aistudio.google.com/apikey',
    groq: 'console.groq.com/keys', cerebras: 'cloud.cerebras.ai', hfrouter: 'huggingface.co/settings/tokens',
    github: 'github.com/settings/tokens (models:read)', nvidia: 'build.nvidia.com',
    freellmapi: 'your local FreeLLMAPI',
  };
  box.innerHTML = `
    <div class="qs-grid">${st.quick_presets.map(p => `
      <div class="field"><label>${esc(p.label)}</label>
        <input type="password" data-qs="${esc(p.key)}" placeholder="API key — ${esc(links[p.key] || '')}">
      </div>`).join('')}</div>
    <div class="row-inline">
      <button class="primary" id="qsGo">Save keys and build my Auto route</button>
      <span class="hint">${st.providers ? `${st.with_keys} provider key(s) saved · Auto route has ${st.auto_models} model(s)`
        : 'Nothing set up yet — one key is enough to start.'}</span>
    </div>`;
  $('#qsGo').addEventListener('click', async () => {
    const keys = {};
    $$('[data-qs]').forEach(i => { if (i.value.trim()) keys[i.dataset.qs] = i.value.trim(); });
    if (!Object.keys(keys).length) return toast('Paste at least one key', true);
    const btn = $('#qsGo');
    btn.disabled = true; btn.textContent = 'Saving and asking each provider what is free…';
    try {
      const res = await api('/api/setup/quick', { method: 'POST', body: { keys } });
      const r = res.route || {};
      toast(r.ok ? `Auto route ready: ${r.cloud} hosted + ${r.local} local models`
                 : (r.error || 'Saved, but no free models were found'), !r.ok);
      if (r.notes?.length) $('#autobuildNote').textContent = r.notes.join(' · ');
    } catch (err) { toast(err.message, true); }
    btn.disabled = false; btn.textContent = 'Save keys and build my Auto route';
    loadQuickSetup(); loadRoutes(); loadCustomProviders(); loadProviderSelect();
  });
  return st;
}

/* First run: say how to get going, right in the chat. */
async function firstRunHint() {
  let st;
  try { st = await api('/api/setup/status'); } catch { return; }
  if (!st.needs_setup || state.messages.length) return;
  $('#messages').innerHTML = `<div class="empty welcome">
    <h3>Welcome to AEGIS 2</h3>
    <p>Paste one free API key (OpenRouter, xKiro, Gemini, Groq, Pollinations…)
      and AEGIS builds a smart route that switches models by itself when one
      runs out — no errors, no model picking.</p>
    <button class="primary" id="goSetup">Quick setup</button>
    <p class="hint">Or start Ollama / LM Studio and chat locally right away.</p>
  </div>`;
  $('#goSetup').addEventListener('click', () =>
    $$('nav button').find(b => b.dataset.tab === 'providers').click());
}

/* ---------------------------------------------------- behaviour settings -- */

async function loadBehaviour() {
  const data = await api('/api/settings');
  const s = data.settings;
  $('#profileSharing').value = s.profile_sharing || 'lite';
  $('#toolDiet').checked = s.tool_diet !== false;
  $('#codeTools').checked = s.code_tools_enabled !== false;
  $('#webTools').checked = s.web_tools_enabled !== false;
  $('#workspaceOn').checked = s.workspace_enabled !== false;
  $('#searxng').value = s.searxng_url || '';
  if (state.generalWorkspace) $('#workspacePath').textContent = state.generalWorkspace;
}

$('#saveBehaviour').addEventListener('click', async () => {
  await api('/api/settings', { method: 'POST', body: {
    profile_sharing: $('#profileSharing').value,
    tool_diet: $('#toolDiet').checked,
    code_tools_enabled: $('#codeTools').checked,
    web_tools_enabled: $('#webTools').checked,
    workspace_enabled: $('#workspaceOn').checked,
    searxng_url: $('#searxng').value.trim(),
  } });
  toast('Saved');
  const tools = await api('/api/tools');
  $('#toolCount').textContent = tools.total;
});

/* ------------------------------------------------ smart route options -- */

$('#providers').addEventListener('change', async e => {
  const smartKey = e.target.dataset.routeSmart;
  const thenKey = e.target.dataset.routeThen;
  const key = smartKey || thenKey;
  if (!key) return;
  const route = state.routes.find(r => r.key === key);
  if (!route) return;
  if (smartKey) route.smart = e.target.checked;
  if (thenKey !== undefined && thenKey) {
    const target = state.routes.find(r => r.key === e.target.value);
    if (target?.allow_paid && !route.allow_paid &&
        !confirm(`When "${route.label}" is spent, continue on "${target.label}", which may cost money?`)) {
      e.target.value = route.then_route || '';
      return;
    }
    route.then_route = e.target.value;
  }
  await saveRouteCandidates(key, route.candidates);
  toast('Route updated');
});

/* ---------------------------------------------------------------- tabs -- */

$$('nav button').forEach(btn => btn.addEventListener('click', () => {
  if (btn.dataset.tab === 'projects') loadProjectsTab();
  if (btn.dataset.tab === 'studio') { renderProjectSelects(); studioShowFields(); loadGallery(); loadStudioBackends(); }
  if (btn.dataset.tab === 'providers') loadQuickSetup();
  if (btn.dataset.tab === 'settings') loadBehaviour();
}));

(async function bootV2() {
  await loadProjectsData();
  // app.js boots in parallel; give it a moment to render the first chat.
  setTimeout(firstRunHint, 1200);
})();
