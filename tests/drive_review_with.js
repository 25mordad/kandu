// Opens each look in a real headless browser, as a user would, and opens a project's settings pop-up from the
// Settings page. It chooses Codex for the reviewers, on a machine that has none, and saves. Prints what each step
// shows as JSON.
// Usage: node drive_review_with.js <browser> <dashboard url> <empty profile folder> <token> <cases as JSON>
// A case is {look, lang, width, height, mobile}. It talks to the browser over the DevTools protocol, with no library.
const {spawn} = require("child_process");
const [chromiumFile, base, profile, apiToken, casesJson] = process.argv.slice(2);
const cases = JSON.parse(casesJson);

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
    if (await evaluate('typeof lastState !== "undefined" && lastState !== null && typeof lastQueue !== "undefined" && lastQueue !== null')) {
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

// A click as a user's, which bubbles; the meadow's name is SVG and has no click() of its own.
const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}))`);

async function waitFor(expression) {
  for (let i = 0; i < 50; i++) {
    if (await evaluate(expression)) return true;
    await sleep(100);
  }
  return false;
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
    await send("Network.setCookie", {name: "kandu-look", value: c.look, url: base});
    await send("Emulation.setDeviceMetricsOverride", {width: c.width, height: c.height, deviceScaleFactor: 1, mobile: c.mobile});
    await load(() => send("Page.navigate", {url: base + "/"}));
    await evaluate(`localStorage.setItem("kandu-language", ${JSON.stringify(c.lang)}); localStorage.setItem("kandu-tab", "settings");`);
    await load(() => send("Page.reload"));
    await evaluate('showTab("settings")');
    await evaluate(`token = ${JSON.stringify(apiToken)};`);
    await click('#projectsettings [data-settings-open]');
    const steps = {look: await evaluate("document.documentElement.dataset.look"), open: await waitFor('document.getElementById("setsheet").open')};
    const field = key => `document.querySelector('#setbody [data-draft="${key}"]')?.closest(".set-field")`;
    steps.label = await evaluate(`${field("reviewer_cli")}?.querySelector(".set-label span")?.textContent || null`);
    await click('[data-draft="reviewer_cli"][data-value="codex"]');
    steps.asked = await waitFor(`Boolean(${field("reviewer_cli")}?.querySelector("[data-codex-why]"))`);
    steps.shown = await evaluate(`(() => {
      const html = document.documentElement, body = document.getElementById("setbody");
      return {why: ${field("reviewer_cli")}?.querySelector("[data-codex-why]")?.textContent || null,
              workers: Boolean(${field("worker_cli")}?.querySelector("[data-codex-why]")),
              pressed: document.querySelector('#setbody [data-draft="reviewer_cli"][aria-pressed="true"]')?.dataset.value || null,
              sideways: html.scrollWidth > html.clientWidth || body.scrollWidth > body.clientWidth};
    })()`);
    await click("[data-set-save]");
    steps.closed = await waitFor('!document.getElementById("setsheet").open');
    const name = "website";
    steps.saved = await evaluate(`fetch("/api/v1/state").then(r => r.json()).then(s => s.project_settings.find(p => p.name === ${JSON.stringify(name)}))
      .then(p => ({worker_cli: p.worker_cli, reviewer_cli: p.reviewer_cli}))`);
    // The next case starts on Claude Code.
    await evaluate(`fetch("/api/v1/projects/${name}/settings?reviewer_cli=claude", {method: "POST", headers: {Authorization: "Bearer " + token}})`);
    results.push({...c, steps});
  }
  console.log(JSON.stringify({results, errors}));
  browser.kill();
  process.exit(0);
}

run().catch(error => fail(error.stack || String(error)));
