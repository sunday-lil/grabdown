/* grabdown 下载助手 — 页面端
 *
 * 干三件事：
 *   1. 页面里找媒体（<video>/<source>/<a> + performance 资源条目 + 内联 m3u8 字符串）
 *   2. 右下角浮一个「grabdown」按钮，一键把当前页媒体交给本地 agent
 *   3. 响应 popup 的 scan 请求
 */

(() => {
  if (window.__grabdownInjected) return;
  window.__grabdownInjected = true;

  const MEDIA_RE = /\.(m3u8|mp4|m4a|webm|flv|ts)(\?|#|$)/i;
  const chipId = 'grabdown-chip';

  function kindOf(url) {
    return /\.m3u8(\?|#|$)/i.test(url) ? 'hls' : 'file';
  }

  function collect() {
    const map = new Map();
    const add = (url, from) => {
      if (!url) return;
      let u = String(url).trim();
      if (!u || u.startsWith('blob:') || u.startsWith('data:')) return;
      if (u.startsWith('//')) u = location.protocol + u;
      if (!/^https?:/i.test(u)) return;
      if (!MEDIA_RE.test(u)) return;
      if (map.has(u)) return;
      map.set(u, { url: u, kind: kindOf(u), from, title: document.title, referer: location.href });
    };

    // <video>/<audio>
    document.querySelectorAll('video, audio').forEach((v) => {
      if (v.currentSrc) add(v.currentSrc, 'video');
      if (v.src) add(v.src, 'video');
      v.querySelectorAll('source').forEach((s) => add(s.src, 'source'));
      // 有些站把 m3u8 塞在 data-* 上
      for (const a of v.attributes) {
        if (/^data-/.test(a.name) && MEDIA_RE.test(a.value)) add(a.value, 'video-data');
      }
    });

    // <a href>
    document.querySelectorAll('a[href]').forEach((a) => add(a.href, 'link'));

    // performance 资源（m3u8 通常在 XHR 里，DOM 上是看不到的）
    try {
      for (const e of performance.getEntriesByType('resource')) add(e.name, 'network');
    } catch (_) {}
    try {
      for (const e of performance.getEntriesByType('navigation')) add(e.name, 'nav');
    } catch (_) {}

    // 内联脚本里的 m3u8/mp4 字面量（flashvars / 播放器配置）
    try {
      const html = document.documentElement.innerHTML;
      const re = /https?:\/\/[^\s"'<>\\]+?\.(?:m3u8|mp4)(?:\?[^\s"'<>\\]*)?/gi;
      let m;
      let guard = 0;
      while ((m = re.exec(html)) && guard++ < 200) add(m[0].replace(/\\\//g, '/'), 'inline');
    } catch (_) {}

    let list = [...map.values()];
    // 有 master 就别再列它的子播放列表（PH 的 index-v1-a1.m3u8 之类），否则会重复下同一个视频
    if (list.some((it) => /\/master\.m3u8/i.test(it.url))) {
      list = list.filter((it) => !/\/index-v\d/i.test(it.url));
    }
    return list.sort((a, b) => (a.kind === b.kind ? 0 : a.kind === 'hls' ? -1 : 1)).slice(0, 30);
  }

  let cfg = { autoCapture: false, autoAll: false };

  async function loadCfg() {
    const r = await send({ type: 'settings' });
    if (r && r.settings) cfg = r.settings;
    return cfg;
  }

  function send(msg) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(msg, (r) => resolve(r || { ok: false, error: 'no response' }));
      } catch (e) {
        resolve({ ok: false, error: String(e) });
      }
    });
  }

  function toast(text, bad) {
    let el = document.getElementById('grabdown-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'grabdown-toast';
      el.style.cssText = [
        'position:fixed', 'right:20px', 'bottom:86px', 'z-index:2147483647',
        'padding:10px 14px', 'border-radius:11px', 'font:13px/1.5 -apple-system,Segoe UI,Microsoft YaHei,sans-serif',
        'color:#fff', 'box-shadow:0 12px 30px rgba(0,0,0,.45)', 'transition:opacity .25s,transform .25s',
        'opacity:0', 'transform:translateY(8px)', 'max-width:320px', 'pointer-events:none',
      ].join(';');
      document.documentElement.appendChild(el);
    }
    el.style.background = bad ? 'rgba(190,50,50,.95)' : 'rgba(20,28,40,.95)';
    el.textContent = text;
    requestAnimationFrame(() => {
      el.style.opacity = '1';
      el.style.transform = 'translateY(0)';
    });
    clearTimeout(el.__t);
    el.__t = setTimeout(() => {
      el.style.opacity = '0';
      el.style.transform = 'translateY(8px)';
    }, 2600);
  }

  function ensureChip() {
    let chip = document.getElementById(chipId);
    if (chip) return chip;
    chip = document.createElement('div');
    chip.id = chipId;
    chip.style.cssText = [
      'position:fixed', 'right:20px', 'bottom:22px', 'z-index:2147483647',
      'display:flex', 'align-items:center', 'gap:8px', 'cursor:pointer',
      'padding:9px 14px 9px 11px', 'border-radius:999px',
      'background:linear-gradient(135deg,#6c7bff,#22d3ee)', 'color:#07101c',
      'font:600 13px/1 -apple-system,Segoe UI,Microsoft YaHei,sans-serif',
      'box-shadow:0 12px 28px rgba(60,90,255,.42)', 'user-select:none',
      'transition:transform .18s,box-shadow .18s', 'opacity:0',
    ].join(';');
    chip.innerHTML = '<span style="display:grid;place-items:center;width:20px;height:20px;border-radius:50%;background:rgba(0,0,0,.18)">'
      + '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#07101c" stroke-width="3"><path d="M12 4v11m0 0l-4-4m4 4l4-4"/><path d="M5 19h14"/></svg></span>'
      + '<span id="grabdown-chip-label">grabdown</span>';
    chip.addEventListener('mouseenter', () => { chip.style.transform = 'translateY(-2px)'; });
    chip.addEventListener('mouseleave', () => { chip.style.transform = 'translateY(0)'; });
    document.documentElement.appendChild(chip);
    requestAnimationFrame(() => { chip.style.opacity = '1'; });
    return chip;
  }

  let lastItems = [];

  async function refreshChip() {
    const items = collect();
    lastItems = items;
    if (!items.length) {
      const c = document.getElementById(chipId);
      if (c) c.remove();
      return;
    }
    const chip = ensureChip();
    const label = chip.querySelector('#grabdown-chip-label');
    if (label) label.textContent = `grabdown · ${items.length}`;
    if (cfg.autoCapture) {
      // 关掉弹窗也生效：发现 m3u8 直接上报，由 background 去重后提交
      send({ type: 'auto-media', items, page: location.href });
    }
    chip.onclick = async () => {
      chip.style.opacity = '.7';
      const r = await send({ type: 'download', items, quality: 'best' });
      chip.style.opacity = '1';
      if (r?.ok) toast(`已提交 ${items.length} 个到 grabdown`);
      else if (r?.offline) toast('grabdown agent 未启动：python -m grabdown agent', true);
      else toast('提交失败：' + (r?.error || r?.message || '未知错误'), true);
    };
  }

  // popup 来问
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg?.type === 'config-changed') {
      loadCfg();
      sendResponse({ ok: true });
      return true;
    }
    if (msg?.type === 'scan') {
      sendResponse({ ok: true, items: collect(), title: document.title, url: location.href });
      return true;
    }
    return false;
  });

  let timer = null;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(refreshChip, 900);
  };
  window.addEventListener('load', schedule, { once: true });
  document.addEventListener('DOMContentLoaded', schedule, { once: true });
  loadCfg().then(schedule);
  setTimeout(schedule, 1200);
  setTimeout(schedule, 4000);
  try {
    new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  } catch (_) {}
  // SPA 路由变化
  for (const m of ['pushState', 'replaceState']) {
    const orig = history[m];
    history[m] = function (...args) {
      const r = orig.apply(this, args);
      schedule();
      return r;
    };
  }
})();
