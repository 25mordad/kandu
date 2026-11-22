// Opens each look in a real headless browser, in the language each case names, and reads the upgrade notice: its
// title, button, entries and links. When the plan says so, it presses «Mark as read» with the token, waits for the
// notice to go, then opens the page in another browser (a new context, with its own cookies and storage) and says
// whether the notice is hidden there too. Prints what each step shows as JSON.
// Usage: node drive_upgrade.js <browser> <dashboard url> <empty profile folder> <token> <plan as JSON>
// The plan is {cases: [{look, lang}], press}. It talks to the browser over the DevTools protocol, with no library.
const {spawn} = require("child_process");
const [chromiumFile, base, profile, apiToken, planJson] = process.argv.slice(2);
const plan = JSON.parse(planJson);
const browser = spawn(chromiumFile, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0",
                                    `--user-data-dir=${profile}`, "about:blank"], {stdio: ["ignore", "ignore", "pipe"]});
const fail = message => { console.error(message); browser.kill(); process.exit(1); };
setTimeout(() => fail("the browser took too long"), 150000);

const endpoint = new Promise((resolve, reject) => {
  let said = "";
  browser.stderr.on("data", chunk => {
    said += chunk;
    const found = said.match(/DevTools listening on (ws:\/\/\S+)/);
    if (found) resolve(found[1]);
  });
  browser.on("exit", () => reject(new Error(`the browser stopped: ${said}`)));
});

let socket, next = 0, session = null;
const waiting = new Map(), listeners = [];
function send(method, params = {}) {
  const id = ++next;
  socket.send(JSON.stringify({id, method, params, ...(session ? {sessionId: session} : {})}));
  return new Promise((resolve, reject) => waiting.set(id, {resolve, reject, method}));
}
const once = method => new Promise(resolve => listeners.push({method, resolve}));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const errors = [];

async function evaluate(expression) {
  const answer = await send("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true});
  if (answer.exceptionDetails) throw new Error(`${expression}: ${answer.exceptionDetails.exception?.description || answer.exceptionDetails.text}`);
  return answer.result.value;
}

async function ready() {
  for (let i = 0; i < 100; i++) {
    if (await evaluate('typeof lastState !== "undefined" && lastState !== null')) {
      await sleep(300);
      return;
    }
    await sleep(100);
  }
  fail("the page never drew the state");
}

async function load(action) {
  const loaded = once("Page.loadEventFired");
  await action();
  await loaded;
  await ready();
}

const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}))`);

async function waitFor(expression) {
  for (let i = 0; i < 50; i++) {
    if (await evaluate(expression)) return true;
    await sleep(100);
  }
  return false;
}

// What the notice shows: whether it is hidden, its title, its button, each entry's text and each entry's link.
const seen = () => evaluate(`(() => {
  const box = document.getElementById("upgrade");
  return {hidden: box.hidden || getComputedStyle(box).display === "none",
          title: document.getElementById("upgradetitle").textContent,
          button: document.getElementById("upgraderead").textContent,
          entries: [...box.querySelectorAll(".upgrade-entry")].map(e => e.textContent),
          links: [...box.querySelectorAll(".upgrade-entry a[href]")].map(a => a.href)};
})()`);

// A page in its own browser context, so it shares no cookie or storage with the others.
async function openPage(look) {
  session = null;  // a context and its page are made by the browser, not by the page before
  const {browserContextId} = await send("Target.createBrowserContext");
  const {targetId} = await send("Target.createTarget", {url: "about:blank", browserContextId});
  session = (await send("Target.attachToTarget", {targetId, flatten: true})).sessionId;
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");
  await send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await send("Network.setCookie", {name: "kandu-look", value: look, url: base});
  await load(() => send("Page.navigate", {url: base + "/"}));
}

async function run() {
  socket = new WebSocket(await endpoint);
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    if (message.id && waiting.has(message.id)) {
      const {resolve, reject, method} = waiting.get(message.id);
      waiting.delete(message.id);
      if (message.error) reject(new Error(`${method}: ${message.error.message}`));
      else resolve(message.result);
      return;
    }
    if (message.method === "Runtime.exceptionThrown") {
      const details = message.params.exceptionDetails;
      errors.push(details.exception?.description || details.text);
    }
    for (const listener of listeners.filter(l => l.method === message.method)) {
      listeners.splice(listeners.indexOf(listener), 1);
      listener.resolve(message.params);
    }
  };
  await new Promise(resolve => { socket.onopen = resolve; });

  const results = [];
  for (const {look, lang} of plan.cases) {
    await openPage(look);
    const steps = {look, lang, page: await evaluate("document.documentElement.dataset.look || LOOK_HERE")};
    if (await evaluate("lang") !== lang) {
      await evaluate(`setLanguage(${JSON.stringify(lang)})`);
      await waitFor(`lang === ${JSON.stringify(lang)}`);
    }
    await waitFor('!document.getElementById("upgrade").hidden');
    await sleep(300);
    steps.shown = await seen();
    if (plan.press) {
      await evaluate(`token = ${JSON.stringify(apiToken)};`);
      await click("#upgraderead");
      steps.gone = await waitFor('document.getElementById("upgrade").hidden');
      await openPage(look);
      await sleep(300);
      steps.elsewhere_hidden = (await seen()).hidden;
    }
    results.push(steps);
  }
  console.log(JSON.stringify({results, errors}));
  browser.kill();
  process.exit(0);
}

run().catch(error => fail(error.stack || String(error)));
