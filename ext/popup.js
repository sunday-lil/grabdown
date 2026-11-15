/* grabdown popup：扫描 → 提交 → WebSocket 实时进度 → 自动捕获开关
 *
 * 三条硬规则（都是踩过坑换来的）：
 *   1. send() 永远不会挂死：2.5s/4s 超时 + 同步异常兜住，按钮一定要给反馈
 *   2. WS 连不上就退回轮询，不阻塞任何功能
 *   3. 保存走一次往返（规范化 + 落盘 + 探活都在后台做）
 */

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let items = [];
let cfg = {};
let jobs = [];
let ws = null;
let wsRetry = null;
let pingRetry = null;
let pollTimer = null;
let backendStale = false;

/* ---------------- 基础 ---------------- */

function toast(text) {
  const t = $('#toast');
  t.textContent = text;
  t.classList.add('on');
  clearTimeout(t._t);
  t._t = setTimeout(() => t.classList.remove('on'), 2200);
}

function send(msg, timeout = 2500) {
  // 绝不挂死：后台休眠 / 扩展刚重载 / 抛同步异常，都要在超时内返回
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (!done) {
        done = true;
        resolve(v);
      }
    };
    const timer = setTimeout(() => finish({ ok: false, error: 'timeout' }), timeout);
    try {
      chrome.runtime.sendMessage(msg, (r) => {
        clearTimeout(timer);
        const err = chrome.runtime.lastError;
        if (err) return finish({ ok: false, error: err.message || String(err) });
        finish(r || { ok: false, error: 'no response' });
      });
    } catch (e) {
      clearTimeout(timer);
      finish({ ok: false, error: String((e && e.message) || e) });
    }
  });
}

function explain(err) {
  const s = String(err || '');
  if (/context invalidated|Receiving end does not exist/i.test(s)) return '扩展刚被重新加载，关掉面板重开一次';
  if (s === 'timeout') return '扩展后台没响应（可能已休眠），重开面板试试';
  return s || '未知错误';
}

window.addEventListener('unhandledrejection', (e) => {
  toast('内部错误：' + explain(e.reason && (e.reason.message || e.reason)));
});

function shortName(url) {
  try {
    const u = new URL(url);
    const last = decodeURIComponent(u.pathname.split('/').filter(Boolean).pop() || u.hostname);
    return last.length > 46 ? last.slice(0, 43) + '…' : last;
  } catch {
    return String(url).slice(0, 46);
  }
}

function fmtBytes(n) {
  if (!n) return '0B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return v.toFixed(v >= 100 ? 0 : 1) + u[i];
}

/* ---------------- 本页（平台识别） ---------------- */

const PLATFORMS = [
  ['哔哩哔哩', /bilibili\.com|b23\.tv/], ['抖音', /douyin\.com|iesdouyin/],
  ['快手', /kuaishou\.com/], ['西瓜视频', /ixigua\.com/], ['微博', /weibo\.(com|cn)/],
  ['小红书', /xiaohongshu\.com|xhslink/], ['YouTube', /youtube\.com|youtu\.be/],
  ['TikTok', /tiktok\.com/], ['X', /twitter\.com|x\.com/], ['Instagram', /instagram\.com/],
  ['Vimeo', /vimeo\.com/], ['Twitch', /twitch\.tv/], ['SoundCloud', /soundcloud\.com/],
  ['PornHub', /pornhub\.com|phncdn\.com/], ['XVideos', /xvideos\.com/], ['XNXX', /xnxx\.com/],
];

function platformName(url) {
  try {
    const host = new URL(url).hostname;
    for (const [name, re] of PLATFORMS) if (re.test(host)) return name;
  } catch {}
  return '普通网页';
}

let pageUrl = '';

async function loadPage() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    pageUrl = (tab && tab.url) || '';
    if (!pageUrl || /^(chrome|edge|about|chrome-extension):/i.test(pageUrl)) {
      $('#pageUrl').textContent = '这个页面不能下载（浏览器内部页）';
      $('#pagePlat').textContent = '—';
      return;
    }
    $('#pageUrl').textContent = pageUrl;
    $('#pageUrl').title = pageUrl;
    $('#pagePlat').textContent = platformName(pageUrl);
    try {
      $('#pageHost').textContent = new URL(pageUrl).hostname;
    } catch {}
    $('#grabPage').disabled = false;
  } catch (e) {
    $('#pageUrl').textContent = '读取失败：' + (e.message || e);
  }
}

/* ---------------- 本页媒体 ---------------- */

function renderItems() {
  $('#count').textContent = String(items.length);
  $('#send').disabled = items.length === 0;
  const box = $('#list');
  if (!items.length) {
    box.innerHTML = `<div class="empty">没有发现 m3u8 / mp4。<br />播放一下视频再点「重新扫描」。</div>`;
    return;
  }
  box.innerHTML = items
    .map(
      (it, i) => `<div class="item">
        <input type="checkbox" data-i="${i}" checked />
        <span class="badge ${it.kind === 'hls' ? '' : 'mp4'}">${it.kind === 'hls' ? 'HLS' : 'MP4'}</span>
        <span class="name" title="${esc(it.url)}">${esc(shortName(it.url))}</span>
      </div>`,
    )
    .join('');
  box.querySelectorAll('input[data-i]').forEach((cb) => {
    cb.onchange = () => {
      const any = [...box.querySelectorAll('input[data-i]')].some((x) => x.checked);
      $('#send').disabled = !any;
    };
  });
}

async function scan() {
  const r = await send({ type: 'scan-tab' });
  if (!r.ok) {
    $('#list').innerHTML = `<div class="empty">${esc(r.error || '无法读取当前页面')}</div>`;
    $('#count').textContent = '0';
    $('#send').disabled = true;
    return;
  }
  items = r.items || [];
  renderItems();
}

/* ---------------- 在线状态 ---------------- */

async function ping() {
  const r = await send({ type: 'ping' });
  const st = $('#status');
  st.classList.toggle('on', !!r.ok);
  st.classList.toggle('off', !r.ok);
  $('#statusLabel').textContent = r.ok ? '在线' : '离线';
  $('#statusText').textContent = r.ok
    ? 'agent ' + (r.data?.version || '') + ' · 已连接' + (backendStale ? '（后台较旧，建议刷新扩展）' : '')
    : 'agent 未启动，展开「agent 设置」看命令';
  if (r.ok) {
    clearTimeout(pingRetry);
    connectWs(r.ws);
  } else {
    clearTimeout(pingRetry);
    pingRetry = setTimeout(() => ping(), 3000);
  }
  return r;
}

/* ---------------- WebSocket ---------------- */

function setWs(text, ok) {
  const el = $('#wsState');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('ok', !!ok);
}

function connectWs(url) {
  url = url || (cfg.agentUrl ? String(cfg.agentUrl).replace(/^http/, 'ws').replace(/\/+$/, '') + '/ws' : 'ws://127.0.0.1:8899/ws');
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) && ws.url === url) return;
  try {
    ws = new WebSocket(url);
  } catch (e) {
    setWs('WS 连接失败', false);
    return;
  }
  ws.onopen = () => {
    setWs('WS 已连接 · 实时', true);
    stopPolling();
  };
  ws.onmessage = (ev) => {
    let m;
    try {
      m = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (m.type === 'hello') {
      jobs = (m.jobs || []).slice();
      renderJobs();
    } else if (m.type === 'job' && m.job) {
      upsertJob(m.job);
      if (m.job.state === 'done' || m.job.state === 'error') ping();
    }
  };
  ws.onclose = () => {
    setWs('WS 断开 · 退回轮询（点我重连）', false);
    startPolling();
    clearTimeout(wsRetry);
    wsRetry = setTimeout(() => ping(), 4000);
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch {}
  };
  clearTimeout(ws._watchdog);
  ws._watchdog = setTimeout(() => {
    if (ws && ws.readyState !== WebSocket.OPEN) {
      setWs('WS 超时 · 已退回轮询（点我重连）', false);
      startPolling();
      try {
        ws.close();
      } catch {}
    }
  }, 3500);
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(loadJobs, 1200);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

/* ---------------- 任务 ---------------- */

function jobRow(j) {
  const pct = j.state === 'done' ? 100 : j.percent || 0;
  const size = j.total_bytes ? `${fmtBytes(j.done_bytes)} / ${fmtBytes(j.total_bytes)}` : '';
  const label = { queued: '排队', running: '下载中', done: '完成', error: '失败', cancelled: '已取消' }[j.state] || j.state;
  return `<div class="job" data-id="${esc(j.id)}">
    <div class="top">
      <span class="st ${esc(j.state)}">${esc(label)}</span>
      <span class="nm" title="${esc(j.out_path || j.url)}">${esc(j.title || shortName(j.url))}</span>
      <span class="sz">${esc(size)}</span>
    </div>
    <div class="bar"><i style="width:${pct}%"></i></div>
    ${j.error ? `<div class="errline">${esc(j.error)}</div>` : ''}
  </div>`;
}

function renderJobs() {
  const box = $('#jobs');
  const list = jobs.slice(0, 6);
  box.innerHTML = list.length ? list.map(jobRow).join('') : `<div class="empty">还没有任务</div>`;
}

function upsertJob(j) {
  const i = jobs.findIndex((x) => x.id === j.id);
  if (i >= 0) jobs[i] = j;
  else jobs.unshift(j);
  jobs.sort((a, b) => (b.created || 0) - (a.created || 0));
  renderJobs();
}

async function loadJobs() {
  const r = await send({ type: 'jobs' });
  if (!r.ok) {
    $('#jobs').innerHTML = `<div class="empty">agent 离线，看不到任务</div>`;
    return r;
  }
  const remote = r.data?.jobs || [];
  jobs = remote.slice();
  renderJobs();
  const running = remote.filter((j) => j.state === 'running' || j.state === 'queued').length;
  $('#live').textContent = running ? `${running} 个进行中` : '';
  return r;
}

/* ---------------- 自动捕获 ---------------- */

async function loadAutoLog() {
  const r = await send({ type: 'auto-log' });
  const log = (r && r.log) || [];
  $('#autoCount').textContent = String(log.length);
  const box = $('#autolog');
  if (!log.length) {
    box.innerHTML = cfg.autoCapture
      ? `<div class="empty">已开启，正在监听所有标签页…</div>`
      : `<div class="empty">开着上面的开关，页面里的 m3u8 会自动进来</div>`;
    return;
  }
  box.innerHTML = log
    .slice(0, 6)
    .map(
      (e) => `<div class="job">
        <div class="top">
          <span class="st ${e.ok ? 'done' : e.offline ? 'error' : 'running'}">${e.ok ? '已提交' : e.offline ? '离线' : '失败'}</span>
          <span class="nm" title="${esc(e.url)}">${esc(shortName(e.url))}</span>
        </div>
      </div>`,
    )
    .join('');
}

/* ---------------- 交互 ---------------- */

$('#send').onclick = async () => {
  const chosen = [...document.querySelectorAll('#list input[data-i]')]
    .filter((c) => c.checked)
    .map((c) => items[Number(c.dataset.i)]);
  if (!chosen.length) return;
  $('#send').disabled = true;
  let r;
  try {
    r = await send({ type: 'download', items: chosen, quality: $('#quality').value });
  } finally {
    $('#send').disabled = false;
  }
  if (r && r.ok) {
    toast(`已提交 ${chosen.length} 个到 grabdown`);
    loadJobs();
  } else if (r && r.offline) {
    toast('agent 未启动：先跑 AGENT.cmd');
  } else {
    toast('提交失败：' + explain(r && (r.error || r.message)));
  }
};

$('#grabPage').onclick = async () => {
  if (!pageUrl) return;
  const btn = $('#grabPage');
  btn.disabled = true;
  let r;
  try {
    r = await send({ type: 'download', items: [{ url: pageUrl, kind: 'ytdlp', title: document.title }] });
  } finally {
    btn.disabled = false;
  }
  if (r && r.ok) {
    toast('已交给 grabdown 下载本页');
    loadJobs();
  } else {
    toast('提交失败：' + explain(r && (r.error || r.message)));
  }
};

$('#rescan').onclick = scan;
$('#refreshJobs').onclick = () => loadJobs();

$('#wsState').onclick = () => {
  setWs('重连中…', false);
  ping();
  toast('重新连接 agent…');
};

$('#autoCapture').onchange = async (e) => {
  const on = e.target.checked;
  const r = await send({ type: 'settings', patch: { autoCapture: on } });
  if (!r || r.ok === false) {
    toast('切换失败：' + explain(r && r.error));
    e.target.checked = !on;
    return;
  }
  cfg = { ...cfg, ...(r.settings || {}) };
  toast(on ? '已开启：所有标签页自动捕获 m3u8' : '已关闭自动捕获');
  loadAutoLog();
};

$('#autoAllBtn').onclick = async () => {
  const next = !cfg.autoAll;
  const r = await send({ type: 'settings', patch: { autoAll: next } });
  if (!r || r.ok === false) {
    toast('切换失败：' + explain(r && r.error));
    return;
  }
  cfg = { ...cfg, ...(r.settings || {}) };
  $('#autoAllBtn').textContent = cfg.autoAll ? 'm3u8 + mp4' : '只捕 m3u8';
};

$('#copyCmd').onclick = async () => {
  try {
    await navigator.clipboard.writeText('python -m grabdown agent');
    toast('命令已复制');
  } catch {
    toast('复制失败，手动输入：python -m grabdown agent');
  }
};

$('#save').onclick = async () => {
  const btn = $('#save');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '保存中…';
  toast('正在保存…');
  try {
    // 一次往返：规范化 + 落盘 + 探活，全在后台做；超时 4s 必有结果
    const r = await send({ type: 'save-settings', agentUrl: $('#agentUrl').value, token: $('#token').value }, 4000);
    if (!r || r.ok === false) {
      toast('保存失败：' + explain(r && r.error));
      return;
    }
    $('#agentUrl').value = r.agentUrl;
    if (r.online) {
      toast('已保存，已连上 ' + r.agentUrl);
      await ping();
      await loadJobs();
    } else {
      toast('已保存，但连不上 ' + r.agentUrl + ' —— 先跑 AGENT.cmd');
      setWs('WS 未连接（agent 离线）', false);
    }
  } catch (e) {
    toast('保存异常：' + explain(e && (e.message || e)));
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
};

/* ---------------- 后台自检 ---------------- */

async function diagnose() {
  const el = $('#diag');
  const r = await send({ type: 'diag' }, 1500);
  if (!r || r.ok === false) {
    el.className = 'diag bad';
    el.textContent = '后台没起来（' + explain(r && r.error) + '）→ edge://extensions 点刷新，版本应为 1.0.4';
    $('#statusText').textContent = '扩展后台未运行：点扩展页的刷新按钮';
    return false;
  }
  const h = r.has || {};
  const miss = Object.entries(h).filter(([, v]) => !v).map(([k]) => k);
  el.className = 'diag' + (miss.length ? ' bad' : ' ok');
  el.textContent = `后台 v${r.version} · 权限 ${(r.permissions || []).join(', ') || '无'}` +
    (miss.length ? ` · 缺失: ${miss.join(', ')}` : ' · 正常');
  return true;
}

/* ---------------- 启动 ---------------- */

(async function init() {
  loadPage();                 // 平台识别不依赖后台，先渲染出来
  // 先自检：SW 没起来就别在别的调用上白等
  const alive = await diagnose();
  if (!alive) {
    $('#agentUrl').value = 'http://127.0.0.1:8899';
    $('#count').textContent = '0';
    $('#send').disabled = true;
    $('#list').innerHTML = `<div class="empty">扩展后台没运行，先到 edge://extensions 点刷新（版本 1.0.4）</div>`;
    return;
  }

  const r = await send({ type: 'settings' });
  if (r && r.ok) cfg = r.settings || {};
  const norm = await send({ type: 'normalize', value: cfg.agentUrl || '' });
  if (!norm || norm.ok === false) backendStale = true;
  cfg.agentUrl = (norm && norm.url) || cfg.agentUrl || 'http://127.0.0.1:8899';

  $('#agentUrl').value = cfg.agentUrl;
  $('#token').value = cfg.token || '';
  $('#quality').value = cfg.quality || 'best';
  $('#autoCapture').checked = !!cfg.autoCapture;
  $('#autoAllBtn').textContent = cfg.autoAll ? 'm3u8 + mp4' : '只捕 m3u8';

  await ping();
  await scan();
  const j = await loadJobs();
  if (!j || !j.ok) startPolling();
  await loadAutoLog();
  setInterval(loadAutoLog, 3000);
})();
