// Real browser regression over scripted in-memory cases; never changes run evidence.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const { chromium } = require("playwright");
const html = fs.readFileSync(process.argv[2], "utf8");
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
    headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
  const errors = [], pages = [];
  async function fixture(mode = "mixed") {
    const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
    pages.push(page);
    page.on("pageerror", error => errors.push(error.message));
    await page.setContent(html);
    await page.evaluate(mode => {
      const s = structuredClone(data.sessions[0]), source = s.candidates[0], evidence = source.evidence[0];
      s.role = "research";
      s.assignment = "Scripted progress-chart fixture";
      s.metrics = [{ name: "error", direction: "min" }, { name: '<img src=x onerror="globalThis.INJECTED=1">', direction: "max" }];
      s.objective_policy = { description: "Reduce fixture error", direction: "min", score: { metric: "error" }, tie_breakers: [] };
      const make = (n, value, validity = "valid", evaluation = "completed", finished = 120 + n * 60) => ({
        ...structuredClone(source), id: `candidate-${String(n).padStart(4, "0")}`, sequence: n,
        parent_id: n === 1 ? null : "candidate-0001", created_at: 100 + n * 10, status: validity || "proposed",
        evidence: [{ ...structuredClone(evidence), contract: s.contract_hash, validity, evaluation,
          validity_record: { verdict: validity, reasons: ["Scripted fixture"] }, metrics: { error: value }, score: value,
          feasible: true, task_completed: true, cells: [], timing: { finished_at: finished } }],
      });
      s.candidates = [make(1, 2), make(2, 0), make(3, 999, "invalid", "not_started"), make(4, null, null),
        make(5, 100, "valid", "incomplete"), make(6, -999), make(7, 1), make(8, 0.4), make(9, true)];
      s.candidates[3].evidence = [];
      s.candidates[5].evidence[0].contract = "old-contract-fixture";
      Object.assign(s.candidates[6].evidence[0], { feasible: false, task_completed: false });
      s.candidates[7].evidence[0].timing.finished_at = null;
      // Equal real measurement spacing differs from the gaps in proposal order.
      [s.candidates[0], s.candidates[1], s.candidates[6]].forEach((c, i) => c.evidence[0].timing.finished_at = 120 + i * 60);
      s.selected = { [s.contract_hash]: "candidate-0002" };
      if (mode === "empty") s.candidates = [];
      if (mode === "single") s.candidates = [s.candidates[0]];
      if (mode === "extreme") {
        s.candidates = [make(1, -Number.MAX_VALUE), make(2, Number.MAX_VALUE)];
        s.selected = { [s.contract_hash]: "candidate-0001" };
      }
      if (mode === "composite") {
        s.objective_policy = { description: "Composite fixture", direction: "max", score: { function_hash: "fixture" }, tie_breakers: [] };
      }
      if (mode === "nonresearch") s.role = "attacker";
      data.sessions[0] = s;
      showSession();
    }, mode);
    return page;
  }
  const ids = page => page.locator("#metric-chart circle.metric-point").evaluateAll(nodes => nodes.map(n => n.dataset.candidate));
  const counts = async page => {
    const result = {};
    for (const name of ["valid", "invalid", "unreviewed"])
      result[name] = await page.locator(`#proposal-outcomes [data-outcome=${name}]`).textContent();
    return result;
  };
  async function finiteSvg(page) {
    const invalid = await page.locator("#metric-chart svg").evaluateAll(nodes => nodes.flatMap(svg =>
      [...svg.querySelectorAll("*")].flatMap(node => [...node.attributes]
        .filter(a => /^(?:d|points|x\d?|y\d?|cx|cy|r|width|height)$/.test(a.name) && /NaN|Infinity/.test(a.value))
        .map(a => `${a.name}=${a.value}`))));
    assert.deepEqual(invalid, [], "Every plotted coordinate must be finite");
  }
  try {
    const page = await fixture();
    assert.deepEqual(await ids(page), ["candidate-0001", "candidate-0002", "candidate-0007", "candidate-0008"]);
    assert.match(await page.locator("#progress").innerText(), /lower is better/i);
    const totals = await counts(page);
    assert.match(totals.valid, /\b5\b/); assert.match(totals.invalid, /\b1\b/); assert.match(totals.unreviewed, /\b2\b/);
    const positions = await page.locator("#metric-chart circle.metric-point").evaluateAll(nodes => nodes.map(n => +n.getAttribute("cx")));
    assert(positions[2] - positions[1] > 2 * (positions[1] - positions[0]), "Missing versions retain their position in version order");
    await finiteSvg(page);
    await page.locator('#metric-chart [data-candidate="candidate-0002"]').click();
    assert.match(await page.locator("#detail").innerText(), /candidate-0002/);
    const seventh = page.locator('#metric-chart [data-candidate="candidate-0007"]');
    await seventh.focus(); await seventh.press("Enter");
    assert.match(await page.locator("#detail").innerText(), /candidate-0007/);
    assert.match(await page.locator("#detail").innerText(), /feasible: false/);
    await page.locator('#proposal-outcomes button[data-candidate="candidate-0003"]').click();
    assert.match(await page.locator("#detail").innerText(), /invalid/);
    await page.locator("#progress-axis").selectOption("time");
    assert.deepEqual(await ids(page), ["candidate-0001", "candidate-0002", "candidate-0007"]);
    assert.match(await page.locator("#progress").innerText(), /timestamp/i);
    const times = await page.locator("#metric-chart circle.metric-point").evaluateAll(nodes => nodes.map(n => +n.getAttribute("cx")));
    assert(Math.abs((times[1] - times[0]) - (times[2] - times[1])) < 0.1, "Time axis uses measurement timestamps");
    const hostile = await page.locator("#progress-metric option").evaluateAll(nodes => nodes.find(n => n.textContent.includes("<img"))?.value);
    assert.notEqual(hostile, undefined); await page.locator("#progress-metric").selectOption(hostile);
    assert.equal(await page.evaluate(() => !!globalThis.INJECTED), false);
    assert.equal(await page.locator("#progress img").count(), 0);
    assert.equal((await ids(page)).length, 0, "Missing metric values are not plotted as zero");
    const errorMetric = await page.locator("#progress-metric option").evaluateAll(nodes =>
      nodes.find(n => n.textContent.startsWith("error"))?.value);
    assert.notEqual(errorMetric, undefined);
    await page.locator("#progress-metric").selectOption(errorMetric);
    await page.locator("#progress-axis").selectOption("version");
    await page.setViewportSize({ width: 390, height: 900 });
    await page.waitForFunction(() => {
      const svg = document.querySelector("#metric-chart svg");
      return svg && Math.abs(svg.viewBox.baseVal.width - svg.getBoundingClientRect().width) < 1;
    });
    await page.locator("#metric-chart").scrollIntoViewIfNeeded();
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => document.documentElement.dataset.theme = theme, theme);
      assert.equal((await ids(page)).length, 4, `${theme}: actual measurements remain visible on mobile`);
      const labelHeights = await page.locator("#metric-chart svg text").evaluateAll(nodes =>
        nodes.map(node => node.getBoundingClientRect().height));
      assert(labelHeights.length > 0 && labelHeights.every(height => height >= 11),
        `${theme}: rendered chart labels stay readable on mobile: ${labelHeights}`);
      assert(await page.locator("#metric-chart svg").evaluate(node => node.getBoundingClientRect().height >= 245),
        `${theme}: mobile chart retains its usable height`);
      await finiteSvg(page);
      const overflow = await page.evaluate(() => [...document.querySelectorAll("body *")]
        .filter(n => n.getBoundingClientRect().right > innerWidth + 1).slice(0, 8)
        .map(n => `${n.tagName}.${n.className}: ${n.getBoundingClientRect().right}`));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true,
        `${theme}: no page overflow: ${overflow.join(", ")}`);
    }
    const before = await page.evaluate(() => document.documentElement.dataset.theme);
    await page.locator("#theme-toggle").click();
    assert.notEqual(await page.evaluate(() => document.documentElement.dataset.theme), before);
    const composite = await fixture("composite");
    assert.match(await composite.locator("#progress-metric option:checked").textContent(), /Objective score/i);
    assert.match(await composite.locator("#progress").innerText(), /higher is better/i);
    for (const mode of ["empty", "single"]) {
      const edge = await fixture(mode); await finiteSvg(edge);
      assert.equal((await ids(edge)).length, mode === "empty" ? 0 : 1);
      for (const value of Object.values(await counts(edge))) assert.match(value, /\b0\b/);
    }
    const extreme = await fixture("extreme");
    assert.equal((await ids(extreme)).length, 2);
    await finiteSvg(extreme);
    const other = await fixture("nonresearch");
    assert.match((await counts(other)).valid, /\b6\b/, "Other roles count every submitted candidate");
    assert.deepEqual(errors, []);
    console.log("Browser fixtures: generic metrics, validity, missing data, contracts, time, navigation, themes and mobile passed.");
  } finally { await Promise.all(pages.map(p => p.close())); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
