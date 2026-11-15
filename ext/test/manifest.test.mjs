/* manifest 与代码一致性检查
 *
 * 起因：background.js 用了 chrome.alarms，但 manifest 里没申请 alarms 权限 →
 * SW 一注册就 TypeError → "Service worker registration failed. Status code: 15"，
 * 整个扩展的开关、保存、下载全都不工作，而且界面上只表现为「点了没反应」。
 *
 * 这个测试把「代码里用到的 chrome.* API」和「manifest 声明的权限」对一遍。
 * 跑：node ext/test/manifest.test.mjs
 */

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const extDir = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const manifest = JSON.parse(fs.readFileSync(path.join(extDir, 'manifest.json'), 'utf8'));

// chrome.X → 需要的权限（runtime/action 之类不需要单独声明）
const NEED = {
  alarms: 'alarms',
  storage: 'storage',
  contextMenus: 'contextMenus',
  tabs: 'tabs',
  notifications: 'notifications',
  downloads: 'downloads',
  scripting: 'scripting',
  action: null,
  runtime: null,
  i18n: null,
};
const perm = new Set(manifest.permissions || []);
const host = manifest.host_permissions || [];

let fails = 0;
for (const file of ['background.js', 'content.js', 'popup.js']) {
  const src = fs.readFileSync(path.join(extDir, file), 'utf8');
  const used = new Set([...src.matchAll(/chrome\.([a-zA-Z]+)/g)].map((m) => m[1]));
  for (const api of used) {
    if (!(api in NEED)) {
      console.log(`? ${file} 用到未知 API chrome.${api}（请补进 NEED 表）`);
      continue;
    }
    const need = NEED[api];
    if (need && !perm.has(need)) {
      console.log(`✗ ${file}: 用了 chrome.${api}，但 manifest 没有 "${need}" 权限`);
      fails++;
    }
  }
  console.log(`· ${file}: ${[...used].join(', ')}`);
}
assert.equal(fails, 0, `有 ${fails} 处权限缺失，SW 会注册失败`);

// 关键字段体检
assert.equal(manifest.manifest_version, 3);
assert.ok(manifest.background?.service_worker, '缺 background.service_worker');
assert.ok(manifest.action?.default_popup, '缺 action.default_popup');
assert.ok(host.some((h) => h.includes('127.0.0.1')), '缺本地 host 权限');
assert.ok(host.some((h) => h.startsWith('ws://')), '缺 ws:// host 权限（实时进度要用）');
assert.ok(fs.existsSync(path.join(extDir, manifest.background.service_worker)));
assert.ok(fs.existsSync(path.join(extDir, manifest.action.default_popup)));

// 版本号必须能对得上，方便确认「到底重载了没」
assert.match(manifest.version, /^\d+\.\d+\.\d+$/);

console.log(`\nmanifest v${manifest.version} · 权限 ${[...perm].join(', ')}`);
console.log('全部通过');
