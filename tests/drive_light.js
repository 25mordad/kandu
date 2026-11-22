// Opens each look in a real headless browser and says how light an open tab is: how many times a burst of stream
// events reads /state, whether hidden tabs were drawn, whether a refresh with the same data redraws the meadow, what
// moves on it, and what the Calm switch does, across a reload.
// Usage: node drive_light.js <browser> <dashboard url> <empty profile folder> <cases as JSON>
// A case is {look, width, height, mobile}. The page's live stream is a stand-in this script feeds, so the events
// arrive when it says; everything else is the real dashboard. It talks to the browser over the DevTools protocol.
const {spawn} = require("child_process");
const [browserPath, base, profile, casesJson] = process.argv.slice(2);
const cases = JSON.parse(casesJson);
const browser = spawn(browserPath, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--password-store=basic", "--hide-scrollbars", "--remote-debugging-port=0",
                                    `--user-data-dir=${profile}`, "about:blank"], {stdio: ["ignore", "ignore", "pipe"]});
const fail = message => { console.error(message); browser.kill(); process.exit(1); };
setTimeout(() => fail("the browser took too long"), 170000);
const endpoint = new Promise((resolve, reject) => {
  let said = "";
  browser.stderr.on("data", chunk => { said += chunk; const found = said.match(/DevTools listening on (ws:\/\/\S+)/); if (found) resolve(found[1]); });
  browser.on("exit", () => reject(new Error(`the browser stopped: ${said}`)));
});
let socket, next = 0, session = null;
const waiting = new Map(), errors = [];
const send = (method, params = {}) => new Promise((resolve, reject) => {
  const id = ++next; waiting.set(id, {resolve, reject});
  socket.send(JSON.stringify({id, method, params, ...(session ? {sessionId: session} : {})}));
});
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function evaluate(expression) {
  const answer = await send("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true});
  if (answer.exceptionDetails) throw new Error(`${expression}: ${answer.exceptionDetails.exception?.description || answer.exceptionDetails.text}`);
  return answer.result.value;
}

// Before the page's own script: the stream is a stand-in that this script feeds, every read of /state is counted,
// and the delay of every timer the page sets is kept.
const STAND_IN = `(() => {
  window.__streams = []; window.__reads = []; window.__delays = [];
  class FakeStream { constructor(url) { this.url = url; this.closed = false; window.__streams.push(this); }
    close() { this.closed = true; } emit(data) { if (!this.closed && this.onmessage) this.onmessage({data: JSON.stringify(data)}); } }
  window.EventSource = FakeStream;
  const fetched = window.fetch.bind(window);
  window.fetch = (url, ...rest) => { if (String(url).startsWith("/api/v1/state")) window.__reads.push(Date.now()); return fetched(url, ...rest); };
  const later = window.setTimeout.bind(window), every = window.setInterval.bind(window);
  window.setTimeout = (fn, ms, ...rest) => { window.__delays.push(ms || 0); return later(fn, ms, ...rest); };
  window.setInterval = (fn, ms, ...rest) => { window.__delays.push(ms || 0); return every(fn, ms, ...rest); };
})()`;
const open = () => evaluate("window.__streams.filter(s => !s.closed).length");

async function ready() {
  for (let i = 0; i < 100; i++) {
    if (await evaluate("typeof lastState !== 'undefined' && lastState !== null && typeof lastQueue !== 'undefined' && lastQueue !== null").catch(() => false)) {
      await sleep(400);
      return;
    }
    await sleep(100);
  }
  fail("the page never drew the state");
}

async function load(url) {
  await send("Page.navigate", {url});
  await sleep(300);
  await ready();
}

// The meadow's page: Today with the meadow opened in Kandu Clean, the Meadow tab in the classic looks.
const MEADOW = `(() => {
  if (document.documentElement.dataset.look === "clean") {
    document.querySelector('[data-tab="today"]').click();
    const box = document.getElementById("meadowbox");
    if (!box.open) box.querySelector("summary").click();
  } else document.querySelector('.tabs [data-tab="meadow"]').click();
})()`;
// What moves on the meadow: each running animation's name and what it moves, and the bees that fly.
const MOTION = `new Promise(done => setTimeout(() => done({
  css: document.getAnimations().filter(a => a.playState === "running").map(a => a.animationName + " " + (a.effect.target.closest(".flying") ? "flying" : a.effect.target.closest("#scene") ? "meadow" : "frame")).sort(),
  flights: document.querySelectorAll("#scene animateMotion").length}), 200))`;
// Each tab that draws from the state and what it holds; "" while it was never drawn.
const TABS = `(() => Object.fromEntries([["tasks", "projects"], ["settings", "projectsettings"], ["bazras", "bazrasreports"], ["days", "byday"]]
  .map(([tab, id]) => [tab, document.getElementById(id).innerHTML.trim() ? "drawn" : ""])))()`;
const TASKS_TAB = `document.documentElement.dataset.look === "clean" ? "tasks" : "work"`;
// The Calm switch: in the header, or in More on a phone in Kandu Clean.
const CALM = `(() => {
  const head = document.getElementById("calm");
  if (head && head.offsetParent) { head.click(); return "header"; }
  document.getElementById(document.getElementById("morenav")?.offsetParent ? "morenav" : "more").click();
  document.querySelector('#moremenu [data-menu="calm"]').click();
  return "more";
})()`;
const CALM_SEEN = `({calm: document.documentElement.dataset.calm === "on",
  pressed: [...document.querySelectorAll("#calm, #moremenu [data-menu=calm]")].filter(b => b.offsetParent || b.id === "calm").map(b => b.getAttribute("aria-pressed"))})`;

(async () => {
  socket = new WebSocket(await endpoint);
  socket.onmessage = ({data}) => {
    const message = JSON.parse(data);
    if (message.id && waiting.has(message.id)) { const {resolve, reject} = waiting.get(message.id); waiting.delete(message.id);
      message.error ? reject(new Error(message.error.message)) : resolve(message.result); }
    if (message.method === "Runtime.exceptionThrown") errors.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
  };
  await new Promise(resolve => { socket.onopen = resolve; });
  const {targetId} = await send("Target.createTarget", {url: "about:blank"});
  session = (await send("Target.attachToTarget", {targetId, flatten: true})).sessionId;
  await send("Runtime.enable"); await send("Page.enable"); await send("Network.enable");
  await send("Page.addScriptToEvaluateOnNewDocument", {source: STAND_IN});
  const results = [];
  for (const c of cases) {
    await send("Network.clearBrowserCookies");
    await send("Network.setCookie", {name: "kandu-look", value: c.look, url: base});
    await send("Emulation.setDeviceMetricsOverride", {width: c.width, height: c.height, deviceScaleFactor: 1, mobile: c.mobile});
    await send("Page.navigate", {url: base + "/"}); await sleep(300);
    await evaluate(`localStorage.clear(); localStorage.setItem("kandu-tab", document.documentElement.dataset.look === "clean" ? "today" : "meadow")`);
    await load(base + "/");
    const seen = {};
    seen.hidden_tabs = await evaluate(TABS);
    await evaluate(`showTab(${TASKS_TAB})`);
    seen.opened_tab = (await evaluate(TABS)).tasks;

    // A burst: 20 events in 2 seconds, once 5 seconds passed since the last read.
    await evaluate(MEADOW);
    const last = await evaluate("Math.max(0, ...window.__reads)");
    await sleep(Math.max(0, last + 5200 - await evaluate("Date.now()")));
    const before = await evaluate("window.__reads.length"), shownAtOnce = [];
    for (let i = 0; i < 20; i++) {
      shownAtOnce.push(await evaluate(`(() => {
        const rows = () => document.querySelectorAll("#live tr").length, was = rows();
        window.__streams.filter(s => !s.closed).at(-1).emit({ts: new Date().toISOString(), type: "phase", task: "o/r#1", phase: "step ${i}"});
        return rows() === Math.min(was + 1, 200) && document.getElementById("live").textContent.includes("step ${i}");
      })()`));
      await sleep(100);
    }
    await sleep(300);
    seen.burst_reads = await evaluate("window.__reads.length") - before;
    seen.lines_at_once = shownAtOnce.every(Boolean);
    await sleep(5200);
    seen.reads_after = await evaluate("window.__reads.length") - before;

    // The same data again leaves the meadow's drawing as it is.
    await evaluate(`(window.__scene = document.querySelector("#scene svg"), refreshState())`);
    seen.meadow_kept = await evaluate(`!!window.__scene && document.querySelector("#scene svg") === window.__scene`);
    seen.motion = await evaluate(MOTION);

    // Calm: nothing moves, no stream, a read once a minute; kept across a reload; off brings both back.
    await calm(seen).catch(error => { seen.failed = String(error).slice(0, 300); });
    results.push({...c, seen});
  }
  console.log(JSON.stringify({errors, results}));
  browser.kill(); process.exit(0);
})().catch(error => fail(error.stack || String(error)));

async function calm(seen) {
    await evaluate("window.__delays.length = 0");
    seen.calm_from = await evaluate(CALM);
    await sleep(300);
    seen.calm = await evaluate(CALM_SEEN);
    seen.calm_motion = await evaluate(MOTION);
    seen.calm_streams = await open();
    seen.calm_minute = await evaluate("window.__delays.includes(60000)");
    await load(base + "/");
    await evaluate(MEADOW);
    seen.reloaded = await evaluate(CALM_SEEN);
    seen.reloaded_streams = await evaluate("window.__streams.length");
    seen.reloaded_motion = await evaluate(MOTION);
    await evaluate(CALM);
    await sleep(600);
    seen.off = await evaluate(CALM_SEEN);
    seen.off_streams = await open();
    seen.off_motion = await evaluate(MOTION);
}
