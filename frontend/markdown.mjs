function inline(text, target) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let at = 0;
  for (const m of text.matchAll(pattern)) {
    target.append(document.createTextNode(text.slice(at, m.index)));
    let node;
    if (m[0].startsWith('`')) { node = document.createElement('code'); node.textContent = m[0].slice(1, -1); }
    else if (m[0].startsWith('**')) { node = document.createElement('strong'); node.textContent = m[0].slice(2, -2); }
    else {
      const link = /^\[([^\]]+)\]\((.+)\)$/.exec(m[0]); node = document.createElement('a');
      node.textContent = link[1]; node.href = link[2]; node.target = '_blank'; node.rel = 'noopener noreferrer';
    }
    target.append(node); at = m.index + m[0].length;
  }
  target.append(document.createTextNode(text.slice(at)));
}

// Render only a small safe Markdown subset; raw HTML is always text.
export function renderMarkdown(text, target, copy) {
  const fragment = document.createDocumentFragment(); let paragraph = [], list = null;
  const flush = () => { if (paragraph.length) { const p = document.createElement('p'); inline(paragraph.join('\n'), p); fragment.append(p); paragraph = []; } list = null; };
  const lines = String(text).split('\n');
  for (let i = 0; i < lines.length; i++) {
    const s = lines[i];
    if (/^\s*```/.test(s)) {
      flush(); const language = s.trim().slice(3).trim().slice(0, 35), code = [];
      while (++i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i]);
      const box = document.createElement('div'); box.className = 'code-block';
      const head = document.createElement('div'); head.className = 'code-head';
      const lang = document.createElement('span'); lang.textContent = language || '代码';
      const button = document.createElement('button'); button.type = 'button'; button.textContent = '复制代码'; button.onclick = () => copy(code.join('\n'));
      head.append(lang, button); const pre = document.createElement('pre'), c = document.createElement('code'); c.textContent = code.join('\n'); pre.append(c); box.append(head, pre); fragment.append(box);
    } else if (/^#{1,4}\s/.test(s)) {
      flush(); const h = document.createElement('h' + Math.min(s.match(/^#+/)[0].length + 1, 4)); inline(s.replace(/^#+\s+/, ''), h); fragment.append(h);
    } else if (/^\s*(?:[-*]|\d+[.)])\s/.test(s)) {
      if (paragraph.length) flush(); const type = /^\s*\d/.test(s) ? 'ol' : 'ul';
      if (!list || list.tagName.toLowerCase() !== type) { list = document.createElement(type); fragment.append(list); }
      const li = document.createElement('li'); inline(s.replace(/^\s*(?:[-*]|\d+[.)])\s+/, ''), li); list.append(li);
    } else if (/^>\s?/.test(s)) { flush(); const q = document.createElement('blockquote'); inline(s.replace(/^>\s?/, ''), q); fragment.append(q); }
    else if (!s.trim()) flush();
    else { list = null; paragraph.push(s); }
  }
  flush(); target.replaceChildren(fragment);
}
