const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor() {
    this.className = ''; this.title = ''; this.attrs = {}; this.listeners = {};
    this.classList = {contains:(v)=>this.className.split(' ').includes(v), add:(v)=>this.className+=' '+v, remove:()=>{}};
  }
  append() {}
  setAttribute(name, value) { this.attrs[name] = value; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
}

const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('function createApprovalGate(');
const end = source.indexOf('function renderJobPipeline(', start);
const context = vm.createContext({
  document: {createElement: () => new Element()},
  canApproveProductionRelease: () => false,
  createStatusIcon: () => new Element(),
  encodeURIComponent,
  api: async () => {},
  openJob: async () => {},
});
vm.runInContext(source.slice(start, end) + ';this.createGate=createApprovalGate;', context);

const denied = context.createGate({status:'queued'}, true, false, 'job1', context.document);
assert.match(denied.className, /is-disabled/);
assert.equal(denied.attrs.role, undefined);
assert.equal(denied.listeners.click, undefined);

context.canApproveProductionRelease = () => true;
const allowed = context.createGate({status:'queued'}, true, false, 'job1', context.document);
assert.match(allowed.className, /is-actionable/);
assert.equal(allowed.attrs.role, 'button');
assert.equal(typeof allowed.listeners.click, 'function');
console.log('Approval gate permission UI: passed');
