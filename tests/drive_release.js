// Opens each look in a real headless browser, as a user would, and presses «Cut a release»: it says what the
// dialog shows, closes it once without a yes, then, when the plan says so, opens it again and confirms. Prints what
// each step shows as JSON.
// Usage: node drive_release.js <browser> <dashboard url> <empty profile folder> <token> <plan as JSON>
// The plan is {looks, confirm}. It talks to the browser over the DevTools protocol, with no library.
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

// What the release dialog shows: whether it is open, its question, its text and whether it offers the confirm.
const seen = () => evaluate(`(() => {
  const box = document.getElementById("releasebox");
  return {open: box.open, head: document.getElementById("releasehead").textContent,
          text: document.getElementById("releasetext").textContent, confirm_hidden: document.getElementById("releasenow").hidden};
})()`);

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
  await send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});

  const results = [];
  for (const look of plan.looks) {
    await send("Network.clearBrowserCookies");
    await send("Network.setCookie", {name: "kandu-look", value: look, url: base});
    await load(() => send("Page.navigate", {url: base + "/"}));
    const steps = {look, page: await evaluate("document.documentElement.dataset.look || LOOK_HERE")};
    steps.action = await waitFor('document.querySelector("[data-release]") !== null');
    await click("[data-release]");
    await waitFor('document.getElementById("releasebox").open && document.getElementById("releasetext").textContent !== ""');
    steps.shown = await seen();
    // Closed without a yes: nothing is asked of the server but the proposal.
    await click('#releasebox button[value="no"]');
    steps.closed = await waitFor('!document.getElementById("releasebox").open');
    if (plan.confirm) {
      await evaluate(`token = ${JSON.stringify(apiToken)};`);
      await click("[data-release]");
      await waitFor('document.getElementById("releasebox").open && !document.getElementById("releasenow").hidden');
      await click("#releasenow");
      await waitFor('document.getElementById("releasenow").hidden');
      steps.after = await seen();
    }
    results.push(steps);
  }
  console.log(JSON.stringify({results, errors}));
  browser.kill();
  process.exit(0);
}

run().catch(error => fail(error.stack || String(error)));
