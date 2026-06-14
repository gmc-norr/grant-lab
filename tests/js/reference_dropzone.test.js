const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const APP_JS = path.join(__dirname, "..", "..", "src", "call_manager", "web", "static", "app.js");

function loadModule() {
  const ctx = vm.createContext({
    document: {
      readyState: "complete",
      addEventListener() {},
      querySelectorAll() { return []; },
      getElementById() { return null; },
    },
    window: {},
  });
  vm.runInContext(fs.readFileSync(APP_JS, "utf8"), ctx);
  return ctx.window.__refsDropzone;
}

test("filterPdfs keeps PDFs, rejects others, enforces caps", () => {
  const mod = loadModule();
  assert.ok(mod, "window.__refsDropzone must be exported");
  const ok = mod.filterPdfs(
    [{ name: "a.pdf", type: "application/pdf", size: 10 },
     { name: "b.csv", type: "text/csv", size: 10 }],
    { maxFiles: 25, maxBytes: 1000 });
  assert.equal(ok.accepted.length, 1);
  assert.equal(ok.rejected.length, 1);
  const over = mod.filterPdfs(
    [{ name: "x.pdf", type: "application/pdf", size: 1 },
     { name: "y.pdf", type: "application/pdf", size: 1 }],
    { maxFiles: 1, maxBytes: 1000 });
  assert.equal(over.accepted.length, 0);
  assert.ok(over.error);
});

test("runQueue caps concurrency and creates placeholders up front", async () => {
  const mod = loadModule();
  const states = [];
  const files = [{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4 }];
  let active = 0, maxActive = 0;
  function uploadOne(file, row) {
    active++; maxActive = Math.max(maxActive, active);
    row.set("processing");
    return new Promise((res) => setTimeout(() => { active--; row.set("done"); res(200); }, 5));
  }
  function makeRow(file) {
    states.push([file.id, "queued"]);  // placeholder created at queue time
    return { set(s) { states.push([file.id, s]); } };
  }
  await mod.runQueue(files, { concurrency: 2, uploadOne, makeRow });
  assert.ok(maxActive <= 2, "concurrency must not exceed 2, saw " + maxActive);
  assert.equal(states.filter((s) => s[1] === "queued").length, 4);
  assert.equal(files.filter((f) => states.some((s) => s[0] === f.id && s[1] === "done")).length, 4);
});

test("429 requeues the same row without a new placeholder", async () => {
  const mod = loadModule();
  let attempts = 0;
  const made = [];
  function makeRow() { const r = { set() {} }; made.push(r); return r; }
  function uploadOne() { attempts++; return Promise.resolve(attempts === 1 ? 429 : 200); }
  await mod.runQueue([{ id: 1 }], { concurrency: 1, uploadOne, makeRow, backoffMs: 1 });
  assert.equal(made.length, 1, "exactly one placeholder across retries");
  assert.equal(attempts, 2, "429 retried once");
});
