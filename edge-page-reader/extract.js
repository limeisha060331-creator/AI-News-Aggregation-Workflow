/**
 * 网页正文抽取。
 *
 * 注意：extractPage 会被 chrome.scripting.executeScript 序列化后注入到目标
 * 页面里执行，因此它必须完全自包含 —— 不能引用模块作用域里的任何变量或函数。
 */
export function extractPage() {
  const MAX_CHARS = 200000;

  const DROP_TAGS = [
    'SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'IFRAME', 'SVG', 'CANVAS',
    'VIDEO', 'AUDIO', 'FORM', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA',
    'DIALOG', 'LINK', 'META', 'OBJECT', 'EMBED'
  ];
  const NOISE_RE = /(^|[-_\s])(comment|comments|share|sharing|social|related|recommend|recommended|promo|promotion|advert|ads?|sponsor|sidebar|side-bar|footer|header|nav|navbar|menu|breadcrumb|subscribe|newsletter|paywall|popup|modal|cookie|banner|toolbar|tag|tags|rating|prev-next|pagination|hot-list|tuiguang|advertise)([-_\s]|$)/i;
  const INLINE_TAGS = ['A', 'STRONG', 'B', 'EM', 'I', 'CODE', 'SPAN', 'SUP', 'SUB', 'U', 'MARK', 'SMALL', 'TIME', 'ABBR', 'CITE', 'Q', 'S', 'DEL', 'INS', 'KBD', 'VAR', 'FONT', 'BIG', 'LABEL'];
  const BLOCK_CANDIDATE_SELECTOR = 'article, main, [role="main"], [itemprop="articleBody"], .article, .post, .entry-content, .post-content, .article-content, .content-body, .markdown-body, .rich_media_content, #js_content, #article, #content';

  const isDropped = (tag) => DROP_TAGS.indexOf(tag) !== -1;
  const isInline = (tag) => INLINE_TAGS.indexOf(tag) !== -1;

  const squash = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const textOf = (el) => (el ? squash(el.innerText || el.textContent) : '');

  function absUrl(raw) {
    if (!raw) return '';
    const value = String(raw).trim();
    if (/^(javascript|data|about|blob):/i.test(value)) return '';
    try {
      return new URL(value, document.baseURI).href;
    } catch (error) {
      return value;
    }
  }

  function metaContent(selectors) {
    for (let i = 0; i < selectors.length; i += 1) {
      const el = document.querySelector(selectors[i]);
      if (!el) continue;
      const value = el.getAttribute('content') || el.getAttribute('datetime') ||
        el.getAttribute('href') || el.textContent || '';
      const text = squash(value);
      if (text) return text;
    }
    return '';
  }

  function isVisible(el) {
    if (!el || !el.isConnected) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle(el);
    if (!style) return true;
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  }

  function classAndId(el) {
    const cls = typeof el.className === 'string' ? el.className : '';
    return squash(`${el.id || ''} ${cls}`);
  }

  function depthOf(el) {
    let depth = 0;
    let node = el;
    while ((node = node.parentElement)) depth += 1;
    return depth;
  }

  function linkDensity(el) {
    const total = textOf(el).length;
    if (!total) return 1;
    const links = el.querySelectorAll('a');
    let linkChars = 0;
    for (let i = 0; i < links.length; i += 1) linkChars += textOf(links[i]).length;
    return Math.min(linkChars / total, 1);
  }

  function scoreOf(el) {
    const text = textOf(el);
    const length = text.length;
    if (length < 140) return 0;
    const paragraphs = el.querySelectorAll('p');
    let paragraphChars = 0;
    for (let i = 0; i < paragraphs.length; i += 1) paragraphChars += textOf(paragraphs[i]).length;
    const punctuation = (text.match(/[。！？!?，,、；;.:]/g) || []).length;
    const density = linkDensity(el);
    let score = (paragraphChars + length * 0.2) * (1 - Math.min(density, 0.95));
    score *= 1 + Math.min(punctuation, 300) / 1500;
    score *= 1 + Math.min(depthOf(el), 25) / 80;
    if (paragraphs.length < 2 && el.tagName === 'DIV') score *= 0.6;
    if (/^(ARTICLE|MAIN)$/.test(el.tagName)) score *= 1.25;
    return score;
  }

  function pickRoot() {
    const seen = [];
    const push = (el) => {
      if (!el || !el.tagName || isDropped(el.tagName)) return;
      if (seen.indexOf(el) !== -1) return;
      if (el.querySelector && el.querySelector('p, li, h1, h2, h3') === null) return;
      seen.push(el);
    };

    const preferred = document.querySelectorAll(BLOCK_CANDIDATE_SELECTOR);
    for (let i = 0; i < preferred.length; i += 1) push(preferred[i]);

    const generic = document.querySelectorAll('div, section, td');
    for (let i = 0; i < generic.length; i += 1) {
      const el = generic[i];
      if (textOf(el).length < 400) continue;
      push(el);
    }
    push(document.body);

    let best = null;
    let bestScore = 0;
    const scored = [];
    for (let i = 0; i < seen.length; i += 1) {
      const score = scoreOf(seen[i]);
      if (score <= 0) continue;
      scored.push({ el: seen[i], score: score, depth: depthOf(seen[i]) });
      if (score > bestScore) {
        bestScore = score;
        best = seen[i];
      }
    }
    if (!best) return document.body;

    // 在得分接近的候选里选最深的一个，避免把整个 body 当成正文
    let chosen = best;
    let chosenDepth = depthOf(best);
    for (let i = 0; i < scored.length; i += 1) {
      const item = scored[i];
      if (item.score < bestScore * 0.8) continue;
      if (item.depth > chosenDepth) {
        chosen = item.el;
        chosenDepth = item.depth;
      }
    }
    return chosen;
  }

  function clean(root) {
    const nodes = root.querySelectorAll('*');
    for (let i = 0; i < nodes.length; i += 1) {
      const el = nodes[i];
      if (!el.isConnected && el.parentElement === null) continue;
      const name = classAndId(el);
      if (isDropped(el.tagName)) {
        el.remove();
        continue;
      }
      if (name && NOISE_RE.test(name)) {
        const ownText = textOf(el).length;
        if (ownText < 400 || linkDensity(el) > 0.5) {
          el.remove();
          continue;
        }
      }
      if (/^(NAV|ASIDE|FOOTER|HEADER)$/.test(el.tagName)) {
        el.remove();
        continue;
      }
      if (!isVisible(el)) {
        el.remove();
      }
    }
    return root;
  }

  function renderInline(node) {
    let out = '';
    const children = node.childNodes;
    for (let i = 0; i < children.length; i += 1) {
      const child = children[i];
      if (child.nodeType === 3) {
        out += String(child.nodeValue || '').replace(/\s+/g, ' ');
        continue;
      }
      if (child.nodeType !== 1) continue;
      const tag = child.tagName;
      if (isDropped(tag) || !isVisible(child)) continue;

      if (tag === 'IMG') {
        const src = absUrl(child.getAttribute('src') || child.getAttribute('data-src') || child.getAttribute('data-original'));
        if (src) out += `![${squash(child.getAttribute('alt'))}](${src})`;
        continue;
      }
      if (tag === 'BR') {
        out += '\n';
        continue;
      }

      const inner = renderInline(child);
      const trimmed = inner.trim();
      switch (tag) {
        case 'STRONG':
        case 'B':
          out += trimmed ? `**${trimmed}**` : '';
          break;
        case 'EM':
        case 'I':
          out += trimmed ? `*${trimmed}*` : '';
          break;
        case 'DEL':
        case 'S':
          out += trimmed ? `~~${trimmed}~~` : '';
          break;
        case 'CODE':
        case 'KBD':
          out += trimmed ? `\`${trimmed}\`` : '';
          break;
        case 'A': {
          const href = absUrl(child.getAttribute('href'));
          if (!trimmed) break;
          out += href ? `[${trimmed}](${href})` : trimmed;
          break;
        }
        default:
          out += inner;
      }
    }
    return out;
  }

  function renderList(listEl, depth) {
    const ordered = listEl.tagName === 'OL';
    let index = parseInt(listEl.getAttribute('start'), 10);
    if (!index || Number.isNaN(index)) index = 1;

    const lines = [];
    const items = listEl.children;
    for (let i = 0; i < items.length; i += 1) {
      const li = items[i];
      if (li.tagName !== 'LI') continue;

      const clone = li.cloneNode(true);
      const sublists = [];
      const kids = Array.prototype.slice.call(clone.children);
      for (let k = 0; k < kids.length; k += 1) {
        if (kids[k].tagName === 'UL' || kids[k].tagName === 'OL') {
          sublists.push(kids[k]);
          kids[k].remove();
        }
      }

      const body = renderBlocks(clone).replace(/\n{2,}/g, '\n').trim();
      const indent = '  '.repeat(depth);
      const marker = ordered ? `${index}. ` : '- ';
      index += 1;

      const bodyLines = body.split('\n');
      const first = squash(bodyLines.shift() || '');
      lines.push(`${indent}${marker}${first}`);
      for (let b = 0; b < bodyLines.length; b += 1) {
        const line = squash(bodyLines[b]);
        if (line) lines.push(`${indent}  ${line}`);
      }
      for (let s = 0; s < sublists.length; s += 1) {
        lines.push(renderList(sublists[s], depth + 1));
      }
    }
    return lines.join('\n');
  }

  function renderTable(table) {
    const rows = [];
    const trs = table.querySelectorAll('tr');
    for (let i = 0; i < trs.length && rows.length < 60; i += 1) {
      const cells = [];
      const tds = trs[i].querySelectorAll('th, td');
      for (let c = 0; c < tds.length; c += 1) cells.push(squash(renderInline(tds[c])).replace(/\|/g, '\\|'));
      if (cells.length) rows.push(cells);
    }
    if (rows.length < 2) return '';
    const width = Math.max.apply(null, rows.map((row) => row.length));
    if (width > 8) return '';
    const pad = (row) => {
      const copy = row.slice();
      while (copy.length < width) copy.push('');
      return `| ${copy.join(' | ')} |`;
    };
    const out = [pad(rows[0]), `| ${new Array(width).fill('---').join(' | ')} |`];
    for (let i = 1; i < rows.length; i += 1) out.push(pad(rows[i]));
    return out.join('\n');
  }

  function renderBlocks(node) {
    const parts = [];
    let buffer = '';

    const flush = () => {
      const text = buffer.replace(/[ \t]*\n[ \t]*/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
      if (text) parts.push(text);
      buffer = '';
    };

    const children = node.childNodes;
    for (let i = 0; i < children.length; i += 1) {
      const child = children[i];
      if (child.nodeType === 3) {
        const text = String(child.nodeValue || '').replace(/\s+/g, ' ');
        if (text.trim()) buffer += text;
        continue;
      }
      if (child.nodeType !== 1) continue;

      const tag = child.tagName;
      if (isDropped(tag) || !isVisible(child)) continue;

      if (isInline(tag) || tag === 'IMG' || tag === 'BR') {
        buffer += renderInline(child);
        continue;
      }

      flush();
      switch (tag) {
        case 'H1':
        case 'H2':
        case 'H3':
        case 'H4':
        case 'H5':
        case 'H6': {
          const level = Number(tag.slice(1));
          const heading = squash(renderInline(child));
          if (heading) parts.push(`${'#'.repeat(level)} ${heading}`);
          break;
        }
        case 'P': {
          const paragraph = renderInline(child).replace(/[ \t]*\n[ \t]*/g, '\n').trim();
          if (paragraph) parts.push(paragraph);
          break;
        }
        case 'BLOCKQUOTE': {
          const quoted = renderBlocks(child).trim();
          if (quoted) {
            parts.push(quoted.split('\n').map((line) => `> ${line}`.trim()).join('\n'));
          }
          break;
        }
        case 'PRE': {
          const codeEl = child.querySelector('code') || child;
          const className = typeof codeEl.className === 'string' ? codeEl.className : '';
          const langMatch = className.match(/language-([\w+#.-]+)/);
          const code = String(child.innerText || codeEl.textContent || '').replace(/\s+$/, '');
          if (code.trim()) parts.push(`\`\`\`${langMatch ? langMatch[1] : ''}\n${code}\n\`\`\``);
          break;
        }
        case 'UL':
        case 'OL':
          parts.push(renderList(child, 0));
          break;
        case 'TABLE': {
          const table = renderTable(child);
          if (table) parts.push(table);
          break;
        }
        case 'HR':
          parts.push('---');
          break;
        case 'FIGCAPTION': {
          const caption = squash(renderInline(child));
          if (caption) parts.push(`*${caption}*`);
          break;
        }
        default: {
          const inner = renderBlocks(child).trim();
          if (inner) parts.push(inner);
        }
      }
    }

    flush();
    return parts.join('\n\n');
  }

  let root;
  try {
    root = clean(pickRoot().cloneNode(true));
  } catch (error) {
    root = document.body.cloneNode(true);
  }

  let markdown = '';
  try {
    markdown = renderBlocks(root).replace(/\n{3,}/g, '\n\n').trim();
  } catch (error) {
    markdown = squash(root.innerText || root.textContent);
  }
  markdown = markdown.slice(0, MAX_CHARS);

  let text = '';
  try {
    text = String(root.innerText || root.textContent || '')
      .replace(/[ \t]+\n/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  } catch (error) {
    text = markdown;
  }
  text = text.slice(0, MAX_CHARS);

  const images = [];
  const imageNodes = root.querySelectorAll('img');
  for (let i = 0; i < imageNodes.length && images.length < 50; i += 1) {
    const src = absUrl(imageNodes[i].getAttribute('src') || imageNodes[i].getAttribute('data-src'));
    if (src) images.push(src);
  }

  let selection = '';
  try {
    const selected = window.getSelection();
    selection = selected ? String(selected).trim().slice(0, MAX_CHARS) : '';
  } catch (error) {
    selection = '';
  }

  const canonical = metaContent(['link[rel="canonical"]']);

  return {
    ok: true,
    url: location.href,
    canonicalUrl: canonical ? absUrl(canonical) : location.href,
    title: metaContent([
      'meta[property="og:title"]',
      'meta[name="twitter:title"]',
      'meta[itemprop="headline"]'
    ]) || squash(document.title),
    docTitle: squash(document.title),
    siteName: metaContent([
      'meta[property="og:site_name"]',
      'meta[name="application-name"]'
    ]) || location.hostname,
    description: metaContent([
      'meta[property="og:description"]',
      'meta[name="description"]',
      'meta[name="twitter:description"]'
    ]),
    author: metaContent([
      'meta[name="author"]',
      'meta[property="article:author"]',
      'meta[itemprop="author"]',
      '[rel="author"]'
    ]),
    publishedTime: metaContent([
      'meta[property="article:published_time"]',
      'meta[name="publishdate"]',
      'meta[name="date"]',
      'meta[itemprop="datePublished"]',
      'time[datetime]'
    ]),
    lang: document.documentElement.getAttribute('lang') || '',
    selection: selection,
    markdown: markdown,
    text: text,
    charCount: text.replace(/\s/g, '').length,
    imageCount: images.length,
    images: images,
    readAt: new Date().toISOString()
  };
}
