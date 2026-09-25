/* AEGIS v2.2: Needle fast intent.
   Before a short plain message goes to a model, ask the local Needle engine
   whether it is really one of AEGIS's own commands. If it is (and the server's
   gates all agree), the message becomes that slash command and runs exactly as
   if you had typed it. Any doubt, error or slowness: the message is sent to the
   model unchanged. Uses helpers from app.js and v21.js. */

state.needleOn = false;

async function needleRefresh() {
  try {
    const s = await api('/api/intent/status');
    state.needleOn = !!(s.enabled && s.installed);
    const el = $('#needleStatus');
    if (el) {
      el.textContent = !s.installed
        ? ' Not installed yet: in the AEGIS folder run  .venv\\Scripts\\python.exe -m pip install cactus-needle==3.0.4'
        : s.error ? ` Last error: ${s.error}`
        : s.loaded ? ` Ready${s.version ? ' (v' + s.version + ')' : ''}.`
        : ' Installed; loads on first use.';
    }
    return s;
  } catch { state.needleOn = false; return null; }
}

async function needleCheck(text) {
  const controller = new AbortController();
  // A command should feel instant. If Needle is still loading, don't make
  // the user wait: send to the model and let Needle catch the next one.
  const timer = setTimeout(() => controller.abort(), 1500);
  try {
    const res = await fetch('/api/intent', {
      method: 'POST', signal: controller.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    return data && data.matched && typeof data.slash === 'string' && data.slash.startsWith('/')
      ? data : null;
  } catch { return null; }
  finally { clearTimeout(timer); }
}

const _handleSendV21 = handleSend;
handleSend = async function () {
  const input = $('#input');
  const text = input.value.trim();
  if (text && !text.startsWith('/') && state.needleOn && !state.streaming
      && !(state.attachments || []).length) {
    const hit = await needleCheck(text);
    // Only rewrite if the box still holds what we asked about.
    if (hit && input.value.trim() === text) {
      input.value = hit.slash;
      const pct = hit.confidence != null ? ` · ${Math.round(hit.confidence * 100)}%` : '';
      toast(`⚡ Needle: ${hit.slash}${pct}`);
    }
  }
  return _handleSendV21();
};

/* v21 only routes slash commands through handleSend; send plain messages
   through it too so Needle gets a look. Registered after v21's listeners, so
   a slash command is still handled by v21 first. */
$('#input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && state.needleOn
      && !$('#input').value.trim().startsWith('/')) {
    e.preventDefault(); e.stopImmediatePropagation();
    handleSend();
  }
}, true);
$('#send').addEventListener('click', e => {
  if (state.needleOn && !$('#input').value.trim().startsWith('/')) {
    e.preventDefault(); e.stopImmediatePropagation();
    handleSend();
  }
}, true);

/* ------------------------------------------------------------ settings -- */

const _loadBehaviourV21 = loadBehaviour;
loadBehaviour = async function () {
  await _loadBehaviourV21();
  const s = (await api('/api/settings')).settings;
  $('#needleOn').checked = s.needle_enabled !== false;
  $('#needleConf').value = s.needle_min_confidence ?? 0.6;
  needleRefresh();
};
$('#saveBehaviour').addEventListener('click', async () => {
  const conf = Math.min(0.95, Math.max(0.3, Number($('#needleConf').value) || 0.6));
  await api('/api/settings', { method: 'POST', body: {
    needle_enabled: $('#needleOn').checked, needle_min_confidence: conf } });
  needleRefresh();
});
$('#needleWarm').addEventListener('click', async () => {
  $('#needleStatus').textContent = ' Preparing… the first time downloads about 30 MB.';
  try {
    const r = await fetch('/api/intent/warmup', { method: 'POST' });
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'failed');
    toast(`Needle ready in ${data.load_seconds}s`);
  } catch (err) { toast('Needle: ' + err.message, true); }
  needleRefresh();
});

needleRefresh();
