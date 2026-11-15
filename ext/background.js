/* grabdown 下载助手 — 后台服务（MV3 service worker）
 *
 * 职责：
 *   1. 跟本地 agent 通信：REST（提交/查询）+ WebSocket（实时进度）
 *   2. 自动捕获：content 脚本发现 m3u8 就上报，这里去重后自动提交（关掉弹窗也生效）
 *   3. 右键菜单、工具栏徽标、离线回退 chrome.downloads
 */

const DEFAULTS = {
  agentUrl: 'http://127.0.0.1:8899',
  token: '',
  quality: 'best',
  autoCapture: false,    // 监听所有标签页，自动把 m3u8 交给 agent
  autoAll: false,        // true = mp4 也自动下（默认只自动下 m3u8）
};

const DEFAULT_AGENT = 'http://127.0.0.1:8899';

/**
 * 把用户可能填的各种写法统一成 http(s) base：
 *   ws://127.0.0.1:8899/ws → http://127.0.0.1:8899
 *   127.0.0.1:8899         → http://127.0.0.1:8899
 *   http://host:8899/      → http://host:8899
 */
function normalizeAgentUrl(raw) {
  let s = String(raw == null ? '' : raw).trim();
  if (!s) return DEFAULT_AGENT;
  s = s.replace(/^wss?:\/\//i, (m) => (m.toLowerCase().startsWith('wss') ? 'https://' : 'http://'));
  if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
  s = s.replace(/\/+$/, '');
  s = s.replace(/\/ws$/i, '');
  s = s.replace(/\/+$/, '');
  return s || DEFAULT_AGENT;
}

const seenKey = 'grabSeen';   // storage.session：已自动提交过的 URL
const logKey = 'grabAutoLog'; // storage.session：自动捕获记录

async function settings() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  const merged = { ...DEFAULTS, ...s };
  merged.agentUrl = normalizeAgentUrl(merged.agentUrl);
  return merged;
}

function wsUrl(agentUrl) {
  return normalizeAgentUrl(agentUrl).replace(/^http/, 'ws') + '/ws';
}

/**
 * 收集目标站点 cookie。
 * PH 的 m3u8 签名是和登录态绑的：只带几个年龄确认 cookie，CDN 会给 410/403；
 * 带上浏览器里的会话 cookie，重新解析页面才拿得到新鲜可用的链接。
 */
async function cookiesFor(url) {
  if (!chrome.cookies?.getAll) return '';
  const out = new Map();
  const hosts = [];
  try {
    const h = new URL(url).hostname;
    hosts.push(h.replace(/^www\./, ''));
    const parts = h.split('.');
    for (let i = 1; i < parts.length - 1; i++) hosts.push(parts.slice(i).join('.'));
  } catch {
    return '';
  }
  for (const domain of [...new Set(hosts)]) {
    try {
      const list = await chrome.cookies.getAll({ domain });
      for (const c of list) out.set(c.name, c.value);
    } catch {}
  }
  // PH 的会话 cookie 在 .pornhub.com 上，CDN 校验时会用到
  if (/pornhub|phncdn/i.test(url)) {
    try {
      for (const c of await chrome.cookies.getAll({ domain: 'pornhub.com' })) out.set(c.name, c.value);
    } catch {}
  }
  return [...out].map(([k, v]) => `${k}=${v}`).join('; ');
}

// 与 grabdown/platforms.py 对齐的轻量识别（只用于给 kind 打标，真正的判断在 agent 里）
const PLATFORMS = [
  ['bilibili', /bilibili\.com|b23\.tv/], ['douyin', /douyin\.com|iesdouyin/],
  ['kuaishou', /kuaishou\.com/], ['ixigua', /ixigua\.com/], ['weibo', /weibo\.(com|cn)/],
  ['xiaohongshu', /xiaohongshu\.com|xhslink/], ['youtube', /youtube\.com|youtu\.be/],
  ['tiktok', /tiktok\.com/], ['twitter', /twitter\.com|x\.com/], ['instagram', /instagram\.com/],
  ['vimeo', /vimeo\.com/], ['twitch', /twitch\.tv/], ['soundcloud', /soundcloud\.com/],
  ['pornhub', /pornhub\.com|phncdn\.com/], ['xvideos', /xvideos\.com/], ['xnxx', /xnxx\.com/],
];

function platformOf(url) {
  try {
    const host = new URL(url).hostname;
    for (const [key, re] of PLATFORMS) if (re.test(host)) return key;
  } catch {}
  return '';
}

async function api(path, init = {}, timeout = 4000) {
  const { agentUrl, token } = await settings();
  const base = String(agentUrl || '').replace(/\/+$/, '');
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  try {
    const res = await fetch(base + path, {
      ...init,
      signal: ctrl.signal,
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { 'X-Grab-Token': token } : {}),
        ...(init.headers || {}),
      },
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok && data.ok !== false, status: res.status, data };
  } catch (e) {
    return { ok: false, offline: true, error: String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
}

/* ---------------- 徽标：在线状态 + 运行中任务数 ---------------- */

async function badge() {
  if (!chrome.action) return { online: false };   // 连 action 都没有就先别管徽标
  const r = await api('/ping', {}, 2500);
  if (!r.ok) {
    await chrome.action.setBadgeText({ text: '离线' });
    await chrome.action.setBadgeBackgroundColor({ color: '#f87171' });
    await chrome.action.setTitle({ title: 'grabdown 离线：先运行 python -m grabdown agent' });
    return { online: false };
  }
  const jobs = await api('/jobs', {}, 2500);
  const running = (jobs.data?.jobs || []).filter((j) => j.state === 'running' || j.state === 'queued').length;
  await chrome.action.setBadgeText({ text: running ? String(running) : '' });
  await chrome.action.setBadgeBackgroundColor({ color: '#34d399' });
  await chrome.action.setTitle({ title: running ? `grabdown：${running} 个任务进行中` : 'grabdown 下载助手（在线）' });
  return { online: true, running };
}

// 所有 chrome.* 调用都做存在性检查：
// 少一个权限就整段挂掉的话，用户只会看到「点了没反应」，排查成本极高。
const has = (path) => path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), chrome) != null;

chrome.runtime.onInstalled.addListener(async () => {
  try {
    chrome.contextMenus?.create?.({ id: 'grab-link', title: '用 grabdown 下载此链接', contexts: ['link'] });
    chrome.contextMenus?.create?.({ id: 'grab-media', title: '用 grabdown 下载此媒体', contexts: ['video', 'audio'] });
  } catch (e) {
    console.warn('[grabdown] contextMenus 不可用：', e && e.message);
  }
  try {
    chrome.alarms?.create?.('ping', { periodInMinutes: 0.5 });
    if (!chrome.alarms) console.warn('[grabdown] 缺 alarms 权限，徽标不会自动刷新（不影响下载）');
  } catch (e) {
    console.warn('[grabdown] alarms 创建失败：', e && e.message);
  }
  badge();
});

chrome.alarms?.onAlarm?.addListener((a) => {
  if (a.name === 'ping') badge();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'sync' && (changes.autoCapture || changes.agentUrl || changes.token)) {
    // 通知所有标签页刷新自己的自动捕获状态
    chrome.tabs.query({}, (tabs) => {
      for (const t of tabs) {
        if (t.id != null) chrome.tabs.sendMessage(t.id, { type: 'config-changed' }).catch(() => {});
      }
    });
  }
});

/* ---------------- 右键菜单 ---------------- */

chrome.contextMenus?.onClicked?.addListener(async (info) => {
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  const kind = /\.m3u8(\?|$)/i.test(url) ? 'hls' : 'file';
  const r = await api('/jobs', {
    method: 'POST',
    body: JSON.stringify({ url, kind, quality: (await settings()).quality, referer: info.pageUrl || '' }),
  });
  if (!r.ok) {
    if (r.offline && kind === 'file') {
      chrome.downloads.download({ url });
      notify('agent 未启动，已改用浏览器直接下载（无多线程）');
    } else {
      notify('提交失败：' + (r.error || r.data?.error || '未知错误'));
    }
  } else {
    notify('已交给 grabdown 下载');
  }
});

function notify(message) {
  try {
    chrome.notifications?.create?.({
      type: 'basic',
      iconUrl: 'icons/icon128.png',
      title: 'grabdown',
      message,
    });
  } catch (e) {
    console.warn('[grabdown] 通知发不出去：', e && e.message);
  }
}

/* ---------------- 自动捕获（跨标签页） ---------------- */

async function rememberSeen(url) {
  const { [seenKey]: seen = {} } = await chrome.storage.session.get(seenKey);
  if (seen[url]) return false;
  seen[url] = Date.now();
  const keys = Object.keys(seen);
  if (keys.length > 600) {
    for (const k of keys.slice(0, 300)) delete seen[k];
  }
  await chrome.storage.session.set({ [seenKey]: seen });
  return true;
}

async function rememberLog(entry) {
  const { [logKey]: log = [] } = await chrome.storage.session.get(logKey);
  log.unshift(entry);
  await chrome.storage.session.set({ [logKey]: log.slice(0, 40) });
}

async function autoSubmit(items, tabUrl) {
  const { autoCapture, autoAll, quality } = await settings();
  if (!autoCapture) return { ok: true, skipped: 'auto-capture off' };
  const picked = (items || []).filter((it) => (autoAll ? true : it.kind === 'hls'));
  if (!picked.length) return { ok: true, skipped: 'no hls' };

  const sent = [];
  for (const it of picked.slice(0, 6)) {
    if (!(await rememberSeen(it.url))) continue;
    const r = await api('/jobs', {
      method: 'POST',
      body: JSON.stringify({
        url: it.url,
        kind: it.kind || 'hls',
        quality,
        referer: it.referer || tabUrl || '',
        cookie: await cookiesFor(it.url),
      }),
    });
    const entry = {
      url: it.url,
      title: it.title || '',
      page: tabUrl || '',
      ts: Date.now(),
      ok: r.ok,
      offline: !!r.offline,
      jobId: r.data?.job?.id || '',
      error: r.ok ? '' : (r.error || r.data?.error || ''),
    };
    await rememberLog(entry);
    sent.push(entry);
    if (r.offline) break;
  }
  if (sent.length) badge();
  return { ok: sent.every((s) => s.ok), sent };
}

/* ---------------- 消息路由 ---------------- */

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg?.type) {
      case 'ping': {
        const r = await api('/ping', {}, 2000);
        sendResponse({ ok: r.ok, offline: !!r.offline, data: r.data || null, error: r.error, ws: wsUrl((await settings()).agentUrl) });
        break;
      }
      case 'jobs': {
        sendResponse(await api('/jobs'));
        break;
      }
      case 'download': {
        const items = Array.isArray(msg.items) ? msg.items : [msg.item];
        const sent = [];
        for (const it of items.slice(0, 10)) {
          const r = await api('/jobs', {
            method: 'POST',
            body: JSON.stringify({
              url: it.url,
              kind: it.kind || (/\.m3u8(\?|$)/i.test(it.url) ? 'hls' : (platformOf(it.url) ? 'ytdlp' : 'file')),
              quality: msg.quality || 'best',
              referer: it.referer || sender?.tab?.url || '',
              cookie: msg.cookie || (await cookiesFor(it.url)),
              name: it.title || '',
            }),
          });
          sent.push({ url: it.url, ok: r.ok, offline: !!r.offline, error: r.error || r.data?.error });
        }
        const fail = sent.filter((x) => !x.ok);
        if (fail.length && fail.every((x) => x.offline)) {
          const first = items[0];
          if (first && !/\.m3u8/i.test(first.url)) {
            chrome.downloads.download({ url: first.url });
            sendResponse({ ok: false, offline: true, message: 'agent 未启动，已改用浏览器下载' });
            break;
          }
        }
        sendResponse({ ok: fail.length === 0, offline: fail.some((x) => x.offline), sent });
        badge();
        break;
      }
      case 'auto-media': {
        const r = await autoSubmit(msg.items, msg.page);
        sendResponse(r);
        break;
      }
      case 'auto-log': {
        const { [logKey]: log = [] } = await chrome.storage.session.get(logKey);
        sendResponse({ ok: true, log });
        break;
      }
      case 'normalize': {
        sendResponse({ ok: true, url: normalizeAgentUrl(msg.value) });
        break;
      }
      case 'save-settings': {
        const url = normalizeAgentUrl(msg.agentUrl);
        await chrome.storage.sync.set({ agentUrl: url, token: String(msg.token || '') });
        const probe = await api('/ping', {}, 2000);
        sendResponse({
          ok: true,
          agentUrl: url,
          online: !!probe.ok,
          version: probe.data?.version || '',
          error: probe.error || '',
        });
        break;
      }
      case 'settings': {
        if (msg.patch) await chrome.storage.sync.set(msg.patch);
        sendResponse({ ok: true, settings: await settings() });
        break;
      }
      case 'diag': {
        sendResponse({
          ok: true,
          version: chrome.runtime.getManifest().version,
          permissions: chrome.runtime.getManifest().permissions || [],
          has: {
            alarms: has('alarms'),
            contextMenus: has('contextMenus'),
            notifications: has('notifications'),
            storage: has('storage'),
            action: has('action'),
          },
        });
        break;
      }
      case 'scan-tab': {
        const tabId = msg.tabId ?? sender?.tab?.id;
        if (tabId == null) { sendResponse({ ok: false, error: 'no tab' }); break; }
        try {
          const res = await chrome.tabs.sendMessage(tabId, { type: 'scan' });
          sendResponse({ ok: true, items: res?.items || [] });
        } catch (e) {
          sendResponse({ ok: false, error: '当前页面不允许注入（chrome:// 或商店页）' });
        }
        break;
      }
      default:
        sendResponse({ ok: false, error: 'unknown message' });
    }
  })();
  return true;
});
