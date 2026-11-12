// Optional real-browser smoke check. Pass a generated example HTML path.
const { chromium } = require("playwright");
const fs = require("fs");
(async () => {
  const html = fs.readFileSync(process.argv[2], "utf8");
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.setContent(html);
    await page
      .getByRole("button", { name: "candidate-0002", exact: true })
      .click();
    await page.locator("#detail select").selectOption("1");
    const text = await page.locator("#detail").innerText();
    if (
      !text.includes("14") ||
      !text.includes("Compare candidate") ||
      !text.includes("Measurement completed") ||
      !text.includes("valid")
    ) {
      throw Error("Missing candidate metrics or validity");
    }
    const patch = await page
      .locator("#detail > pre")
      .filter({ hasText: "--- parent/items.json" })
      .innerText();
    if (!patch.includes("water")) throw Error("Missing comparison diff");
    if (await page.getByText("ATIF trajectory", { exact: false }).count())
      throw Error("Deterministic packing should not manufacture an agent trajectory");
    await page
      .getByRole("button", { name: "candidate-0003", exact: true })
      .click();
    if (!(await page.locator("#detail").innerText()).includes("invalid"))
      throw Error("Missing invalid verdict");
    await page
      .getByRole("button", { name: "candidate-0004", exact: true })
      .click();
    if (
      !(await page.locator("#detail").innerText()).includes("feasible: false")
    )
      throw Error("Missing constraint outcome");
    if (errors.length) throw Error(errors.join("\n"));
    console.log(
      "Chromium viewer: selection, comparison, metrics, constraints, invalidity and measurement completion passed",
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
