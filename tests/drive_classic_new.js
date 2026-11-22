// Opens the Classic new look in a real headless browser at several widths and says, for each, how tall the header is,
// whether anything on the page goes past its edge, and what the explanation under the pointer says over two buttons.
// Usage: node drive_classic_new.js <browser> <dashboard url> <empty profile folder> <cases as JSON>
// A case is {lang, width, height, mobile}. It talks to the browser over the DevTools protocol, with no library.
const {spawn} = require("child_process");
const [browserPath, base, profile, casesJson] = process.argv.slice(2);
const cases = JSON.parse(casesJson);
const browser = spawn(browserPath, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--password-store=basic", "--hide-scrollbars", "--remote-debugging-port=0",
                                    `--user-data-dir=${profile}`, "about:blank"], {stdio: ["ignore", "ignore", "pipe"]});
const fail = message => { console.error(message); browser.kill(); process.exit(1); };
setTimeout(() => fail("the browser took too long"), 150000);
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
// The token gauge under each patch on the meadow: how many there are, which of them cover a name, a switch or a flower,
// or sit above their own name or past the meadow's edge, and what the first one says on hover.
const GAUGES = `const gauges = () => {
  const box = sel => [...document.querySelectorAll("#scene " + sel)].map(e => e.getBoundingClientRect());
  const meet = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
  const scene = document.querySelector("#scene svg").getBoundingClientRect(), names = box(".patch-name");
  const others = [...names, ...box(".switch"), ...box("[data-review]")];
  const all = [...document.querySelectorAll("#scene .tgauge")];
  const tip = all[0] && (all[0].dispatchEvent(new MouseEvent("mouseover", {bubbles: true})), document.querySelector(".tip").textContent);
  return {count: all.length, names: names.length, tip, used: all.map(g => g.querySelector("b").textContent), over: all.map(g => g.classList.contains("over")),
          bad: all.map(g => g.getBoundingClientRect()).flatMap((g, i) => [
            ...others.filter(o => meet(g, o)).map(o => i + " covers " + [o.left, o.top, o.width, o.height].map(Math.round).join(",")),
            ...(names.some(n => n.bottom <= g.top + 1 && n.left < g.right && g.left < n.right) ? [] : [i + " is not under a name"]),
            ...(g.left < scene.left || g.right > scene.right || g.bottom > scene.bottom ? [i + " is past the meadow"] : [])])};
};`;
const MEASURE = `(() => { ${GAUGES}
  const W = innerWidth, bar = document.querySelector(".top .bar");
  const past = [...document.querySelectorAll("body *")].filter(e => { const r = e.getBoundingClientRect();
    return r.width && (r.right > W + 1 || r.left < -1) && !e.closest(".tabs, svg, dialog, .tip"); }).map(e => e.tagName + "." + e.className);
  const tipOver = id => { document.getElementById(id).dispatchEvent(new MouseEvent("mouseover", {bubbles: true})); const tip = document.querySelector(".tip"); return tip.hidden ? "" : tip.textContent; };
  return {look: document.documentElement.dataset.look, lang: document.documentElement.lang, header: document.querySelector(".top").offsetHeight,
          barPast: bar.scrollWidth > bar.clientWidth + 1, past: past.slice(0, 5), stats: document.querySelectorAll("#stats .schip").length,
          hive: !!document.querySelector("#stats .hivechip svg"), fields: [...document.querySelectorAll("#fieldtabs button")].map(b => b.dataset.field),
          wake: document.getElementById("wake").dataset.mode, wakeTip: tipOver("wake"), restartTip: tipOver("restart"), gauges: gauges()};
})()`;
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
  await send("Network.setCookie", {name: "kandu-look", value: "classic-new", url: base});
  const results = [];
  for (const c of cases) {
    await send("Emulation.setDeviceMetricsOverride", {width: c.width, height: c.height, deviceScaleFactor: 1, mobile: c.mobile});
    await send("Page.navigate", {url: base + "/"}); await sleep(600);
    await evaluate(`localStorage.setItem("kandu-language", "${c.lang}"); localStorage.setItem("kandu-field", "all")`);
    await send("Page.navigate", {url: base + "/"});
    let seen = null;
    for (let i = 0; i < 40; i++) {  // until the state and the queue are drawn
      await sleep(250);
      seen = await evaluate(MEASURE).catch(() => null);
      if (seen && seen.stats && seen.fields.length) break;
    }
    results.push({...c, seen});
  }
  console.log(JSON.stringify({errors, results}));
  browser.kill(); process.exit(0);
})().catch(error => fail(error.stack || String(error)));
