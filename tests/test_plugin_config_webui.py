"""Run the actual config modal functions in Node with a minimal DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = JSON.parse(fs.readFileSync(0, 'utf8'));
const configModal = source.slice(source.indexOf('let pluginConfigCache'), source.indexOf('// ═══════════════ 5. Contexts'));
const schema = {
  group: {type: 'string'}, limit: {type: 'int'}, ignored: {type: 'string'},
  request: {type: 'object', items: {
    blacklist: {type: 'list'}, auto: {type: 'bool'}, secret: {type: 'string', invisible: true},
  }},
  hidden: {type: 'object', invisible: true, items: {key: {type: 'string'}}},
};
const config = {group: '100', limit: 1, ignored: 'drop', request: {blacklist: ['101'], auto: true}};
const controls = new Map();
const form = {set innerHTML(html) {
  controls.clear();
  for (const m of html.matchAll(/<input\b[^>]*\bid="pcf-([^"]+)"[^>]*>/g)) {
    controls.set('pcf-' + m[1], {value: m[0].match(/\bvalue="([^"]*)"/)?.[1] || '', checked: m[0].includes(' checked')});
  }
  // Simulate a missing control and stale DOM nodes belonging to invisible config.
  controls.delete('pcf-ignored');
  controls.set('pcf-request.secret', {value: 'never submit'});
  controls.set('pcf-hidden.key', {value: 'never submit'});
}};
const toasts = [], posts = [];
let application;
const context = vm.createContext({
  document: {getElementById: id => ({
    'plugin-config-modal': {classList: {add() {}, remove() {}}},
    'plugin-config-title': {}, 'plugin-config-form': form,
  }[id] || controls.get(id))},
  apiJSON: async (path, options) => {
    assert.equal(path, '/api/plugins/example/config');
    if (!options) return {schema, config};
    assert.equal(options.method, 'POST');
    posts.push(JSON.parse(JSON.stringify(options.body)));
    if (application === 'error') throw new Error('disk failure');
    return {status: 'ok', application};
  },
  showToast: (text, success = true) => toasts.push([text, success]),
  loadPlugins() {},
});
vm.runInContext(configModal, context);
const open = () => context.openPluginConfig('example');
const save = () => context.savePluginConfig();
const set = (key, value) => controls.get('pcf-' + key).value = value;
(async () => {
  await open();
  // External update after form open, unrelated to the administrator's edit.
  config.request.blacklist = ['999'];
  set('group', '303');
  application = 'applied';
  await save();
  assert.deepEqual(posts.pop(), {data: {group: '303'}});
  assert.match(toasts.pop()[0], /热生效/);

  await open();
  set('request.blacklist', '');
  controls.get('pcf-request.auto').checked = false;
  set('limit', '0');
  set('group', '');
  application = 'not_running';
  await save();
  assert.deepEqual(posts.pop(), {data: {group: '', limit: 0, request: {blacklist: [], auto: false}}});
  assert.doesNotMatch(toasts.pop()[0], /热生效/);

  await open();
  set('group', '123');
  set('group', '100');
  set('request.blacklist', ' 999 , ');
  await save();
  assert.equal(posts.length, 0);
  assert.match(toasts.pop()[0], /无改动/);

  for (const [status, message, success] of [
    ['apply_failed', /热应用失败/, false], ['unchanged', /无改动/, true], ['error', /保存失败/, false],
  ]) {
    await open();
    set('request.blacklist', '42,999');
    application = status;
    await save();
    assert.deepEqual(posts.pop(), {data: {request: {blacklist: ['42', '999']}}});
    const [text, positive] = toasts.pop();
    assert.match(text, message);
    assert.doesNotMatch(text, /热生效/);
    assert.equal(positive, success);
  }
})().catch(err => { console.error(err); process.exitCode = 1; });
'''


def test_plugin_config_modal_sends_changed_leaves_and_reports_application():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to execute the config modal regression")
    panel = Path(__file__).resolve().parents[1] / "mohobot/web_panel/static/index.html"
    script = panel.read_text(encoding="utf-8").split("<script>", 1)[1].split("</script>", 1)[0]
    syntax = subprocess.run([node, "--check"], input=script, text=True, capture_output=True, timeout=10)
    assert syntax.returncode == 0, syntax.stderr
    behavior = subprocess.run(
        [node, "-e", _HARNESS], input=json.dumps(script), text=True, capture_output=True, timeout=10,
    )
    assert behavior.returncode == 0, behavior.stderr
