// Runs the dashboard page's script in Node with a minimal DOM stand-in and
// renders every view and drawer from the store sections given on stdin (as
// the stream's "snapshot" event delivers them). Prints, as
// JSON, every HTML string the page assigned to innerHTML (plus the hrefs it
// built), so tests/test_dash_ui.py can check that data never reaches the
// page unescaped. No network: fetch and EventSource never answer.
'use strict';
const fs = require('fs');
const vm = require('vm');

const [htmlPath] = process.argv.slice(2);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const html = fs.readFileSync(htmlPath, 'utf8');
const script = html.slice(html.indexOf('<script>') + '<script>'.length, html.lastIndexOf('</script>'));

const written = [];
function element(id) {
  const el = {
    id,
    _html: '',
    textContent: '',
    className: '',
    value: '',
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    setAttribute() {},
    getAttribute: () => null,
    addEventListener() {},
    querySelector: () => element('?'),
    querySelectorAll: () => [],
    closest: () => null,
    focus() {},
    click() {},
  };
  Object.defineProperty(el, 'innerHTML', {
    get: () => el._html,
    set: (v) => {
      el._html = String(v);
      written.push({ id, html: el._html });
    },
  });
  return el;
}
const els = {};
const document = {
  getElementById: (id) => (els[id] = els[id] || element(id)),
  querySelector: () => element('?'),
  querySelectorAll: () => [],
  addEventListener() {},
  body: element('body'),
  visibilityState: 'visible',
};
const never = () => new Promise(() => {});
class EventSource {
  constructor() {
    this.readyState = 0;
  }
  addEventListener() {}
  close() {}
}
const ctx = {
  document,
  window: { addEventListener() {}, open() {} },
  location: { hash: '', hostname: 'dashboard.example', protocol: 'https:' },
  history: { replaceState() {} },
  localStorage: { getItem: () => null, setItem() {} },
  navigator: {},
  fetch: never,
  EventSource,
  setInterval: () => 0,
  setTimeout: () => 0,
  clearTimeout() {},
  requestAnimationFrame: () => 0,
  alert() {},
  console,
  Date,
  Math,
  JSON,
  Promise,
  Object,
  Array,
  String,
  Number,
  Set,
  encodeURIComponent,
  decodeURIComponent,
};
vm.createContext(ctx);
vm.runInContext(script, ctx, { filename: 'cluster-topology.html' });
ctx.__input = input;
ctx.__keys = null;
vm.runInContext(
  `
  var __keys;
  applySections(__input.sections, true);
  invs = __input.investigations || [];
  renderAll();
  __keys = Object.keys(D);
  for (const m of D.models || []) openModel(m.name);
  for (const n of D.nodes || []) openNode(n.name);
  for (const t of D.teams || []) openTeam(t.name);
  doSearch(__input.search || '');
`,
  ctx,
);
process.stdout.write(JSON.stringify({ written, keys: ctx.__keys }));
