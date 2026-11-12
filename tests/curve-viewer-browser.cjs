// Capture the real curve-fitting viewer. Arguments: exported HTML, image directory.
// Run the documented five-candidate --demo first; this script never edits evidence.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

(async () => {
  const [html, destination] = process.argv.slice(2);
  if (!html || !destination) throw Error("Usage: node tests/curve-viewer-browser.cjs VIEWER.html IMAGE_DIRECTORY");
  fs.mkdirSync(destination, { recursive: true });
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1200 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setContent(fs.readFileSync(html, "utf8"));
    const summary = await page.evaluate(() => {
      const session = data.sessions[0];
      const selected = session.selected[session.contract_hash];
      const winner = session.candidates.find(candidate => candidate.id === selected);
      const invalid = session.candidates.find(candidate => candidate.status === "invalid");
      return {
        selected, invalid: invalid?.id,
        candidates: session.candidates.length,
        rmse: winner.evidence[0].metrics.rmse,
        starts: data.run.solver_trial_starts,
      };
    });
    if (summary.candidates !== 5 || summary.starts !== 4 || !summary.invalid || !(summary.rmse < 0.2))
      throw Error("Expected the actual complete curve-fitting demonstration");
    await page.getByRole("button", { name: summary.selected, exact: true }).click();
    const image = page.locator("figure.artifact-preview img");
    await image.evaluate(async node => { await node.decode(); });
    if (await page.getByText("ATIF trajectory", { exact: false }).count())
      throw Error("The deterministic evaluator must not manufacture agent traces");
    const metricTable = page.locator("#detail table").first();
    await metricTable.scrollIntoViewIfNeeded();
    await page.evaluate(() => {
      const top = document.querySelector("#detail table").getBoundingClientRect().top + scrollY;
      scrollTo(0, Math.max(0, top - 70));
    });
    await page.screenshot({ path: path.join(destination, "curve-viewer.png") });

    await page.getByRole("button", { name: summary.invalid, exact: true }).click();
    const detail = await page.locator("#detail").innerText();
    if (!detail.includes("invalid") || !detail.includes("between -10 and 10"))
      throw Error("The invalid candidate's actual reason is missing");
    if (await page.locator("figure.artifact-preview img").count())
      throw Error("An invalid candidate must not have a measured curve");
    await page.setViewportSize({ width: 1600, height: 1700 });
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: path.join(destination, "curve-comparison.png") });
    if (errors.length) throw Error(errors.join(", "));
    console.log(JSON.stringify(summary));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
