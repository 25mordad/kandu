// Opens the Classic look in a real headless browser, as a user would, and clicks a task where it shows: a flower on
// the meadow and a row on the Work page. Each click should open the Kandu Clean look's task sheet over the same page,
// with what the Classic look showed besides. Prints what each step shows as JSON.
// Usage: node drive_classic_sheet.js <browser> <dashboard url> <empty profile folder> <shots folder or ""> <cases as JSON>
// A case is {lang, width, height, mobile}. It talks to the browser over the DevTools protocol, with no library.
const {spawn} = require("child_process");
const [chromiumFile, base, profile, shots, casesJson] = process.argv.slice(2);
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

// A click as a user's, which bubbles; a flower on the meadow is SVG and has no click() of its own.
const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true}))`);

async function waitFor(expression) {
  for (let i = 0; i < 50; i++) {
    if (await evaluate(expression)) return true;
    await sleep(100);
  }
  return false;
}

// What the sheet shows once a task was clicked, and what is under it.
const seen = () => evaluate(`(() => {
  const sheet = document.getElementById("sheet"), html = document.documentElement, body = document.getElementById("sheetbody");
  const all = selector => [...sheet.querySelectorAll(selector)];
  return {open: sheet.open, task: shownTask, title: sheet.querySelector(".sheet-who h2")?.textContent || null, tab,
          modal: document.getElementById("modal").open, phases: all(".phase").length, steps: Boolean(sheet.querySelector(".steps")),
          links: all(".sheet-state a.textlink").map(a => a.href), chips: all(".sheet-state .chip, .sheet-state .muted, .sheet-state .badge").map(el => el.textContent),
          run: Boolean(sheet.querySelector(".sheet-state [data-run]")), flight: Boolean(sheet.querySelector("#sheetfoot #taskflight [data-fly]")),
          status: sheet.querySelector(".status-line")?.textContent || null,
          sideways: html.scrollWidth > html.clientWidth || body.scrollWidth > body.clientWidth};
})()`);

async function shot(name) {
  if (!shots) return;
  const png = await send("Page.captureScreenshot", {format: "png"});
  require("fs").writeFileSync(`${shots}/${name}.png`, Buffer.from(png.data, "base64"));
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
    const name = `${c.lang}-${c.width}`;
    await send("Network.clearBrowserCookies");
    await send("Network.setCookie", {name: "kandu-look", value: "classic", url: base});
    await send("Emulation.setDeviceMetricsOverride", {width: c.width, height: c.height, deviceScaleFactor: 1, mobile: c.mobile});
    await load(() => send("Page.navigate", {url: base + "/"}));
    await evaluate(`localStorage.setItem("kandu-language", ${JSON.stringify(c.lang)}); localStorage.setItem("kandu-tab", "meadow");`);
    await load(() => send("Page.navigate", {url: base + "/"}));  // "/" opens the saved tab; a view's own address would win
    const steps = {look: await evaluate("document.documentElement.dataset.look")};

    // On the meadow: the flower of a stuck task with a PR.
    await click(`g[data-review="${c.stuck}"]`);
    await waitFor('document.getElementById("sheet").open');
    steps.meadow = await seen();
    await shot(`${name}-stuck`);
    await click("[data-close-sheet]");
    steps.closed = await waitFor('!document.getElementById("sheet").open && shownTask === null');

    // The task a bee works on: its run log opens over the sheet, which stays the task's.
    await click(`g[data-review="${c.working}"]`);
    await waitFor('document.getElementById("sheet").open');
    steps.working = await seen();
    await click("#sheet .sheet-state [data-run]");
    await waitFor('document.getElementById("modal").open');
    steps.run = await seen();
    await evaluate('document.getElementById("modal").close()');
    await sleep(100);
    steps.after_run = await seen();
    await evaluate('document.getElementById("sheet").close()');
    await waitFor("shownTask === null");

    // On the Work page: a task's row.
    await evaluate('showTab("work")');
    await click(`tr[data-review="${c.row}"] .textlink`);
    await waitFor('document.getElementById("sheet").open');
    steps.work = await seen();
    await shot(`${name}-work`);
    await evaluate('document.getElementById("sheet").close()');
    await waitFor("shownTask === null");
    results.push({...c, steps});
  }
  console.log(JSON.stringify({results, errors}));
  browser.kill();
  process.exit(0);
}

run().catch(error => fail(error.stack || String(error)));
