'use strict';
// Dependency-free regression checks for the real front-end handler.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const code = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8').replace(/\nboot\(\);\s*$/, '\n');
const handlers = {}, nodes = {}, requests = [];
function node(extra = {}) {
  return {hidden: false, textContent: '', className: '', addEventListener() {},
    classList: {remove() {}, toggle() {}}, ...extra};
}
for (const name of ['loading', 'app', 'onboarding', 'mobile-menu', 'test-result']) nodes['#' + name] = node();
nodes['.sidebar'] = node();
nodes['#onboarding'].replaceChildren = () => {nodes['#wizard-form'] = null;};
class TestFormData {
  constructor(form) {this.form = form;}
  [Symbol.iterator]() {return Object.entries(this.form.data)[Symbol.iterator]();}
}
const context = vm.createContext({
  document: {querySelector: s => nodes[s] || null, addEventListener: (kind, fn) => {handlers[kind] = fn;}},
  window: {addEventListener() {}}, location: {hash: ''},
  FormData: TestFormData, setInterval() {}, setTimeout() {}, console,
  fetch: async (url, options) => {
    requests.push({url, options});
    return {ok: true, json: async () => url === '/api/status'
      ? {csrf: 'synthetic-csrf', authenticated: true, setup_required: false}
      : {message: '发送成功'}};
  }
});
vm.runInContext(code, context);
vm.runInContext('refresh = async () => {};', context);
function form(id) {
  return {id, data: {wecom_url: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=synthetic-test-only'},
    reportValidity: () => true, querySelectorAll: () => []};
}
async function clickTest(actualForm) {
  const button = {dataset: {action: 'test-wecom', channel: 'wecom'}, disabled: false, closest: () => actualForm};
  await handlers.click({target: {closest: () => button}});
  assert.equal(button.disabled, false);
  return requests.at(-1);
}
(async () => {
  // Reproduce the bug: wizard still exists, but clicked button belongs to notification form.
  nodes['#wizard-form'] = form('wizard-form');
  nodes['#onboarding'].hidden = true;
  const regular = await clickTest(form('notification-form'));
  assert.equal(regular.url, '/api/notifications/test');
  assert.equal(regular.options.headers['X-Setup-Token'], undefined);
  assert.equal(JSON.parse(regular.options.body).url.includes('synthetic-test-only'), true);
  assert.equal(nodes['#test-result'].textContent, '发送成功');
  // Wizard is valid only while onboarding is actually visible.
  nodes['#onboarding'].hidden = false;
  vm.runInContext("step = 3; setupToken = 'synthetic-setup';", context);
  const wizard = await clickTest(nodes['#wizard-form']);
  assert.equal(wizard.url, '/api/setup/test');
  assert.equal(wizard.options.headers['X-Setup-Token'], 'synthetic-setup');
  // Authenticated boot actively removes stale wizard instead of merely hiding it.
  await vm.runInContext('boot()', context);
  assert.equal(nodes['#wizard-form'], null);
  assert.equal(nodes['#onboarding'].hidden, true);
  assert.equal(nodes['#app'].hidden, false);
  const afterBoot = await clickTest(form('notification-form'));
  assert.equal(afterBoot.url, '/api/notifications/test');
  assert.equal(afterBoot.options.headers['X-CSRF-Token'], 'synthetic-csrf');
  console.log('Front-end regression PASS: stale wizard routing / active wizard / authenticated cleanup / CSRF');
})().catch(error => {console.error(error); process.exitCode = 1;});
