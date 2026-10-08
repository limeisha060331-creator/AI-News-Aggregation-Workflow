const DEFAULTS = {
  endpoint: 'http://localhost:8787/ingest',
  headers: '{}',
  autoRead: true,
  pushFormat: 'markdown'
};

const el = {
  endpoint: document.getElementById('endpoint'),
  headers: document.getElementById('headers'),
  pushFormat: document.getElementById('pushFormat'),
  autoRead: document.getElementById('autoRead'),
  save: document.getElementById('save'),
  test: document.getElementById('test'),
  reset: document.getElementById('reset'),
  status: document.getElementById('status')
};

function setStatus(message, kind) {
  el.status.textContent = message || '';
  el.status.className = 'status' + (kind ? ` status--${kind}` : '');
}

function originPattern(url) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    return `${parsed.protocol}//${parsed.hostname}/*`;
  } catch (error) {
    return null;
  }
}

async function ensurePermission(url) {
  const origin = originPattern(url);
  if (!origin) return true;
  if (origin.startsWith('http://localhost/') || origin.startsWith('http://127.0.0.1/')) return true;
  const granted = await chrome.permissions.contains({ origins: [origin] });
  if (granted) return true;
  return chrome.permissions.request({ origins: [origin] });
}

async function load() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  el.endpoint.value = stored.endpoint || '';
  el.headers.value = stored.headers || '{}';
  el.pushFormat.value = stored.pushFormat === 'text' ? 'text' : 'markdown';
  el.autoRead.checked = stored.autoRead !== false;
}

async function save() {
  const endpoint = el.endpoint.value.trim();
  const headers = el.headers.value.trim() || '{}';

  try {
    const parsed = JSON.parse(headers);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('请求头必须是 JSON 对象。');
    }
  } catch (error) {
    setStatus('请求头不是合法 JSON：' + error.message, 'error');
    return false;
  }

  if (endpoint && !originPattern(endpoint)) {
    setStatus('接口地址必须是 http:// 或 https:// 开头的完整地址。', 'error');
    return false;
  }

  if (endpoint) {
    const granted = await ensurePermission(endpoint);
    if (!granted) {
      setStatus('没有获得访问该域名的权限，推送会失败。', 'warn');
    }
  }

  await chrome.storage.local.set({
    endpoint: endpoint,
    headers: headers,
    pushFormat: el.pushFormat.value,
    autoRead: el.autoRead.checked
  });
  setStatus('已保存 ✓', 'ok');
  return true;
}

el.save.addEventListener('click', () => save());

el.test.addEventListener('click', async () => {
  setStatus('测试中…');
  const endpoint = el.endpoint.value.trim();
  if (!endpoint) {
    setStatus('请先填写接口地址。', 'error');
    return;
  }
  try {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source: 'edge-page-reader',
        type: 'ping',
        title: '连接测试',
        url: 'about:blank',
        content: 'test',
        readAt: new Date().toISOString()
      })
    });
    setStatus(response.ok ? `连接正常（${response.status}）✓` : `接口返回 ${response.status}`, response.ok ? 'ok' : 'warn');
  } catch (error) {
    setStatus('连接失败：' + (error.message || String(error)), 'error');
  }
});

el.reset.addEventListener('click', async () => {
  el.endpoint.value = DEFAULTS.endpoint;
  el.headers.value = DEFAULTS.headers;
  el.pushFormat.value = DEFAULTS.pushFormat;
  el.autoRead.checked = DEFAULTS.autoRead;
  await save();
});

load();
