const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '../../frontend/index.html'), 'utf8');
const source = html.slice(html.indexOf('let scrapeEnabled = false;'),
  html.indexOf('async function refreshScrapeStatus()'));

function setup() {
  const elements = {};
  const calls = [];
  const element = id => elements[id] ||= {
    style: {}, value: '', checked: false,
    setAttribute(name, value) { this[name] = value; },
  };
  const context = vm.createContext({
    document: { getElementById: element, querySelectorAll: () => [] },
    API: '',
    saveSettings: async refresh => { calls.push(['save', refresh]); return true; },
    safeJson: async response => response,
    fetch: async (url, options) => {
      calls.push([url, options]);
      return { ok: true, success: true, processed: 3 };
    },
    refreshScrapeStatus: async () => {},
  });
  vm.runInContext(source, context);
  return { context, element, calls };
}

test('start saves settings and changes the button; failed save restores stopped state', async () => {
  const { context, element, calls } = setup();
  context.renderScrapeToggle(false);
  assert.equal(element('btnScrapeToggle').textContent, '开始刮削');
  await context.toggleScraping();
  assert.equal(calls[0][0], 'save');
  assert.equal(element('btnScrapeToggle').textContent, '停止刮削');
  assert.equal(element('btnScrapeToggle')['aria-pressed'], 'true');
  context.renderScrapeToggle(false);
  context.saveSettings = async () => false;
  await context.toggleScraping();
  assert.equal(element('btnScrapeToggle').textContent, '开始刮削');
  assert.equal(element('btnScrapeToggle').disabled, false);
});

test('stop saves only the master switch, even with unfinished directory edits', async () => {
  const { context, element, calls } = setup();
  context.renderScrapeToggle(true);
  context.saveSettings = async () => { throw new Error('Unfinished directory fields'); };
  await context.toggleScraping();
  assert.equal(calls[0][0], '/api/config');
  assert.deepEqual(JSON.parse(calls[0][1].body), { scrape_enabled: false });
  assert.equal(element('btnScrapeToggle').textContent, '开始刮削');
});

test('force scan saves first, selects the requested row, and refreshes after completion', async () => {
  const { context, calls } = setup();
  const fields = { '.scrape-force': {}, '.scrape-pair-msg': {} };
  const row = { querySelector: selector => fields[selector] };
  context.document.querySelectorAll = () => [{}, row];
  await context.forceScrapeDirectory(row);
  assert.deepEqual(calls[0], ['save', false]);
  assert.equal(calls[1][0], '/api/library/scrape/run-once?mapping_index=1&force=true');
  assert.equal(calls[2][0], '/api/library/scrape/monitor/refresh');
  assert.equal(fields['.scrape-force'].disabled, false);
  assert.match(fields['.scrape-pair-msg'].textContent, /3/);
});

test('failed settings save prevents a force scan', async () => {
  const { context, calls } = setup();
  const fields = { '.scrape-force': {}, '.scrape-pair-msg': {} };
  const row = { querySelector: selector => fields[selector] };
  context.document.querySelectorAll = () => [row];
  context.saveSettings = async () => false;
  await context.forceScrapeDirectory(row);
  assert.equal(calls.length, 0);
  assert.equal(fields['.scrape-force'].disabled, false);
});

test('directory rows restore and serialize independent Emby roots', () => {
  const { context, element } = setup();
  const rows = [];
  context.document.createElement = () => {
    const fields = {};
    return { style: {}, querySelector: selector => fields[selector] ||= { value: '' } };
  };
  element('scrapeDirectoryRows').append = row => rows.push(row);
  context.document.querySelectorAll = () => rows;
  context.addScrapeDirectory({ watch_dir: '/downloads/a', output_dir: '/archive/a', emby_media_root: '/media/a' });
  context.addScrapeDirectory({ watch_dir: '/downloads/b', output_dir: '/archive/b' });
  const pairs = JSON.parse(JSON.stringify(context.scrapeDirectoryPairs()));
  assert.equal(pairs[0].emby_media_root, '/media/a');
  assert.equal(pairs[1].emby_media_root, '');
  assert.match(rows[0].innerHTML, /Emby 对应目录/);
});

test('switching modes seeds the first mapping and preserves single-mode Emby settings', () => {
  const { context, element } = setup();
  element('cfgScrapeWatch').value = '/downloads';
  element('cfgScrapeOutput').value = '/archive';
  element('cfgEmbyMediaRoot').value = '/media/original';
  context.document.querySelector = () => null;
  let seeded;
  context.addScrapeDirectory = pair => { seeded = pair; };
  element('cfgScrapeMultiEnabled').checked = true;
  context.toggleScrapeDirectories();
  assert.equal(seeded.emby_media_root, '/media/original');
  assert.equal(element('embySingleMapping').hidden, true);
  assert.equal(element('cfgEmbyMediaRoot').disabled, true);
  assert.equal(element('embyMultiMappingHint').hidden, false);
  element('cfgScrapeMultiEnabled').checked = false;
  context.toggleScrapeDirectories();
  assert.equal(element('embySingleMapping').hidden, false);
  assert.equal(element('cfgEmbyMediaRoot').disabled, false);
  assert.equal(element('cfgEmbyMediaRoot').value, '/media/original');
});
