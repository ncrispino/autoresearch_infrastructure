// Optional real-browser check. Pass the malicious-SVG fixture's exported viewer.
const { chromium } = require("playwright");
const fs = require("fs");
(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    const page = await browser.newPage();
    const requests = [], errors = [];
    page.on("request", request => {
      if (/^https?:/.test(request.url())) requests.push(request.url());
    });
    page.on("pageerror", error => errors.push(error.message));
    await page.setContent(fs.readFileSync(process.argv[2], "utf8"));
    const image = page.locator("figure.artifact-preview img");
    await image.waitFor();
    await image.evaluate(async node => { await node.decode(); });
    if (await page.locator("#detail svg").count()) throw Error("SVG was inserted into viewer DOM");
    if (await page.evaluate(() => globalThis.ARTIFACT_EXECUTED)) throw Error("Artifact script executed");
    if (requests.length) throw Error("Artifact requested external resources: " + requests.join(", "));
    if (errors.length) throw Error(errors.join(", "));
    const metrics = await page.evaluate(() => {
      const list = metricList({ error: 0.123456789012345, parameters: 3 });
      return Array.from(list.children, node => ({ text: node.textContent, title: node.title }));
    });
    if (metrics[0].text !== "error: ≈0.123457" || !metrics[0].title.includes("0.123456789012345"))
      throw Error("Rounded metric must preserve its full value in its title");
    if (metrics[1].text !== "parameters: 3") throw Error("Integer metrics should remain readable");
    console.log("Artifact image decoded; scripts and external resources stayed inactive.");
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
