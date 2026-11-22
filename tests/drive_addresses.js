// Opens the dashboard in a real headless browser and walks its views by address, as a user would, then prints what each
// step showed as JSON. Usage: node drive_addresses.js <browser> <dashboard url> <empty profile folder> <plan as JSON>
// The plan is {looks, task, project, run, lost}: in each look it opens the task, a project and a run log, reloads, goes
// Back and Forward, then opens each address it saw in every look, and each lost address once.
const {spawn} = require("child_process");
const [browserPath, base, profile, planJson] = process.argv.slice(2);
const plan = JSON.parse(planJson);

const browser = spawn(browserPath, ["--headless", "--no-sandbox", "--disable-gpu", "--no-first-run", "--password-store=basic", "--remote-debugging-port=0",
                                    `--user-data-dir=${profile}`, "about:blank"], {stdio: ["ignore", "ignore", "pipe"]});
const fail = message => { console.error(message); browser.kill(); process.exit(1); };
setTimeout(() => fail("the browser took too long"), 170000);

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
const waiting = new Map(), listeners = [], errors = [];
function send(method, params = {}) {
  const id = ++next;
  socket.send(JSON.stringify({id, method, params, ...(session ? {sessionId: session} : {})}));
  return new Promise((resolve, reject) => waiting.set(id, {resolve, reject, method}));
}
const once = method => new Promise(resolve => listeners.push({method, resolve}));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

async function evaluate(expression) {
  const answer = await send("Runtime.evaluate", {expression, awaitPromise: true, returnByValue: true});
  if (answer.exceptionDetails) throw new Error(`${expression}: ${answer.exceptionDetails.exception?.description || answer.exceptionDetails.text}`);
  return answer.result.value;
}

// A page has settled once it drew the state and its address no longer changes.
async function settled() {
  let last = null;
  for (let i = 0; i < 100; i++) {
    const now = await evaluate('typeof lastState !== "undefined" && lastState !== null && typeof lastQueue !== "undefined" && lastQueue !== null'
                               + ' && !markView.held ? location.pathname : null');
    if (now !== null && now === last) return;
    last = now;
    await sleep(200);
  }
  fail(`the page never settled: ${await evaluate("location.href")}`);
}

async function load(action) {
  const loaded = once("Page.loadEventFired");
  await action();
  await loaded;
  await settled();
}

// What the page shows: its look, address, tab, what is open over it, and whether it is the same page as before.
async function seen() {
  return evaluate(`({look: document.documentElement.dataset.look, address: location.pathname, tab: (TAB_ADDRESS[tab] || tab),
    task: $("sheet").open ? shownTask : null, project: $("setsheet").open ? setEdit?.name || null : null,
    run: $("modal").open ? shownRun : null, note: $("toast").hidden ? null : $("toast").textContent, same: window.kept === true,
    history: history.length})`);
}
const keep = () => evaluate("window.kept = true; $('toast').hidden = true;");
const step = async (expression) => { await evaluate(expression); await settled(); return seen(); };

async function open(look, address) {
  await send("Network.clearBrowserCookies");
  await send("Storage.clearDataForOrigin", {origin: base, storageTypes: "all"});
  await send("Network.setCookie", {name: "kandu-look", value: look, url: base});
  await load(() => send("Page.navigate", {url: base + address}));
  return seen();
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

  const walks = {}, addresses = new Set();
  for (const look of plan.looks) {
    const s = {};
    s.first = await open(look, "/");
    await keep();
    s.task = await step(`showTask(${JSON.stringify(plan.task)})`);
    s.run = await step(`showRun(${JSON.stringify(plan.run)})`);
    await load(() => send("Page.reload"));
    s.run_reloaded = await seen();
    await keep();
    s.run_back = await step("history.back()");
    s.task_back = await step("history.back()");
    s.task_forward = await step("history.forward()");
    s.task_closed = await step('$("sheet").close()');
    s.settings = await step('showTab("settings")');
    s.project = await step(`openSettings(${JSON.stringify(plan.project)})`);
    await load(() => send("Page.reload"));
    s.project_reloaded = await seen();
    await keep();
    s.project_back = await step("history.back()");
    s.settings_back = await step("history.back()");
    for (const each of Object.values(s)) addresses.add(each.address);
    walks[look] = s;
  }
  const pasted = {};
  for (const address of addresses) {
    pasted[address] = {};
    for (const look of plan.looks) pasted[address][look] = await open(look, address);
  }
  const lost = {};
  for (const address of plan.lost) lost[address] = await open(plan.looks[0], address);
  console.log(JSON.stringify({walks, pasted, lost, errors}));
  browser.kill();
  process.exit(0);
}

run().catch(error => fail(error.stack || String(error)));
