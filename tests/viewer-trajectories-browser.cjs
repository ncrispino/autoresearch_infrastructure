// Check every embedded native/saved ATIF in a single-session HTML export.
// Usage: node tests/viewer-trajectories-browser.cjs VIEWER.html [SCREENSHOT.png]
const { chromium } = require("playwright");
const fs = require("fs");

(async () => {
  const [html, screenshot] = process.argv.slice(2);
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1550, height: 1200 } });
    const errors = [], requests = [];
    page.on("pageerror", error => errors.push(error.message));
    page.on("request", request => {
      if (/^https?:/.test(request.url())) requests.push(request.url());
    });
    await page.setContent(fs.readFileSync(html, "utf8"));
    const facts = await page.evaluate(() => ({
      sessions: data.sessions.length,
      candidates: data.sessions[0].candidates.map(candidate => candidate.id),
      snapshots: Object.values(data.snapshot_trajectories || {}),
    }));
    if (facts.sessions !== 1 || !facts.snapshots.length)
      throw Error("Expected a single-session export with saved ATIF");
    const seen = new Set();
    for (const candidate of facts.candidates) {
      await page.getByRole("button", { name: candidate, exact: true }).click();
      for (const trace of await page.locator("#detail details").all()) {
        const summary = trace.locator("summary").first();
        if (!(await summary.innerText()).startsWith("ATIF trajectory")) continue;
        await summary.click();
        if (await trace.locator("pre").count() !== 1)
          throw Error("Embedded trace incorrectly requires a server");
        const raw = await trace.locator("pre").innerText();
        const snapshot = facts.snapshots.find(saved => saved.raw === raw);
        if (!snapshot || !JSON.parse(raw).schema_version?.startsWith("ATIF-"))
          throw Error("Trace must display the original embedded ATIF without a server");
        seen.add(snapshot.sha256);
        await summary.click();
      }
    }
    if (seen.size !== facts.snapshots.length)
      throw Error("Not every embedded trace is inspectable");
    for (const image of await page.locator("figure.artifact-preview img").all())
      await image.evaluate(async node => { await node.decode(); });
    if (screenshot) await page.screenshot({ path: screenshot });
    if (errors.length || requests.length) throw Error(JSON.stringify({ errors, requests }));
    console.log(JSON.stringify({ candidates: facts.candidates.length, offlineTracesOpened: seen.size }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
