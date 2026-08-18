#!/usr/bin/env node
/**
 * Assert what a built retro.html actually renders, by running its scripts.
 *
 * Executed verification for #3808 (D-006). The defect this guards is not a
 * logic error the Python tests can see: the churn dump had failed on every run
 * it ever had, `build_dashboard.py` dropped the section, and the page rendered
 * normally -- a missing panel is indistinguishable from a feature that was
 * never built. The fix is a rendering claim ("an absent dump says so on the
 * page"), so it is proved by loading the page in a DOM and reading it.
 *
 * Usage:
 *   node check_dashboard_render.js <retro.html> --unavailable spend,churn
 *   node check_dashboard_render.js <retro.html> --rendered spend,churn
 *
 * --unavailable: the named sections must be visible AND carry the notice
 *                naming their data file and the dump that owes it.
 * --rendered:    the named sections must show their data and no notice.
 */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require(process.env.JSDOM_PATH || "jsdom");

const [, , htmlPath, ...rest] = process.argv;
if (!htmlPath) {
  console.error("usage: check_dashboard_render.js <retro.html> [--unavailable a,b] [--rendered a,b]");
  process.exit(64);
}

const opts = { unavailable: [], rendered: [] };
for (let i = 0; i < rest.length; i += 2) {
  const key = rest[i].replace(/^--/, "");
  if (!(key in opts)) {
    console.error(`unknown option ${rest[i]}`);
    process.exit(64);
  }
  opts[key] = (rest[i + 1] || "").split(",").filter(Boolean);
}

// Sections keyed the way build_dashboard.py keys their data sources.
const SECTIONS = {
  spend: { section: "spendSection", body: "spendbody", notice: "spendunavail", file: "spend.json", workflow: "retro_spend_dump.yml" },
  churn: { section: "churnSection", body: "churnbody", notice: "churnunavail", file: "churn.json", workflow: "retro_churn_dump.yml" },
};

const dom = new JSDOM(fs.readFileSync(htmlPath, "utf8"), { runScripts: "dangerously" });
const doc = dom.window.document;

const failures = [];
const check = (ok, message) => {
  if (!ok) failures.push(message);
};

for (const key of opts.unavailable) {
  const s = SECTIONS[key];
  const section = doc.getElementById(s.section);
  const notice = doc.getElementById(s.notice);
  const body = doc.getElementById(s.body);
  check(section && !section.hidden, `${key}: the whole section is hidden — an absent dump must be visible, not omitted`);
  check(notice && !notice.hidden, `${key}: no unavailable notice rendered`);
  check(body && body.hidden, `${key}: the data body is showing without data`);
  const text = notice ? notice.textContent.replace(/\s+/g, " ") : "";
  check(text.includes(s.file), `${key}: the notice does not name data/${s.file}`);
  check(text.includes(s.workflow), `${key}: the notice does not name ${s.workflow}, so a reader cannot find the failed run`);
  check(/unavailable/i.test(text), `${key}: the notice does not say the section is unavailable`);
  const link = notice && notice.querySelector("a");
  check(link && link.href.includes(s.workflow), `${key}: the notice does not link that workflow's runs`);
}

for (const key of opts.rendered) {
  const s = SECTIONS[key];
  const notice = doc.getElementById(s.notice);
  const body = doc.getElementById(s.body);
  check(body && !body.hidden, `${key}: the data body is hidden even though the dump landed`);
  check(!notice || notice.hidden, `${key}: an unavailable notice is showing over real data`);
  check(body && body.textContent.trim().length > 0, `${key}: the section rendered empty`);
}

// Whatever the case, the provenance footer must list every source.
const prov = doc.getElementById("provenancerows");
check(prov && prov.children.length > 0, "the data-provenance table rendered no rows");

const name = path.basename(htmlPath);
if (failures.length) {
  console.error(`FAIL ${name}:\n  - ${failures.join("\n  - ")}`);
  process.exit(1);
}
console.log(
  `ok ${name}: ${opts.unavailable.length ? `unavailable=[${opts.unavailable}] ` : ""}` +
    `${opts.rendered.length ? `rendered=[${opts.rendered}]` : ""}`.trim()
);
