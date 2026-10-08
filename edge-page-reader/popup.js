import { extractPage } from './extract.js';

const DEFAULTS = {
  endpoint: 'http://localhost:8787/ingest',
  headers: '{}',
  autoRead: true,
  pushFormat: 'markdown'
};

const BLOCKED_URL_RE = /^(edge|chrome|about|devtools|view-source|chrome-extension|edge-extension|extension|moz-extension|data|blob):/i;
const STORE_URL_RE = /(chrome\.google\.com\/webstore|microsoftedge\.microsoft\.com\/addons|microsoftedge\.microsoft\.com\/extensions)/i;

const el = {
  siteName: document.getElementById('siteName'),
  pageTitle: document.getElementById('pageTitle'),
  pageMeta: document.getElementById('pageMeta'),
  format: document.getElementById('format'),
  preview: document.getElementById('preview'),
  copy: document.getElementById('copy'),
  download: document.getElementById('download'),
  push: document.getElementById('push'),
  reload: document.getElementById('reload'),
  status: document.getElementById('status'),
  openOptions: document.getElementById('openOptions')
};

let settings = Object.assign({}, DEFAULTS);
let page = null;
let statusTimer = null;

function setStatus(message, kind) {
  el.status.textContent = message || '';
  el.status.className = 'status' + (kind ? ` status--${kind}` : '');
  if (statusTimer) clearTimeout(statusTimer);
  if (message) {
    statusTimer = setTimeout(() => {
      el.status.textContent = '';
      el.status.className = 'status';
    }, 4000);
  }
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function buildJson(currentPage) {
  return {
    source: 'edge-page-reader',
    title: currentPage.title,
    url: currentPage.url,
    canonicalUrl: currentPage.canonicalUrl,
    siteName: currentPage.siteName,
    author: currentPage.author,
    publishedTime: currentPage.publishedTime,
    description: currentPage.description,
    charCount: currentPage.charCount,
    readAt: currentPage.readAt,
    content: currentPage.markdown
  };
}

function currentValue() {
  const format = el.format.value;
  if (format === 'text') return el.preview.value;
  if (format === 'json') return JSON.stringify(buildJson(page), null, 2);
  return el.preview.value;
}

function renderPage() {
  if (!page) return;
  el.siteName.textContent = page.siteName || '当前网页';
  el.pageTitle.textContent = page.title || page.docTitle || '(无标题)';

  const meta = [];
  if (page.charCount) meta.push(`正文 ${page.charCount.toLocaleString('zh-CN')} 字`);
  if (page.imageCount) meta.push(`图片 ${page.imageCount}`);
  if (page.author) meta.push(page.author);
  const published = formatDate(page.publishedTime);
  if (published) meta.push(published);
  el.pageMeta.textContent = meta.join(' · ');

  el.preview.value = page.markdown || page.text || '';
  if (!page.markdown && !page.text) setStatus('没有抽到正文，可试试用鼠标选中正文后再点“复制”。', 'warn');

  const hasSelection = Boolean(page.selection);
  const selectionOption = el.format.querySelector('option[value="selection"]');
  selectionOption.disabled = !hasSelection;
  selectionOption.textContent = hasSelection
    ? `选中内容（${page.selection.length} 字）`
    : '选中内容（无）';
}

async function grabPage() {
  setStatus('读取中…');
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = tabs && tabs[0];
  if (!tab || typeof tab.id !== 'number') throw new Error('没有找到活动标签页。');

  const url = tab.url || '';
  if (BLOCKED_URL_RE.test(url) || STORE_URL_RE.test(url)) {
    throw new Error('浏览器内置页面、扩展商店页面无法读取，请切换到普通网页。');
  }

  let results;
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractPage
    });
  } catch (error) {
    throw new Error('注入失败：该页面可能是 PDF 阅读器或受保护页面。');
  }

  const result = results && results[0] && results[0].result;
  if (!result || !result.ok) throw new Error('页面返回了空内容。');
  page = result;
  return result;
}

async function reload(options) {
  const silent = options && options.silent;
  try {
    await grabPage();
    renderPage();
    if (!silent) setStatus('已读取 ✓', 'ok');
  } catch (error) {
    page = null;
    el.siteName.textContent = '无法读取';
    el.pageTitle.textContent = '当前页面不支持读取';
    el.pageMeta.textContent = '';
    el.preview.value = '';
    setStatus(error.message || String(error), 'error');
  }
}

async function copyToClipboard(text) {
  if (!text) throw new Error('没有可复制的内容。');
  try {
    await navigator.clipboard.writeText(text);
    return;
  } catch (error) {
    const scratch = document.createElement('textarea');
    scratch.value = text;
    scratch.style.position = 'fixed';
    scratch.style.opacity = '0';
    document.body.appendChild(scratch);
    scratch.select();
    const ok = document.execCommand('copy');
    scratch.remove();
    if (!ok) throw new Error('复制失败，请手动选中后复制。');
  }
}

function parseHeaders(raw) {
  const value = String(raw || '').trim();
  if (!value) return {};
  try {
    const parsed = JSON.parse(value);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
  } catch (error) {
    throw new Error('设置里的请求头不是合法 JSON。');
  }
  throw new Error('设置里的请求头必须是 JSON 对象。');
}

function safeFileName(title) {
  const base = String(title || 'page').replace(/[\\/:*?"<>|]/g, '_').trim() || 'page';
  return `${base.slice(0, 80)}.md`;
}

async function pushToEndpoint() {
  if (!page) throw new Error('还没有读取到页面内容。');
  if (!settings.endpoint) throw new Error('请先在设置里填写接口地址。');

  const format = el.format.value;
  const body = {
    source: 'edge-page-reader',
    title: page.title,
    url: page.url,
    canonicalUrl: page.canonicalUrl,
    siteName: page.siteName,
    author: page.author,
    publishedTime: page.publishedTime,
    description: page.description,
    charCount: page.charCount,
    readAt: page.readAt,
    format: format,
    content: currentValue()
  };

  const response = await fetch(settings.endpoint, {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, parseHeaders(settings.headers)),
    body: JSON.stringify(body)
  });
  if (!response.ok) throw new Error(`接口返回 ${response.status}`);
  return response;
}

el.reload.addEventListener('click', () => reload());

el.format.addEventListener('change', () => {
  setStatus('');
  if (!page) return;
  el.preview.value = el.format.value === 'text'
    ? (page.text || '')
    : (el.format.value === 'selection' ? (page.selection || '') : (page.markdown || ''));
});

el.copy.addEventListener('click', async () => {
  try {
    const value = currentValue();
    await copyToClipboard(value);
    setStatus(`已复制 ${value.length.toLocaleString('zh-CN')} 字符 ✓`, 'ok');
  } catch (error) {
    setStatus(error.message || String(error), 'error');
  }
});

el.download.addEventListener('click', async () => {
  try {
    if (!page) throw new Error('还没有读取到页面内容。');
    const blob = new Blob([page.markdown || el.preview.value], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    await chrome.downloads.download({ url: url, filename: safeFileName(page.title), saveAs: true });
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    setStatus('已开始下载 ✓', 'ok');
  } catch (error) {
    setStatus(error.message || String(error), 'error');
  }
});

el.push.addEventListener('click', async () => {
  setStatus('推送中…');
  try {
    const response = await pushToEndpoint();
    setStatus(`推送成功（${response.status}）✓`, 'ok');
  } catch (error) {
    setStatus(error.message || String(error), 'error');
  }
});

el.openOptions.addEventListener('click', (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
});

async function init() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  settings = Object.assign({}, DEFAULTS, stored);
  el.format.value = settings.pushFormat === 'text' ? 'text' : 'markdown';

  if (settings.autoRead) {
    await reload({ silent: true });
    if (page) setStatus('');
  } else {
    el.siteName.textContent = '未读取';
    el.pageTitle.textContent = '点击“重新读取”开始';
  }
}

init();
