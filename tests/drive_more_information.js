// Opens each look of the dashboard in a real headless browser, in a language and at a width, with the queen asking a
// question and asking to drop a blocker, and says what the buttons that open a waiting task's sheet read, whether each
// shows its full words inside the page, and what opens when the queen's ask's button is clicked (#144).
// Usage: node drive_more_information.js <browser> <dashboard url> <empty profile folder> <cases as JSON>
// A case is {look, lang, width, height, mobile}. It talks to the browser over the DevTools protocol, with no library.
const {spawn} = require("child_process");
const [browserPath, base, profile, casesJson] = process.argv.slice(2);
const cases = JSON.parse(casesJson);
const browser = spawn(browserPath, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--password-store=basic", "--hide-scrollbars", "--remote-debugging-port=0",
                                    `--user-data-dir=${profile}`, "about:blank"], {stdio: ["ignore", "ignore", "pipe"]});
const fail = message => { console.error(message); browser.kill(); process.exit(1); };
setTimeout(() => fail("the browser took too long"), 280000);
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
// Each drawn button that opens a task's sheet in the queen's asks, and on the card of "Waits for you" where the look has
// one, as Kandu Clean has on Today: its words, and whether they are cut, wrap past the button or leave the page. Then the labels the page gives the
// same button on every item of "Waits for you", drawn or not.
const MEASURE = `(() => {
  const W = innerWidth;
  const button = b => { const r = b.getBoundingClientRect();
    return {text: b.textContent.trim(), cut: b.scrollWidth > b.clientWidth + 1 || b.scrollHeight > b.clientHeight + 1,
            outside: r.left < -1 || r.right > W + 1, empty: !r.width || !r.height}; };
  const drawn = sel => [...document.querySelectorAll(sel)].map(button);
  return {look: document.documentElement.dataset.look, lang: document.documentElement.lang,
          asks: drawn("#asks .ask [data-review]"), card: drawn(".wait-card [data-review]"),
          waits: typeof lastState !== "undefined" && lastState && lastQueue ? waitItems(lastState, lastQueue).map(i => ({task: i.task, label: i.actions.at(-1).label, attrs: i.actions.at(-1).attrs})) : []};
})()`;
const CLICK = `(() => {
  const ask = document.querySelector("#asks .ask [data-review]");
  ask.click();
  return ask.dataset.review;
})()`;
const SHEET = `(() => ({open: document.getElementById("sheet").open, text: document.getElementById("sheetbody").textContent}))()`;
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
  const results = [];
  for (const c of cases) {
    await send("Network.setCookie", {name: "kandu-look", value: c.look, url: base});
    await send("Emulation.setDeviceMetricsOverride", {width: c.width, height: c.height, deviceScaleFactor: 1, mobile: c.mobile});
    await send("Page.navigate", {url: base + "/"}); await sleep(600);
    await evaluate(`localStorage.setItem("kandu-language", "${c.lang}"); localStorage.setItem("kandu-tab", "${c.look === "clean" ? "today" : "meadow"}")`);
    await send("Page.navigate", {url: base + "/"});
    let seen = null;
    for (let i = 0; i < 40; i++) {  // until the asks, and the items that wait, are drawn
      await sleep(250);
      seen = await evaluate(MEASURE).catch(() => null);
      if (seen && seen.asks.length >= 2 && seen.waits.length && (c.look !== "clean" || seen.card.length)) break;
    }
    if (seen && c.look === "clean") {  // Kandu Clean shows the asks off Today and Tasks: read them on Bazras
      const card = seen.card;
      await evaluate('document.querySelector(\'.tabs [data-tab="bazras"]\').click()');
      await sleep(250);
      seen = {...await evaluate(MEASURE), card};
    }
    let sheet = null;
    if (seen && seen.asks.length) {
      const task = await evaluate(CLICK);
      for (let i = 0; i < 20; i++) {
        await sleep(250);
        sheet = await evaluate(SHEET);
        if (sheet.open && sheet.text) break;
      }
      sheet.task = task;
    }
    results.push({...c, seen, sheet});
  }
  console.log(JSON.stringify({errors, results}));
  browser.kill(); process.exit(0);
})().catch(error => fail(error.stack || String(error)));
