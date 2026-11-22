// Opens the dashboard in a real headless browser, as a user would, and prints what each step shows as JSON.
// Usage: node drive_looks.js <browser> <dashboard url> <empty profile folder> <cases as JSON>
// A case is {lang, width, height, mobile}. Each starts from a browser that had chosen the honeycomb look, then
// switches from the look it opens in to the other and back, reloading after each switch, and on every page of each
// look says whether the page scrolls sideways. It talks to the browser over the DevTools protocol, with no library.
const {spawn} = require("child_process");
const [browserPath, base, profile, casesJson] = process.argv.slice(2);
const cases = JSON.parse(casesJson);

const browser = spawn(browserPath, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--password-store=basic", "--remote-debugging-port=0",
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

// A click that reloads the page runs just after the call returns, so the call never loses its page on the way.
const soon = click => evaluate(`setTimeout(() => ${click}, 0)`);

// A page is ready once it has the state from the API and has drawn it.
async function ready() {
  for (let i = 0; i < 100; i++) {
    if (await evaluate('typeof lastState !== "undefined" && lastState !== null && typeof lastQueue !== "undefined" && lastQueue !== null')) {
      await sleep(300);
      return;
    }
    await sleep(100);
  }
  fail(`the page never drew the state: ${await evaluate("document.documentElement.outerHTML.slice(0, 300)")}`);
}

async function load(action) {
  const loaded = once("Page.loadEventFired");
  await action();
  await loaded;
  await ready();
}

// What the page shows: its look, language and direction, and each of its pages that scrolls sideways.
async function seen() {
  return evaluate(`(() => {
    const html = document.documentElement;
    const sideways = [];
    const pages = [...document.querySelectorAll("[data-panel]")].map(el => el.dataset.panel);
    const was = tab;
    for (const page of pages) {
      showTab(page);
      if (html.scrollWidth > html.clientWidth) sideways.push(page);
    }
    showTab(was);
    return {look: html.dataset.look, lang: html.lang, dir: html.dir, pages, sideways};
  })()`);
}

// The Look menu, opened from the header's Look button: the looks it lists, the one it marks, then closed again.
async function menu() {
  return evaluate(`(() => {
    document.getElementById("look").click();
    const box = document.getElementById("lookmenu");
    const seen = {open: box.open, looks: [...box.querySelectorAll("[data-choose-look]")].map(b => b.dataset.chooseLook),
                  current: [...box.querySelectorAll('[aria-current="true"]')].map(b => b.dataset.chooseLook),
                  sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth};
    box.close();
    return seen;
  })()`);
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
  const {targetId} = await send("Target.createTarget", {url: "about:blank"});
  session = (await send("Target.attachToTarget", {targetId, flatten: true})).sessionId;
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");

  const results = [];
  for (const c of cases) {
    await send("Network.clearBrowserCookies");
    await send("Storage.clearDataForOrigin", {origin: base, storageTypes: "all"});
    await send("Emulation.setDeviceMetricsOverride", {width: c.width, height: c.height, deviceScaleFactor: 1, mobile: c.mobile});
    // Before the switch came back, the browser chose the honeycomb look and its language.
    await load(() => send("Page.navigate", {url: base + "/"}));
    await evaluate(`localStorage.setItem("kandu-look", "honeycomb"); localStorage.setItem("kandu-language", ${JSON.stringify(c.lang)});`);
    const steps = {};
    await load(() => send("Page.reload"));
    steps.first = await seen();
    steps.menu = await menu();
    await load(() => soon('(document.getElementById("look").click(), document.querySelector(\'#lookmenu [data-choose-look="clean"]\').click())'));
    steps.switched = await seen();
    await load(() => send("Page.reload"));
    steps.reloaded = await seen();
    steps.clean_menu = await menu();
    await evaluate('document.getElementById("more").click()');
    steps.more_open = await evaluate('document.getElementById("moremenu").open');
    await load(() => soon('(document.querySelector(\'#moremenu [data-menu="look"]\').click(), document.querySelector(\'#lookmenu [data-choose-look="classic"]\').click())'));
    steps.back = await seen();
    await load(() => send("Page.reload"));
    steps.back_reloaded = await seen();
    results.push({...c, steps});
  }
  console.log(JSON.stringify({results, errors}));
  browser.kill();
  process.exit(0);
}

run().catch(error => fail(error.stack || String(error)));
