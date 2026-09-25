/* First run: until the "about" note in Me (About me) is filled in, show a
   one-line banner pointing there. Nothing is sent anywhere; it only reads
   /api/me on this PC. */
(async function welcome() {
  let data;
  try { data = await api('/api/me'); } catch { return; }
  if (!data || !data.setup_needed) return;
  const bar = document.createElement('div');
  bar.id = 'welcomeBar';
  bar.style.cssText = 'padding:.55rem 1rem;background:var(--accent,#3b82f6);color:#fff;' +
    'font-size:.9rem;display:flex;gap:.75rem;align-items:center;flex-wrap:wrap';
  bar.innerHTML = '<span>👋 Welcome! Tell AEGIS about yourself so answers fit you: open the ' +
    '<b>Me</b> tab and fill in <b>about</b> and <b>working-style</b> (replace the {{…}} parts).</span>' +
    '<button class="sm" id="welcomeGo">Open Me</button>' +
    '<button class="sm ghost" id="welcomeLater" style="color:#fff">Later</button>';
  document.body.prepend(bar);
  $('#welcomeGo').addEventListener('click', () => {
    document.querySelector('nav button[data-tab="me"]')?.click();
    bar.remove();
  });
  $('#welcomeLater').addEventListener('click', () => bar.remove());
})();
