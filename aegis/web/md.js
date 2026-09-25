/* AEGIS v2 - a small, safe Markdown renderer. No dependencies.
   Everything is HTML-escaped first; only a fixed set of constructs is turned
   back into tags, so model output can never inject markup or script.
   Also renders [[media:ID:mime]] markers as inline image/audio/video players. */

(function () {
  const escHtml = s => String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function mediaTag(id, mime) {
    const src = `/api/media/${encodeURIComponent(id)}`;
    const dl = `<a class="media-dl" href="${src}" download>download</a>`;
    if (/^image\//.test(mime)) {
      return `<figure class="media"><img src="${src}" loading="lazy" alt="generated image" class="gen">${dl}</figure>`;
    }
    if (/^audio\//.test(mime)) {
      return `<figure class="media"><audio controls preload="metadata" src="${src}"></audio>${dl}</figure>`;
    }
    if (/^video\//.test(mime)) {
      return `<figure class="media"><video controls preload="metadata" src="${src}"></video>${dl}</figure>`;
    }
    return `<figure class="media"><a href="${src}" target="_blank">open file</a> ${dl}</figure>`;
  }

  function fileChip(path) {
    const raw = path.replace(/&amp;/g, '&').replace(/&#39;/g, "'").replace(/&quot;/g, '"');
    const name = raw.split(/[\\/]/).pop();
    return `<a class="file-chip" href="/api/files/download?path=${encodeURIComponent(raw)}" download>⬇ ${name}</a>`;
  }
  window.fileChip = fileChip;

  function inline(text) {
    // text is already escaped
    const codes = [];
    text = text.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    text = text.replace(/\[\[media:([a-f0-9]{6,32}):([a-z0-9.+\/-]*)\]\]/gi,
      (_, id, mime) => mediaTag(id, mime));
    text = text.replace(/\[\[file:([^\]]+)\]\]/g, (_, p) => fileChip(p));
    // images: only our own media URLs or https
    text = text.replace(/!\[([^\]]*)\]\(((?:\/api\/media\/|https:\/\/)[^\s)]+)\)/g,
      (_, alt, url) => `<img class="gen" src="${url}" alt="${alt}" loading="lazy">`);
    text = text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+|\/api\/media\/[^\s)]+)\)/g,
      (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    text = text.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?'"])/g,
      (_, pre, url) => `${pre}<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
    text = text.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, '$1<em>$2</em>');
    text = text.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, '$1<em>$2</em>');
    text = text.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    text = text.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
    return text;
  }

  function table(lines) {
    const row = l => l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
    const head = row(lines[0]);
    const body = lines.slice(2).map(row);
    return '<div class="md-table"><table><thead><tr>' + head.map(h => `<th>${inline(h)}</th>`).join('')
      + '</tr></thead><tbody>' + body.map(r => '<tr>' + r.map(c => `<td>${inline(c)}</td>`).join('') + '</tr>').join('')
      + '</tbody></table></div>';
  }

  function render(src) {
    src = String(src ?? '').replace(/\r\n/g, '\n');
    // <think>…</think> from reasoning models -> a collapsed block
    src = src.replace(/<think>([\s\S]*?)(<\/think>|$)/g, (_, t) => `\u0001THINK${btoa(unescape(encodeURIComponent(t)))}\u0001`);
    const lines = escHtml(src).split('\n');
    let out = '';
    let i = 0;
    let list = null;          // 'ul' | 'ol'
    let para = [];
    const flushPara = () => { if (para.length) { out += `<p>${inline(para.join('<br>'))}</p>`; para = []; } };
    const closeList = () => { if (list) { out += `</${list}>`; list = null; } };

    while (i < lines.length) {
      const line = lines[i];
      const think = line.match(/^\u0001THINK([A-Za-z0-9+/=]*)\u0001(.*)$/);
      if (think) {
        flushPara(); closeList();
        let body = '';
        try { body = decodeURIComponent(escape(atob(think[1]))); } catch { body = ''; }
        out += `<details class="think"><summary>thinking</summary><div>${escHtml(body).replace(/\n/g, '<br>')}</div></details>`;
        if (think[2]) lines[i] = think[2]; else i++;
        continue;
      }
      const fence = line.match(/^\s*```\s*([\w+#.-]*)\s*$/);
      if (fence) {
        flushPara(); closeList();
        const lang = fence[1];
        const buf = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        out += `<div class="codeblock"><div class="codehead"><span>${lang || 'code'}</span>`
          + `<button class="sm ghost copy-code" type="button">Copy</button></div>`
          + `<pre><code>${buf.join('\n')}</code></pre></div>`;
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        flushPara(); closeList();
        const buf = [line, lines[i + 1]];
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        out += table(buf);
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) { flushPara(); closeList(); const n = Math.min(h[1].length + 1, 6); out += `<h${n}>${inline(h[2])}</h${n}>`; i++; continue; }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { flushPara(); closeList(); out += '<hr>'; i++; continue; }
      const bq = line.match(/^\s*&gt;\s?(.*)$/);
      if (bq) { flushPara(); closeList(); out += `<blockquote>${inline(bq[1])}</blockquote>`; i++; continue; }
      const ul = line.match(/^(\s*)[-*+]\s+(.*)$/);
      const ol = line.match(/^(\s*)\d+[.)]\s+(.*)$/);
      if (ul || ol) {
        flushPara();
        const kind = ul ? 'ul' : 'ol';
        if (list !== kind) { closeList(); out += `<${kind}>`; list = kind; }
        let item = (ul || ol)[2];
        const task = item.match(/^\[( |x|X)\]\s+(.*)$/);
        if (task) item = `<input type="checkbox" disabled ${task[1] !== ' ' ? 'checked' : ''}> ${task[2]}`;
        const indent = (ul || ol)[1].length;
        out += `<li${indent >= 2 ? ' class="nested"' : ''}>${inline(item)}</li>`;
        i++;
        continue;
      }
      if (!line.trim()) { flushPara(); closeList(); i++; continue; }
      closeList();
      para.push(line);
      i++;
    }
    flushPara(); closeList();
    return out;
  }

  window.renderMarkdown = render;
  window.mediaTag = mediaTag;

  document.addEventListener('click', e => {
    const btn = e.target.closest('.copy-code');
    if (!btn) return;
    const code = btn.closest('.codeblock')?.querySelector('code')?.innerText || '';
    navigator.clipboard?.writeText(code).then(() => {
      btn.textContent = 'Copied'; setTimeout(() => btn.textContent = 'Copy', 1400);
    });
  });
  document.addEventListener('click', e => {
    if (e.target.classList?.contains('gen')) e.target.classList.toggle('big');
  });
})();
