// Runs the operator page's own render functions headlessly, so the page can be TESTED and not just
// parsed. Used by test_switchboard_e2e.py after the 2026-09-08 gamma defect: the page iterated a
// hard-coded ['alpha','beta'] pair, so a third team would register and never render.
//
//   node page_probe.js <page.html> <directory.json>
//
// Prints the HTML that dir() writes into #dir-all. Exits 1 with the error on any throw.
const fs = require('fs');
const vm = require('vm');

const page = fs.readFileSync(process.argv[2], 'utf8');
const directory = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const script = page.split('<script>')[1].split('</script>')[0];

const rec = {};
const el = () => ({ innerHTML: '', textContent: '', value: '', prepend() {}, remove() {},
                    appendChild() {}, children: { length: 0 }, lastChild: null });
const ctx = {
  document: { getElementById: (id) => (rec[id] = rec[id] || el()), createElement: el, title: '' },
  WebSocket: function () { this.close = () => {}; },      // connect() runs at load; keep it inert
  location: { host: '127.0.0.1:3790' },
  setTimeout: () => {},
  fetch: () => Promise.resolve({ json: () => ({}) }),
  console,
};
vm.createContext(ctx);
vm.runInContext(script, ctx);
vm.runInContext('dir(' + JSON.stringify(directory) + ')', ctx);
process.stdout.write((rec['dir-all'] || {}).innerHTML || '');
