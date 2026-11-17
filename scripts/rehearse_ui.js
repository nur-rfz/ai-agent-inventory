// One rehearsal of the on-camera path, driven through the real UI with Playwright.
// Usage: node scripts/rehearse_ui.js <ui-base-url> <cursor-mcp.json path> [screenshot dir]
// Prints one JSON line with timings (seconds after the config edit) and any page errors.
const fs = require("fs");
const { chromium } = require("playwright");

(async () => {
  const [base, cfgPath, shots] = process.argv.slice(2);
  const res = { ok: false, errors: [] };
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  p.on("pageerror", (e) => res.errors.push(e.message));
  p.on("console", (m) => { if (m.type() === "error") res.errors.push(m.text()); });
  const step = async (name, fn, timeout = 30000) => {
    try { await fn(timeout); res[name] = +((Date.now() - t0) / 1000).toFixed(2); }
    catch (e) { res.failed_at = name; throw e; }
  };
  let t0 = Date.now();
  try {
    await p.goto(base);
    await p.waitForFunction(() => document.querySelectorAll("tbody tr[data-agent]").length >= 10, null, { timeout: 60000 });
    await p.waitForFunction(() => /Duplicates \(\d+\)/.test(document.querySelector("nav")?.innerText || ""), null, { timeout: 60000 });

    // --- the on-camera moment: add the MCP server in Cursor ---
    const before = await p.$$eval("tbody tr[data-agent]", (rs) => rs.map((r) => r.getAttribute("data-agent")));
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    cfg.mcpServers["inbox-digest"] = { url: "http://127.0.0.1:8931/mcp", headers: { Authorization: "Bearer demo" } };
    t0 = Date.now();
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2));

    let id;
    await step("row_visible", (t) => p.waitForFunction((known) => {
      const r = [...document.querySelectorAll("tbody tr[data-agent]")].find((x) => !known.includes(x.getAttribute("data-agent")));
      return r ? r.getAttribute("data-agent") : null;
    }, before, { timeout: t }).then(async (h) => { id = await h.jsonValue(); }));
    const row = `tbody tr[data-agent='${id}']`;
    if (shots) await p.screenshot({ path: `${shots}/live-1-row.png` });
    await step("card_filled", (t) => p.waitForFunction((sel) => {
      const r = document.querySelector(sel); return r && !r.querySelector(".shimmer") && !/analyzing/.test(r.innerText);
    }, row, { timeout: t }));
    res.card_name = (await p.textContent(`${row} td:first-child span.font-medium`))?.trim();
    if (shots) await p.screenshot({ path: `${shots}/live-2-card.png` });
    await step("cluster_badge", (t) => p.waitForSelector(`${row} :text-matches("4 people built this")`, { timeout: t }), 45000);

    await p.click(`${row} button:has-text("people built this")`);
    const card = "article:has-text('4 people built the same agent')";
    await step("cluster_confirmed", (t) => p.waitForSelector(`${card} :text("confirmed")`, { timeout: t }), 45000);
    if (shots) await p.screenshot({ path: `${shots}/live-3-cluster.png` });
    await p.click(`${card} button:has-text('Preview one Langdock agent')`);
    await step("preview_open", (t) => p.waitForSelector("text=Consolidated Langdock agent", { timeout: t }));
    await step("preview_includes_live", (t) => p.waitForSelector(".prose-ld", { timeout: t }).then(() =>
      p.waitForSelector("li:has-text('Presenter')", { timeout: t })), 60000);
    if (shots) await p.screenshot({ path: `${shots}/live-4-preview.png` });
    await p.click("button:has-text('Approve')");
    await step("approved", (t) => p.waitForSelector("button:has-text('✓ Approved')", { timeout: t }));
    await p.click("button:has-text('Create in Langdock')");
    await step("create_result", (t) => p.waitForSelector("text=/Dry run|created|failed/", { timeout: t }));
    if (shots) await p.screenshot({ path: `${shots}/live-5-create.png`, fullPage: true });
    res.ok = res.errors.length === 0;
  } catch (e) {
    res.error = String(e.message || e).split("\n")[0];
    if (shots) await p.screenshot({ path: `${shots}/live-error.png` }).catch(() => {});
  } finally {
    console.log(JSON.stringify(res));
    await b.close();
  }
})();
