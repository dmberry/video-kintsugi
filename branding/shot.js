// Screenshot the running page with headless Chrome (no npm packages), for the README.
// node shot.js <url> <output.png> <width> [js to run first] [dark]
const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");

const [url, output, width, js, dark] = process.argv.slice(2);
const port = 9400 + Math.floor(Math.random() * 400);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "shot-chrome-"));
const chrome = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
  "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  "--hide-scrollbars", `--user-data-dir=${profile}`, `--remote-debugging-port=${port}`, "about:blank",
], { stdio: "ignore" });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  let ws;
  for (let i = 0; !ws && i < 50; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = list.find((t) => t.type === "page");
      if (page) ws = new WebSocket(page.webSocketDebuggerUrl);
    } catch (e) { await sleep(200); }
  }
  let id = 0;
  const pending = {};
  const send = (method, params = {}) => new Promise((res) => {
    pending[++id] = res;
    ws.send(JSON.stringify({ id, method, params }));
  });
  ws.onmessage = (m) => {
    const d = JSON.parse(m.data);
    if (d.id && pending[d.id]) { pending[d.id](d.result || d.error); delete pending[d.id]; }
  };
  await new Promise((r) => { ws.onopen = r; });
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: +width, height: 900, deviceScaleFactor: 2, mobile: false });
  if (dark) await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  await send("Page.navigate", { url });
  await sleep(1200);
  if (js) await send("Runtime.evaluate", { expression: js, awaitPromise: true });
  await sleep(600);
  const { result } = await send("Runtime.evaluate", { expression: "document.documentElement.scrollHeight", returnByValue: true });
  const shot = await send("Page.captureScreenshot",
    { format: "png", captureBeyondViewport: true, clip: { x: 0, y: 0, width: +width, height: result.value, scale: 1 } });
  fs.writeFileSync(output, Buffer.from(shot.data, "base64"));
  ws.close();
  chrome.kill("SIGKILL");
  fs.rmSync(profile, { recursive: true, force: true });
})().catch((e) => { console.error(e); chrome.kill("SIGKILL"); process.exit(1); });
