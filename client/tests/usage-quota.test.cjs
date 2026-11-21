'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const file = path.join(__dirname, '..', 'components', 'panels', 'panels-usage.js');
const src = fs.readFileSync(file, 'utf8');

const start = src.indexOf('function quotaView(');
const end = src.indexOf('function quotaCardHTML(', start);
assert.ok(start >= 0 && end > start, 'quotaView must precede quotaCardHTML');
const quotaView = require('node:vm').runInNewContext(src.slice(start, end) + '\nquotaView', {});
const fmt = { tx: (v) => v, num: (v) => String(v), date: () => 'Jan 1' };

test('limited quota computes remaining tokens and used percentage', () => {
  const view = quotaView({ enabled: true, totalLimited: true, limit: 1000, used: 250, resetsAt: 'x' }, fmt);
  assert.equal(view.unlimited, false);
  assert.equal(view.remaining, 750);
  assert.equal(view.pct, 25);
  assert.match(view.usedLine, /已用 250 \/ 1000 Tokens/);
  assert.match(view.timing, /下次恢复/);
});

test('used beyond the limit clamps to zero without going negative', () => {
  const view = quotaView({ enabled: true, totalLimited: true, limit: 100, used: 180 }, fmt);
  assert.equal(view.remaining, 0);
  assert.equal(view.pct, 100);
});

test('unlimited quota hides the bar and the denominator', () => {
  const view = quotaView({ enabled: true, totalLimited: false, limit: 0, used: 10 }, fmt);
  assert.equal(view.unlimited, true);
  assert.equal(view.remaining, null);
  assert.equal(view.pct, null);
  assert.match(view.usedLine, /总额度不限额/);
  const noLimit = quotaView({ enabled: true, used: 3 }, fmt);
  assert.equal(noLimit.unlimited, true);
});

test('missing or disabled quota renders no card', () => {
  assert.equal(quotaView(null, fmt), null);
  assert.equal(quotaView({ enabled: false, limit: 10, used: 1 }, fmt), null);
});

test('per-model remaining clamps negatives and keeps null as unlimited', () => {
  const view = quotaView({
    enabled: true, totalLimited: true, limit: 10, used: 1,
    models: [{ provider: 'p', model: 'm', remaining: -5 }, { provider: 'p2', model: 'm2', remaining: null }],
  }, fmt);
  assert.equal(view.models[0].remaining, 0);
  assert.equal(view.models[1].remaining, null);
  assert.deepEqual(view.models[0].label, 'p / m');
});

test('panel mounts the quota card between the overview and usage columns', () => {
  assert.ok(src.includes("'</section>' + quotaCardHTML(view)"));
  assert.ok(src.indexOf("quotaCardHTML(view)") < src.indexOf("'<div class=\"usage-columns\">'"));
  assert.match(src, /clientAuthMe\?\.\(\)\.catch\(\(\) => null\)/);
});
