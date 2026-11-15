/* 扩展后台逻辑的离线测试（Node，无浏览器）
 *
 * 做法：用最小 chrome / fetch 桩把 background.js import 进来，
 * 手动触发 onMessage 监听器，验证：
 *   - 自动捕获开关关闭时什么都不做
 *   - 打开后只挑 m3u8（autoAll=false）
 *   - 同一个 URL 第二次不重复提交（去重）
 *
 * 跑：node ext/test/auto.test.mjs
 */

import assert from 'node:assert/strict';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const storageSync = {};
const storageSession = {};
const listeners = { message: [], installed: [], alarm: [], menu: [], storage: [] };
const posts = [];
const downloads = [];
const notifications = [];

globalThis.chrome = {
  storage: {
    sync: {
      get: async (defaults) => ({ ...defaults, ...storageSync }),
      set: async (obj) => Object.assign(storageSync, obj),
    },
    session: {
      get: async (key) => (key in storageSession ? { [key]: storageSession[key] } : {}),
      set: async (obj) => Object.assign(storageSession, obj),
    },
    onChanged: { addListener: (fn) => listeners.storage.push(fn) },
  },
  runtime: {
    onMessage: { addListener: (fn) => listeners.message.push(fn) },
    onInstalled: { addListener: (fn) => listeners.installed.push(fn) },
    lastError: null,
  },
  contextMenus: { create: () => {}, onClicked: { addListener: (fn) => listeners.menu.push(fn) } },
  alarms: { create: () => {}, onAlarm: { addListener: (fn) => listeners.alarm.push(fn) } },
  action: {
    setBadgeText: async () => {},
    setBadgeBackgroundColor: async () => {},
    setTitle: async () => {},
  },
  tabs: { query: () => {}, sendMessage: async () => ({}) },
  notifications: { create: (n) => notifications.push(n) },
  downloads: { download: (o) => downloads.push(o) },
};

// fetch 桩：/ping /jobs /jobs(POST) /jobs/<id>
const agentJobs = [];
globalThis.fetch = async (url, init = {}) => {
  const u = String(url);
  const body = init.body ? JSON.parse(init.body) : null;
  if (u.endsWith('/ping')) {
    return { ok: true, status: 200, json: async () => ({ ok: true, agent: 'grabdown', version: '0.1.0' }) };
  }
  if (u.endsWith('/jobs') && (!init.method || init.method === 'GET')) {
    return { ok: true, status: 200, json: async () => ({ ok: true, jobs: agentJobs }) };
  }
  if (u.endsWith('/jobs') && init.method === 'POST') {
    posts.push(body);
    const job = { id: 'j' + posts.length, url: body.url, state: 'running', percent: 0, created: Date.now() };
    agentJobs.push(job);
    return { ok: true, status: 200, json: async () => ({ ok: true, job }) };
  }
  return { ok: false, status: 404, json: async () => ({ ok: false }) };
};

const here = path.dirname(fileURLToPath(import.meta.url));
const modPath = pathToFileURL(path.join(here, '..', 'background.js')).href;
await import(modPath);

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function sendMessage(msg, sender = { tab: { url: 'https://page.test/watch' } }) {
  return new Promise((resolve) => {
    const fn = listeners.message[0];
    assert.ok(fn, 'background 没有注册 onMessage');
    fn(msg, sender, resolve);
  });
}

const HLS = { url: 'https://cdn.test/a/master.m3u8?hash=1', kind: 'hls', title: '视频 A' };
const HLS2 = { url: 'https://cdn.test/b/master.m3u8?hash=2', kind: 'hls', title: '视频 B' };
const MP4 = { url: 'https://cdn.test/c/1080.mp4?hash=3', kind: 'file', title: '视频 C' };

// 1) 关闭时不应提交
let r = await sendMessage({ type: 'auto-media', items: [HLS], page: 'https://page.test/watch' });
assert.equal(posts.length, 0, '关闭时不应提交');
assert.match(String(r.skipped || ''), /auto-capture off/);
console.log('✓ 关闭开关时不上报');

// 2) 打开开关，只挑 m3u8
storageSync.autoCapture = true;
r = await sendMessage({ type: 'auto-media', items: [HLS, MP4], page: 'https://page.test/watch' });
await wait(30);
assert.equal(posts.length, 1, '应只提交 m3u8 一条');
assert.match(posts[0].url, /master\.m3u8/);
assert.equal(posts[0].kind, 'hls');
assert.equal(posts[0].referer, 'https://page.test/watch');
console.log('✓ 只自动捕获 m3u8，并带上页面 referer');

// 3) 去重：同一个 URL 再来一次不重复提交
await sendMessage({ type: 'auto-media', items: [HLS], page: 'https://page.test/watch' });
await wait(30);
assert.equal(posts.length, 1, '重复 URL 不应重复提交');
console.log('✓ 同一 m3u8 去重');

// 4) 新 URL 会提交
await sendMessage({ type: 'auto-media', items: [HLS2], page: 'https://page.test/other' });
await wait(30);
assert.equal(posts.length, 2, '新 URL 应提交');
console.log('✓ 新 m3u8 正常提交');

// 5) autoAll=true 时 mp4 也下
storageSync.autoAll = true;
await sendMessage({ type: 'auto-media', items: [MP4], page: 'https://page.test/c' });
await wait(30);
assert.equal(posts.length, 3);
assert.equal(posts[2].kind, 'file');
console.log('✓ autoAll 打开后 mp4 也自动下');

// 6) 自动捕获记录可读
const log = await sendMessage({ type: 'auto-log' });
assert.ok(log.log.length >= 3, 'auto-log 应记录提交历史');
console.log('✓ auto-log 记录', log.log.length, '条');

// 7) settings 读写
const st = await sendMessage({ type: 'settings', patch: { quality: '1080' } });
assert.equal(st.settings.quality, '1080');
console.log('✓ settings 读写');

// 8) 地址规范化：用户填 ws:// 也能用
const cases = [
  ['ws://127.0.0.1:8899/ws', 'http://127.0.0.1:8899'],
  ['wss://host:9/ws', 'https://host:9'],
  ['127.0.0.1:8899', 'http://127.0.0.1:8899'],
  ['http://127.0.0.1:8899/', 'http://127.0.0.1:8899'],
  [' http://127.0.0.1:8899/ws/ ', 'http://127.0.0.1:8899'],
  ['', 'http://127.0.0.1:8899'],
];
for (const [input, expect] of cases) {
  const r = await sendMessage({ type: 'normalize', value: input });
  assert.equal(r.url, expect, `normalize(${JSON.stringify(input)})`);
}
console.log('✓ ws:// / 缺协议 / 尾斜杠 都能规范化');

// 9) 保存流程：存量里是错的值（ws://…/ws），normalize 之后写回 http
storageSync.agentUrl = 'ws://127.0.0.1:8899/ws';
const n1 = await sendMessage({ type: 'normalize', value: storageSync.agentUrl });
assert.equal(n1.url, 'http://127.0.0.1:8899');
const saved = await sendMessage({ type: 'settings', patch: { agentUrl: n1.url } });
assert.equal(saved.settings.agentUrl, 'http://127.0.0.1:8899');
// 读回来也应该是规范化的（settings() 里会再规范化一次）
assert.match(saved.settings.agentUrl, /^http:\/\//);
console.log('✓ 保存流程把 ws:// 纠成 http:// 并落盘');

console.log('\n全部通过');
