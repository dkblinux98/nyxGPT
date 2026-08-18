#!/usr/bin/env node
/**
 * Execute a built retro.html's page script under a minimal DOM shim and print
 * what the freshness stamps actually rendered (#3807).
 *
 * Why this exists: the build stamp and the per-source "as of" lines are
 * produced by JavaScript at page load. A Python test can assert the data
 * reached the HTML, but not that the script runs — a template typo blanks the
 * whole dashboard silently, which is exactly the failure mode this feature is
 * supposed to protect the reader from. Running the script in a JS engine is
 * the only way to see the rendered strings.
 *
 * Usage: node tests/retro_render_check.mjs <built.html>
 * Output: JSON on stdout — {buildstamp, staleafter, provenancerows, asof: {...}}
 * Exit 1 if the page script throws.
 */

import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const file = process.argv[2];
if (!file) {
  console.error('usage: node tests/retro_render_check.mjs <built.html>');
  process.exit(2);
}
const html = readFileSync(file, 'utf8');

const script = html.match(/<script>([\s\S]*)<\/script>/);
if (!script) {
  console.error('no <script> block in ' + file);
  process.exit(1);
}

function makeEl(props = {}) {
  return {
    innerHTML: '',
    textContent: '',
    hidden: false,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    getAttribute: () => null,
    appendChild() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    ...props,
  };
}

// The elements the script addresses by id are created on demand, so a new id
// in the template needs no shim change; [data-asof] carriers are read out of
// the markup so their dataset matches the real page.
const byId = new Map();
const asofEls = [...html.matchAll(/data-asof="([^"]*)"/g)].map(([, keys]) =>
  makeEl({ dataset: { asof: keys } }),
);

const document = {
  getElementById(id) {
    if (!byId.has(id)) byId.set(id, makeEl({ id }));
    return byId.get(id);
  },
  querySelectorAll(sel) {
    return sel === '[data-asof]' ? asofEls : [];
  },
  addEventListener() {},
  documentElement: makeEl(),
};

try {
  vm.runInNewContext(script[1], {
    document,
    window: { addEventListener() {} },
    console,
    innerWidth: 1200,
    innerHeight: 900,
  });
} catch (err) {
  console.error('page script threw: ' + (err && err.stack ? err.stack : err));
  process.exit(1);
}

const read = (id) => {
  const el = byId.get(id);
  if (!el) return null;
  return el.innerHTML || el.textContent || '';
};

console.log(
  JSON.stringify(
    {
      buildstamp: read('buildstamp'),
      staleafter: read('staleafter'),
      provenancerows: read('provenancerows'),
      asof: Object.fromEntries(asofEls.map((el) => [el.dataset.asof, el.innerHTML])),
    },
    null,
    1,
  ),
);
