// Click through the real setup page in headless Edge (DevTools protocol) and save one PNG per step.
// Usage: node shots.mjs <edge.exe> <port> <token> <outdir>      (Node 22+: global fetch and WebSocket)
// Output: <outdir>/<vi|en>-step1.png … -step5.png and <vi|en>-status.png
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [edgePath, port, token, outDir] = process.argv.slice(2);
if (!outDir) { console.error("usage: node shots.mjs <edge.exe> <port> <token> <outdir>"); process.exit(2); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shotsFor(lang, debugPort) {
  const profile = mkdtempSync(join(tmpdir(), "uavlink-edge-"));
  const edge = spawn(edgePath, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
    `--lang=${lang === "vi" ? "vi" : "en-US"}`, "--window-size=1280,1300", "about:blank",
  ], { stdio: "ignore" });
  try {
    let target;
    for (let i = 0; i < 50 && !target; i++) {
      try {
        const list = await (await fetch(`http://127.0.0.1:${debugPort}/json`)).json();
        target = list.find((t) => t.type === "page");
      } catch { /* not up yet */ }
      if (!target) await sleep(200);
    }
    if (!target) throw new Error("Edge did not start");

    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error("devtools socket")); });
    let nextId = 1;
    const pending = new Map();
    ws.onmessage = (m) => {
      const msg = JSON.parse(m.data);
      if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
    };
    const send = (method, params = {}) => new Promise((res, rej) => {
      const id = nextId++;
      pending.set(id, (msg) => (msg.error ? rej(new Error(msg.error.message)) : res(msg.result)));
      ws.send(JSON.stringify({ id, method, params }));
    });
    const run = async (expr) => (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result?.value;
    const waitFor = async (expr, what, tries = 100) => {
      for (let i = 0; i < tries; i++) { if (await run(expr)) return; await sleep(200); }
      throw new Error(`timeout waiting for ${what}`);
    };
    const snap = async (name) => {
      await sleep(400);
      const r = await send("Page.captureScreenshot", { format: "png" });
      writeFileSync(join(outDir, `${lang}-${name}.png`), Buffer.from(r.data, "base64"));
      console.log(`  ${join(outDir, `${lang}-${name}.png`)}`);
    };
    // click the first visible button whose text matches the regular expression
    const click = (re) => run(`(() => { const b = [...document.querySelectorAll("button")].find(x => /${re}/i.test(x.textContent) && !x.disabled); if (b) b.click(); return !!b; })()`);

    await send("Page.enable");
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/?t=${token}` });
    await waitFor(`document.querySelectorAll("button.card.choice").length > 0`, "the role cards");
    await run(`localStorage.setItem("uavlink.lang", ${JSON.stringify(lang)}); location.reload()`);
    await sleep(600);
    await waitFor(`document.querySelectorAll("button.card.choice").length > 0`, "the role cards after reload");

    // step 1: role (the CM5 card is the first one)
    await run(`document.querySelector("button.card.choice").click()`);
    await snap("step1");
    await click(lang === "vi" ? "Tiếp tục" : "Continue");

    // step 2: preset dgp-current
    await waitFor(`document.querySelector("h2") && /2\\./.test(document.querySelector("h2").textContent)`, "step 2");
    await click(lang === "vi" ? "Cấu hình mặc định" : "Default configuration");
    await snap("step2");
    await click(lang === "vi" ? "Tiếp tục" : "Continue");

    // step 3: settings; fill every empty required text field so that validation passes
    await waitFor(`document.querySelector("h2") && /3\\./.test(document.querySelector("h2").textContent)`, "step 3");
    await run(`[...document.querySelectorAll("input[type=text],input:not([type])")].forEach(i => { if (!i.value && i.required) { i.value = "203.0.113.10"; i.dispatchEvent(new Event("input", {bubbles: true})); i.dispatchEvent(new Event("change", {bubbles: true})); } })`);
    await snap("step3");
    await click(lang === "vi" ? "Tiếp tục" : "Continue");

    // step 4: the pre-check runs by itself (preview only on WSL); wait until the progress list is final
    await waitFor(`document.querySelector("h2") && /4\\./.test(document.querySelector("h2").textContent)`, "step 4");
    await waitFor(`!/Working…|Đang làm việc…/.test(document.body.innerText)`, "the pre-check to finish", 150);
    await sleep(1500);
    await snap("step4");

    // step 5 (Apply is disabled on WSL) and the Status page
    await run(`location.hash = "#step=5"`);
    await waitFor(`document.querySelector("h2") && /5\\./.test(document.querySelector("h2").textContent)`, "step 5");
    await snap("step5");
    await run(`location.hash = "#status"`);
    await sleep(2500);
    await snap("status");
    ws.close();
  } finally {
    edge.kill();
    await sleep(500);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* Edge may still hold files */ }
  }
}

await shotsFor("vi", 9333);
await shotsFor("en", 9334);
