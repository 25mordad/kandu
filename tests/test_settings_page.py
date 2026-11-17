"""The Settings page (#46): the projects as a list, each opening its settings in a pop-up that changes only on Save,
and beside them the usage limits, the machines and Kandu's own switches and pace. The page's own functions run in node
on the state the API gives; the calls a Save makes go to the real API, as the page sends them."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from kandu import rename
from kandu.config import load_config
from kandu.dashboard import Api, dispatch
from kandu.state import StateStore
from tests import test_tasks_page as tasks
from tests import test_today as today
from tests.test_today import NODE, QUEUE, ROOT, Page, state

LINES = ("const SETTING_GROUPS = ", "const SETTING_LABELS = ", "const SETTING_HINTS = ", "const LIMIT_STEP = ")
FUNCTIONS = ("settingsProjects", "settingsProjectsHtml", "settingsDraft", "settingsCalls", "mergeOnAsked", "saveStep",
             "applySettings", "settingsBody", "settingsFoot", "settingControl", "stepper", "limitsHtml", "machinesHtml",
             "kanduHtml", "switchRow", "defaultLookHtml")
TOKEN = "secret-token"


def row(name, **fields):
    return {"name": name, "paused": False, "paused_in_config": False, "max_parallel_workers": 1,
            "split_approval": "user", "queen_answers_one_way": False, "priority": 1, "daily_token_limit": 2000000,
            "auto_merge": False, "frozen": False, "worker_cli": "claude"} | fields


ROWS = [row("website", max_parallel_workers=2, auto_merge=True), row("blog", paused=True),
        row("shop", split_approval="queen", frozen=True)]
MACHINES = [{"name": "hive-box", "always_on": True, "down_since": None},
            {"name": "studio", "always_on": False, "down_since": "2026-11-15T12:00:00+00:00"}]
BACKUP = {"enabled": True, "at": "03:00", "destination": "~/backups", "keep": 7, "copies": [], "last": None}


def settings_state(**fields):
    return state(project_settings=ROWS, machines=MACHINES, clean_merged=True, drop_blockers=False,
                 show_requests=False, backup=BACKUP, pace={"wake_every_minutes": 60, "max_cycle_minutes": 50}) | fields


@unittest.skipUnless(NODE, "needs node to run the page's script")
class SettingsPageTest(unittest.TestCase, Page):
    def run_js(self, script, lang="en"):
        """Runs the page's own functions in node, with its texts, in `lang`."""
        prelude = (f"const TEXT = {json.dumps(self.text)};\nlet lang = {json.dumps(lang)};\n"
                   "let confirming = false, sendingBack = false, flights = {open: [], flown: []};\n"
                   + "".join(self.line(start) for start in today.LINES + tasks.LINES + LINES)
                   + "".join(self.function(name) for name in dict.fromkeys(today.FUNCTIONS + tasks.FUNCTIONS + FUNCTIONS)))
        done = subprocess.run([NODE, "-"], input=prelude + f"\n(async () => {{ console.log(JSON.stringify(await (async () => {{ {script} }})())); }})();",
                              capture_output=True, text=True, timeout=30, env={**os.environ, "TZ": "UTC"})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def given(self, s=None):
        return f"const s = {json.dumps(s or settings_state())}; const queue = {json.dumps(QUEUE)};"

    # The list of projects.

    def test_projects_show_as_a_list_in_the_registrys_order_with_their_state_line_and_a_summary(self):
        found = self.run_js(self.given() + "return settingsProjects(s, queue);")
        self.assertEqual([(p["name"], p["line"], p["tone"]) for p in found],
                         [("website", "2 stuck", "red"), ("blog", "Paused", "muted"), ("shop", "1 waits for you", "amber")])
        self.assertEqual([p["summary"] for p in found],
                         ["Running · 2 bees at most · merges itself", "Paused · 1 bee at most · waits for you",
                          "Running · 1 bee at most · frozen"])

    def test_each_project_has_a_button_that_opens_its_settings(self):
        html = self.run_js(self.given() + "return settingsProjectsHtml(settingsProjects(s, queue));")
        for name in ("website", "blog", "shop"):
            self.assertIn(f'data-settings-open="{name}"', html)
        self.assertEqual(html.count("data-settings-open="), 3)
        self.assertIn("2 bees at most", html)

    def test_add_a_project_opens_todays_add_flow_from_the_list(self):
        section = self.page.split('<section data-panel="settings"')[1].split("</section>")[0]
        self.assertIn('id="addopen"', section)
        self.assertEqual(self.text["en"]["add_a_project"], "+ Add a project")
        self.assertIn(f'data-t="add_a_project"', section)
        self.assertIn('$("addopen").onclick = () => openAdd();', self.page)

    # The pop-up.

    def body(self, r, lang="en", edit="{}"):
        return self.run_js(f"const r = {json.dumps(r)}; const edit = {{was: settingsDraft(r), now: settingsDraft(r), refused: [], asking: false, ...{edit}}};"
                           "return settingsBody(r, edit);", lang)

    def test_the_pop_up_shows_every_setting_in_its_groups_work_merge_and_queen(self):
        html = self.body(ROWS[0])
        heads = [self.text["en"][k] for k in ("set_group_work", "set_group_merge", "set_group_queen")]
        self.assertEqual(heads, ["Work", "Merge", "The queen"])
        at = [html.index(f">{h}<") for h in heads]
        self.assertEqual(at, sorted(at))
        work, merge, queen = html[at[0]:at[1]], html[at[1]:at[2]], html[at[2]:]
        for setting in ("paused", "max_parallel_workers", "worker_cli", "priority", "daily_token_limit"):
            self.assertIn(f'data-draft="{setting}"', work, setting)
        for setting in ("auto_merge", "frozen"):
            self.assertIn(f'data-draft="{setting}"', merge, setting)
        for setting in ("split_approval", "queen_answers_one_way"):
            self.assertIn(f'data-draft="{setting}"', queen, setting)
        self.assertIn('value="2000000"', work)  # its daily tokens
        self.assertIn('<b class="number">2</b>', work)  # two workers at most

    # Run with: Claude Code or Codex (#81).

    def test_the_pop_up_shows_run_with_claude_code_or_codex_with_the_projects_choice(self):
        for r, pressed in ((ROWS[0], "claude"), (row("website", worker_cli="codex"), "codex")):
            field = self.body(r).split('data-draft="max_parallel_workers"')[-1].split('data-draft="priority"')[0]
            self.assertIn(">Run with<", field)
            self.assertIn(self.text["en"]["set_run_with_hint"], field)
            buttons = re.findall(r'data-draft="worker_cli" data-value="(\w+)" aria-pressed="(\w+)"[^>]*>([^<]*)<', field)
            self.assertEqual(buttons, [("claude", str(pressed == "claude").lower(), "Claude Code"),
                                       ("codex", str(pressed == "codex").lower(), "Codex")])
            self.assertNotIn("data-codex-why", field)  # nothing is said before the machine answers

    def test_run_with_reads_in_persian(self):
        field = self.body(ROWS[0], "fa").split('data-draft="max_parallel_workers"')[-1].split('data-draft="priority"')[0]
        self.assertEqual(self.text["fa"]["set_run_with"], "اجرا با")
        for key in ("set_run_with", "set_run_with_hint", "cli_claude", "cli_codex"):
            self.assertIn(self.text["fa"][key], field, key)

    def codex_popup(self, script, answers, lang="en"):
        """The website's pop-up, whose machine answers each ask with the next of `answers`: the API's answer."""
        return self.popup(f"""
const asked = [], answers = {json.dumps(answers)};
globalThis.get = async path => {{ asked.push(path); return answers.shift(); }};
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
{script}""", lang)

    def test_choosing_codex_on_a_machine_without_a_working_codex_shows_the_doctors_reason_beside_the_switch(self):
        result = self.codex_popup("""
press("worker_cli", "codex"); await settle();
const chosen = {asked: [...asked], body: els.setbody.innerHTML, calls: settingsCalls("website", setEdit.was, setEdit.now)};
press("worker_cli", "claude"); await settle();
return {chosen, back: els.setbody.innerHTML, sent: [...sent]};""", [{"machine": "server", "why": "not logged in; run: codex login"}])
        self.assertEqual(result["chosen"]["asked"], ["/projects/website/codex"])
        field = result["chosen"]["body"].split('data-draft="max_parallel_workers"')[-1].split('data-draft="priority"')[0]
        self.assertIn('data-draft="worker_cli" data-value="codex" aria-pressed="true"', field)
        self.assertIn("data-codex-why>Codex cannot run on server: not logged in; run: codex login</div>", field)
        self.assertEqual(result["chosen"]["calls"], [{"setting": "worker_cli", "path": "/projects/website/settings?worker_cli=codex"}])
        self.assertNotIn("data-codex-why", result["back"])  # Claude Code chosen again: nothing about Codex
        self.assertEqual(result["sent"], [])  # nothing is saved before Save

    def test_the_reason_reads_in_persian(self):
        for why, said in (("not installed", "نصب نیست"), ("not logged in; run: codex login", "واردش نشده‌ای؛ اجرا کن: codex login"),
                          ("codex login status did not answer", "codex login status جواب نداد"),
                          ("the machine does not answer", "ماشین جواب نمی‌دهد")):
            body = self.codex_popup('press("worker_cli", "codex"); await settle(); return els.setbody.innerHTML;',
                                    [{"machine": "server", "why": why}], "fa")
            self.assertIn(f"data-codex-why>Codex روی server اجرا نمی‌شود: {said}</div>", body, why)

    def test_a_machine_where_codex_works_says_nothing(self):
        body = self.codex_popup('press("worker_cli", "codex"); await settle(); return els.setbody.innerHTML;',
                                [{"machine": "server", "why": None}])
        self.assertNotIn("data-codex-why", body)

    def test_a_project_on_codex_asks_its_machine_when_its_pop_up_opens(self):
        result = self.run_js(self.line("let setEdit = ") + "".join(self.function(n) for n in ("openSettings", "checkCodex", "drawSettings")) + f"""
let lastState = {{project_settings: [{json.dumps(row("website", worker_cli="codex"))}]}};
const els = {{setbody: {{innerHTML: ""}}, setfoot: {{innerHTML: ""}}, setsheet: {{showModal() {{}}}}}};
const $ = id => els[id];
const asked = [];
globalThis.get = async path => {{ asked.push(path); return {{machine: "server", why: "not installed"}}; }};
openSettings("website"); await new Promise(resolve => setTimeout(resolve, 0));
return {{asked, body: els.setbody.innerHTML}};""")
        self.assertEqual(result["asked"], ["/projects/website/codex"])
        self.assertIn("Codex cannot run on server: not installed", result["body"])
        self.assertEqual(self.run_js(self.line("let setEdit = ") + "".join(self.function(n) for n in ("openSettings", "checkCodex", "drawSettings")) + f"""
let lastState = {{project_settings: [{json.dumps(ROWS[0])}]}};
const els = {{setbody: {{innerHTML: ""}}, setfoot: {{innerHTML: ""}}, setsheet: {{showModal() {{}}}}}};
const $ = id => els[id];
const asked = [];
globalThis.get = async path => {{ asked.push(path); return null; }};
openSettings("website"); return asked;"""), [])  # a project on Claude Code asks nothing of Codex

    def test_an_answer_for_a_pop_up_closed_or_set_back_to_claude_is_not_drawn(self):
        result = self.codex_popup("""
let release;
globalThis.get = path => new Promise(resolve => { release = () => resolve({machine: "server", why: "not installed"}); });
press("worker_cli", "codex"); press("worker_cli", "claude"); release(); await settle();
return els.setbody.innerHTML;""", [])
        self.assertNotIn("data-codex-why", result)

    def test_the_pop_up_draws_the_draft_not_the_saved_values(self):
        html = self.body(ROWS[0], edit='{now: {...settingsDraft(r), priority: 3, auto_merge: false}}')
        self.assertIn('<b class="number">3</b>', html)
        self.assertRegex(html, r'role="switch" aria-checked="false" data-draft="auto_merge"')

    def test_a_pause_set_in_the_config_shows_and_cannot_be_changed(self):
        html = self.body(row("blog", paused=True, paused_in_config=True))
        pause = html.split('data-draft="paused"')[0].rsplit("<div", 1)[1] + html.split('data-draft="paused"')[1].split("</div>")[0]
        self.assertIn("disabled", pause)
        self.assertIn(self.text["en"]["paused_in_config_hint"], html)
        self.assertNotIn("disabled", self.body(row("blog", paused=True)).split('data-draft="paused"')[1].split(">")[0])

    def test_removing_the_project_sits_at_the_bottom_apart_and_marked_as_dangerous(self):
        html = self.body(ROWS[0])
        danger = html.split('class="set-danger"')[1]
        self.assertIn('data-remove="website"', danger)
        self.assertIn('class="btn danger"', danger)
        self.assertGreater(html.index('class="set-danger"'), html.index('data-draft="queen_answers_one_way"'))
        self.assertEqual(html.count("data-remove="), 1)
        self.assertIn('if (hit("remove")) { removeProject(hit("remove")); return; }', self.page)

    def test_the_pop_up_reads_in_persian(self):
        html = self.body(ROWS[0], "fa")
        for key in ("set_group_work", "set_group_merge", "set_group_queen", "setting_auto_merge", "set_split_by"):
            self.assertIn(self.text["fa"][key], html, key)
        self.assertIn('<b class="number">۲</b>', html)
        self.assertIn('dir="ltr"', html.split('data-draft="daily_token_limit"')[0].rsplit("<input", 1)[1])

    def test_the_pop_up_rises_from_the_bottom_on_a_phone_and_is_centred_on_a_wide_screen(self):
        self.assertRegex(self.page, r'<dialog id="setsheet" class="sheet"')
        self.assertRegex(self.style, r"dialog\.sheet \{[^}]*width:min\(600px")
        phone = self.style.split("@media (max-width: 760px) {")[1].split("\n}\n")[0]
        self.assertRegex(phone, r"dialog\.sheet \{[^}]*margin:auto 0 0")

    # Save, Cancel and the calls.

    def calls(self, was, now):
        return self.run_js(f"return settingsCalls({json.dumps(was['name'])}, settingsDraft({json.dumps(was)}), {json.dumps(now)});")

    def draft(self, r):
        return {k: v for k, v in r.items() if k != "paused_in_config"}

    def test_nothing_changed_sends_nothing(self):
        self.assertEqual(self.calls(ROWS[0], self.draft(ROWS[0])), [])

    def test_each_change_goes_through_todays_call(self):
        now = self.draft(ROWS[0]) | {"paused": True, "max_parallel_workers": 3, "priority": 2,
                                     "daily_token_limit": 300000, "auto_merge": False, "frozen": True,
                                     "split_approval": "queen", "queen_answers_one_way": True}
        self.assertEqual(self.calls(ROWS[0], now), [
            {"setting": "paused", "path": "/projects/website/pause"},
            {"setting": "max_parallel_workers", "path": "/parallel?project=website%3A3"},
            {"setting": "priority", "path": "/projects/website/settings?priority=2"},
            {"setting": "daily_token_limit", "path": "/projects/website/settings?daily_token_limit=300000"},
            {"setting": "auto_merge", "path": "/projects/website/settings?auto_merge=false"},
            {"setting": "frozen", "path": "/projects/website/settings?frozen=true"},
            {"setting": "split_approval", "path": "/projects/website/settings?split_approval=queen"},
            {"setting": "queen_answers_one_way", "path": "/projects/website/settings?queen_answers_one_way=true"}])
        resume = self.calls(ROWS[1], self.draft(ROWS[1]) | {"paused": False})
        self.assertEqual(resume, [{"setting": "paused", "path": "/projects/blog/resume"}])

    def test_a_save_applies_each_change_and_a_refused_one_keeps_the_pop_up_open_and_says_which(self):
        result = self.run_js(f"""
const r = {json.dumps(ROWS[0])}, sent = [];
const edit = {{was: settingsDraft(r), now: {{...settingsDraft(r), priority: 4, daily_token_limit: 5}}, refused: [], asking: false}};
const send = async path => {{ sent.push(path); return path.includes("daily_token_limit") ? {{ok: false, why: "daily_token_limit is too low"}} : {{ok: true}}; }};
const refused = await applySettings("website", edit, send);
return {{sent, refused, was: edit.was, html: settingsBody(r, edit)}};""")
        self.assertEqual(result["sent"], ["/projects/website/settings?priority=4",
                                          "/projects/website/settings?daily_token_limit=5"])
        self.assertEqual(result["refused"], [{"setting": "daily_token_limit", "why": "daily_token_limit is too low"}])
        self.assertEqual(result["was"]["priority"], 4)  # kept, so it is not sent again
        self.assertEqual(result["was"]["daily_token_limit"], 2000000)
        alert = result["html"].split('role="alert">')[1].split("</div>")[0]
        self.assertEqual(" ".join(re.sub(r"<[^>]+>", " ", alert).split()), "Not saved: Daily tokens: daily_token_limit is too low")
        everything = self.run_js(f"""
const r = {json.dumps(ROWS[0])};
const edit = {{was: settingsDraft(r), now: {{...settingsDraft(r), priority: 4}}, refused: [], asking: false}};
return await applySettings("website", edit, async () => ({{ok: true}}));""")
        self.assertEqual(everything, [])

    def test_turning_merges_by_itself_on_asks_first_and_turning_it_off_asks_nothing(self):
        steps = self.run_js(f"""
const off = settingsDraft({json.dumps(ROWS[1])}), on = settingsDraft({json.dumps(ROWS[0])});
const edit = (was, now, asking = false) => ({{was, now, refused: [], asking}});
return [saveStep(edit(off, off)), saveStep(edit(off, {{...off, auto_merge: true}})),
        saveStep(edit(off, {{...off, auto_merge: true}}, true)), saveStep(edit(on, {{...on, auto_merge: false}})),
        saveStep(edit(off, {{...off, priority: 2}}))];""")
        self.assertEqual(steps, ["close", "ask", "save", "save", "save"])
        foot = self.run_js(f"""const r = {json.dumps(ROWS[1])};
return settingsFoot({{was: settingsDraft(r), now: {{...settingsDraft(r), auto_merge: true}}, refused: [], asking: true}});""")
        self.assertIn(self.text["en"]["set_merge_ask"], foot)
        self.assertIn("data-set-yes", foot)
        self.assertIn("data-set-no", foot)
        plain = self.run_js(f"const r = {json.dumps(ROWS[1])}; return settingsFoot({{was: settingsDraft(r), now: settingsDraft(r), refused: [], asking: false}});")
        self.assertIn("data-set-save", plain)
        self.assertIn("data-set-cancel", plain)
        self.assertNotIn("data-set-yes", plain)

    def test_declining_saves_nothing_and_cancel_or_closing_discards_the_draft(self):
        handler = self.page.split('document.addEventListener("click", e => {')[1].split("\n});\n")[0]
        self.assertIn('if (hit("set-no")) { setEdit.asking = false; drawSettings(); return; }', handler)
        self.assertIn('if (hit("set-cancel")) { $("setsheet").close(); return; }', handler)
        self.assertIn('$("setsheet").addEventListener("close", () => { setEdit = null; });', self.page)
        self.assertIn("now: settingsDraft(row)", self.function("openSettings"))

    # The pop-up at work: its own functions on stand-ins for the page, the server and the controls.

    def popup(self, script, lang="en"):
        """Opens the website's pop-up and runs `script` in it. `server(path)` answers each call: true, {error} for a
        refusal, or "lost" when the token is no longer taken. `press(key, value)` finds a drawn control of the draft
        and clicks it; `type(value)` types in the daily tokens."""
        page = "".join(self.function(name) for name in ("openSettings", "checkCodex", "drawSettings", "changeDraft", "saveSettings"))
        return self.run_js(self.line("let setEdit = ") + page + f"""
let token = {json.dumps(TOKEN)}, lastState = {{project_settings: {json.dumps(ROWS)}}}, server = () => true;
const sent = [], toasts = [], els = {{setbody: {{innerHTML: "", scrollTop: 0}}, setfoot: {{innerHTML: ""}}, setsheet: {{open: false}}}};
els.setsheet.showModal = () => {{ els.setsheet.open = true; }};
els.setsheet.close = () => {{ els.setsheet.open = false; setEdit = null; }};  // as the page's close listener does
const $ = id => els[id];
function toast(text) {{ toasts.push(text); }}
function renderSettings() {{}}
async function askToken() {{ return false; }}
async function post() {{ return false; }}
async function act(button, path) {{
  if (!token) return false;
  sent.push(path);
  const answer = server(path);
  if (answer === true) {{ post.failed = null; return {{}}; }}
  if (answer === "lost") {{ token = ""; post.failed = {{error: "a valid token is required"}}; return false; }}
  post.failed = answer;
  return false;
}}
const controls = () => [...(els.setbody.innerHTML + els.setfoot.innerHTML).matchAll(/<(?:button|input)\\b[^>]*>/g)].map(([tag]) => {{
  const attrs = Object.fromEntries([...tag.matchAll(/([\\w-]+)="([^"]*)"/g)].map(([, k, v]) => [k, v]));
  const dataset = Object.fromEntries(Object.entries(attrs).filter(([k]) => k.startsWith("data-"))
    .map(([k, v]) => [k.slice(5).replace(/-(\\w)/g, (_, c) => c.toUpperCase()), v]));
  return {{dataset, value: attrs.value, disabled: /\\sdisabled[\\s>]/.test(tag), getAttribute: k => attrs[k] ?? null}};
}});
const press = (key, value) => {{
  const hit = controls().find(c => c.dataset.draft === key && (value === undefined || c.dataset.value === value || c.dataset.by === value));
  if (!hit || hit.disabled) throw new Error(`no ${{key}} ${{value}} to press`);
  changeDraft(hit);
}};
const type = value => {{ const field = controls().find(c => c.dataset.draft === "daily_token_limit"); field.value = value; changeDraft(field); }};
const typeName = value => {{ const field = controls().find(c => c.dataset.draft === "name"); field.value = value; changeDraft(field); }};
const sheet = () => ({{open: els.setsheet.open, editing: setEdit !== null, sent: [...sent], toasts: [...toasts], body: els.setbody.innerHTML, foot: els.setfoot.innerHTML}});
openSettings("website");
{script}""", lang)

    def test_the_minus_and_plus_change_the_draft_one_at_a_time_and_never_below_one(self):
        result = self.popup("""
press("priority", "1"); press("priority", "1"); const up = setEdit.now.priority;
press("max_parallel_workers", "-1"); const down = setEdit.now.max_parallel_workers;
let stuck = false; try { press("max_parallel_workers", "-1"); } catch { stuck = true; }
changeDraft({dataset: {draft: "max_parallel_workers", by: "-1"}, getAttribute: () => null});
return {up, down, stuck, after: setEdit.now.max_parallel_workers, sent, body: els.setbody.innerHTML};""")
        self.assertEqual(result["up"], 3)  # the website's priority was 1
        self.assertEqual(result["down"], 1)  # it had two workers at most
        self.assertTrue(result["stuck"])  # the minus is off at one
        self.assertEqual(result["after"], 1)
        self.assertEqual(result["sent"], [])  # nothing is sent before Save
        self.assertIn('<b class="number">3</b>', result["body"])

    def test_the_choices_set_true_and_false_as_yes_and_no_and_the_queen_by_name(self):
        result = self.popup("""
press("paused", "true"); press("queen_answers_one_way", "true"); press("split_approval", "queen");
const changed = {...setEdit.now};
press("paused", "false"); press("queen_answers_one_way", "false"); press("split_approval", "user");
return {changed, back: settingsCalls("website", setEdit.was, setEdit.now)};""")
        self.assertIs(result["changed"]["paused"], True)
        self.assertIs(result["changed"]["queen_answers_one_way"], True)
        self.assertEqual(result["changed"]["split_approval"], "queen")
        self.assertEqual(result["back"], [])  # set back as they were, Save has nothing to send

    def test_a_switch_flips_and_flips_back(self):
        result = self.popup("""
press("frozen"); const once = setEdit.now.frozen, drawn = els.setbody.innerHTML;
press("frozen"); return {once, twice: setEdit.now.frozen, drawn};""")
        self.assertIs(result["once"], True)
        self.assertIs(result["twice"], False)
        self.assertRegex(result["drawn"], r'role="switch" aria-checked="true" data-draft="frozen"')

    def test_typed_daily_tokens_become_a_number_and_an_empty_field_stays_empty(self):
        result = self.popup("""
const body = els.setbody.innerHTML;
type(" 300000 "); const typed = setEdit.now.daily_token_limit, calls = settingsCalls("website", setEdit.was, setEdit.now);
type(""); return {typed, calls, empty: setEdit.now.daily_token_limit, same: els.setbody.innerHTML === body};""")
        self.assertEqual(result["typed"], 300000)
        self.assertEqual(result["calls"], [{"setting": "daily_token_limit", "path": "/projects/website/settings?daily_token_limit=300000"}])
        self.assertEqual(result["empty"], "")
        self.assertTrue(result["same"])  # the field is not drawn again under the cursor

    def test_a_change_after_the_merge_question_takes_the_question_away(self):
        result = self.popup("""
lastState.project_settings[0].auto_merge = false; openSettings("website");
press("auto_merge"); await saveSettings({}); const asked = sheet();
type("5"); return {asked, after: sheet()};""")
        self.assertIn("data-set-yes", result["asked"]["foot"])
        self.assertEqual(result["asked"]["sent"], [])
        self.assertTrue(result["asked"]["open"])
        self.assertIn("data-set-save", result["after"]["foot"])

    def test_save_with_nothing_changed_closes_and_sends_nothing(self):
        result = self.popup("await saveSettings({}); return sheet();")
        self.assertEqual(result["sent"], [])
        self.assertFalse(result["open"])
        self.assertEqual(result["toasts"], [])

    def test_save_sends_each_change_closes_and_says_it_is_saved(self):
        result = self.popup("""
press("priority", "1"); press("frozen"); type("300000");
await saveSettings({}); return sheet();""")
        self.assertEqual(result["sent"], ["/projects/website/settings?priority=2",
                                          "/projects/website/settings?daily_token_limit=300000",
                                          "/projects/website/settings?frozen=true"])
        self.assertFalse(result["open"])
        self.assertFalse(result["editing"])
        self.assertEqual(result["toasts"], ["website: saved"])

    def test_a_refused_change_keeps_the_pop_up_open_with_the_servers_reason_and_the_rest_saved(self):
        result = self.popup("""
server = path => path.includes("daily_token_limit") ? {error: "daily_token_limit must be at least 100000"} : true;
press("priority", "1"); type("5"); press("frozen");
await saveSettings({}); const first = {...sheet(), was: {...setEdit.was}};
sent.length = 0; server = () => true; await saveSettings({});
return {first, again: sheet()};""")
        first = result["first"]
        self.assertEqual(len(first["sent"]), 3)  # a refusal does not stop the others
        self.assertTrue(first["open"])
        self.assertEqual(first["toasts"], [])
        alert = first["body"].split('role="alert">')[1].split("</div>")[0]
        self.assertEqual(" ".join(re.sub(r"<[^>]+>", " ", alert).split()),
                         "Not saved: Daily tokens: daily_token_limit must be at least 100000")
        self.assertEqual((first["was"]["priority"], first["was"]["frozen"]), (2, True))
        self.assertEqual(result["again"]["sent"], ["/projects/website/settings?daily_token_limit=5"])  # only what was refused
        self.assertFalse(result["again"]["open"])
        self.assertEqual(result["again"]["toasts"], ["website: saved"])

    def test_a_refusal_without_a_reason_says_the_request_failed(self):
        result = self.popup('server = () => ({}); press("frozen"); await saveSettings({}); return sheet();', "fa")
        alert = result["body"].split('role="alert">')[1]
        self.assertIn(self.text["fa"]["request_failed"], alert)
        self.assertTrue(result["open"])

    def test_a_lost_token_stops_the_save_at_that_change(self):
        result = self.popup("""
server = () => "lost"; press("priority", "1"); press("frozen");
await saveSettings({}); return sheet();""")
        self.assertEqual(result["sent"], ["/projects/website/settings?priority=2"])
        self.assertTrue(result["open"])
        alert = result["body"].split('role="alert">')[1].split("</div>")[0]
        self.assertEqual(" ".join(re.sub(r"<[^>]+>", " ", alert).split()), "Not saved: Priority: a valid token is required")

    def test_without_a_token_and_none_given_nothing_is_sent(self):
        result = self.popup('token = ""; press("frozen"); await saveSettings({}); return sheet();')
        self.assertEqual(result["sent"], [])
        self.assertTrue(result["open"])
        self.assertNotIn('role="alert"', result["body"])

    def test_a_pop_up_closed_while_saving_is_not_drawn_again(self):
        result = self.popup("""
server = () => { els.setsheet.close(); return {error: "no"}; }; press("frozen");
const body = els.setbody.innerHTML; await saveSettings({});
return {...sheet(), same: els.setbody.innerHTML === body};""")
        self.assertFalse(result["open"])
        self.assertTrue(result["same"])
        self.assertEqual(result["toasts"], [])

    # The project's name.

    def test_the_pop_up_shows_the_projects_name_in_an_editable_field_above_its_settings(self):
        html = self.body(ROWS[0])
        field = re.search(r'<input type="text"[^>]*data-draft="name"[^>]*>', html)
        self.assertIsNotNone(field)
        self.assertIn('value="website"', field.group(0))
        self.assertIn('dir="ltr"', field.group(0))
        self.assertIn(f'aria-label="{self.text["en"]["set_name"]}"', field.group(0))
        self.assertLess(html.index('data-draft="name"'), html.index(f'>{self.text["en"]["set_group_work"]}<'))
        self.assertIn(self.text["en"]["set_name_hint"], html)

    def test_the_name_field_draws_the_draft(self):
        html = self.body(ROWS[0], edit='{now: {...settingsDraft(r), name: "site"}}')
        self.assertRegex(html, r'data-draft="name" value="site"')

    def test_a_changed_name_is_sent_last_through_the_settings_call(self):
        now = self.draft(ROWS[0]) | {"name": "my-site", "priority": 2, "frozen": True}
        self.assertEqual(self.calls(ROWS[0], now), [
            {"setting": "priority", "path": "/projects/website/settings?priority=2"},
            {"setting": "frozen", "path": "/projects/website/settings?frozen=true"},
            {"setting": "name", "path": "/projects/website/settings?name=my-site"}])

    def test_typing_a_name_changes_only_the_draft_and_save_renames_with_the_other_settings(self):
        result = self.popup("""
const body = els.setbody.innerHTML;
typeName(" site "); const typed = setEdit.now.name, same = els.setbody.innerHTML === body, before = [...sent];
press("frozen"); await saveSettings({}); return {typed, same, before, ...sheet()};""")
        self.assertEqual(result["typed"], "site")
        self.assertTrue(result["same"])  # the field is not drawn again under the cursor
        self.assertEqual(result["before"], [])  # nothing is sent before Save
        self.assertEqual(result["sent"], ["/projects/website/settings?frozen=true", "/projects/website/settings?name=site"])
        self.assertFalse(result["open"])
        self.assertEqual(result["toasts"], ["site: saved"])

    def test_a_name_set_back_as_it_was_sends_nothing(self):
        result = self.popup('typeName("site"); typeName("website"); await saveSettings({}); return sheet();')
        self.assertEqual(result["sent"], [])
        self.assertFalse(result["open"])

    def test_an_empty_name_is_refused_in_the_pop_up_and_sends_nothing(self):
        result = self.popup('typeName("  "); await saveSettings({}); return sheet();')
        self.assertEqual(result["sent"], [])
        self.assertTrue(result["open"])
        alert = result["body"].split('role="alert">')[1].split("</div>")[0]
        self.assertEqual(" ".join(re.sub(r"<[^>]+>", " ", alert).split()), "Not saved: Name: give the project a name")

    def test_a_refused_name_keeps_the_pop_up_open_with_the_servers_reason_in_both_languages(self):
        script = """
server = path => path.includes("name=") ? {error: "stop or wait for the running work first"} : true;
typeName("site"); press("frozen"); await saveSettings({}); return {...sheet(), name: setEdit.name, was: setEdit.was};"""
        for lang, words in (("en", "Name: stop or wait for the running work first"),
                            ("fa", f'{self.text["fa"]["set_name"]}: {self.text["values_fa"]["stop or wait for the running work first"]}')):
            result = self.popup(script, lang)
            self.assertTrue(result["open"])
            self.assertEqual(result["name"], "website")  # it keeps its name
            self.assertIs(result["was"]["frozen"], True)  # the other setting was saved
            alert = result["body"].split('role="alert">')[1].split("</div>")[0]
            self.assertIn(words, " ".join(re.sub(r"<[^>]+>", " ", alert).split()))

    def test_after_a_rename_the_pop_up_goes_on_under_the_new_name(self):
        result = self.popup("""
server = path => path.includes("priority") ? {error: "no"} : true;
typeName("site"); press("priority", "1"); await saveSettings({}); const first = {...sheet(), name: setEdit.name};
sent.length = 0; server = () => true; await saveSettings({}); return {first, again: sheet()};""")
        self.assertEqual(result["first"]["name"], "site")
        self.assertTrue(result["first"]["open"])
        self.assertEqual(result["again"]["sent"], ["/projects/site/settings?priority=2"])
        self.assertEqual(result["again"]["toasts"], ["site: saved"])

    def test_the_name_field_reads_in_persian_and_fits_a_phone(self):
        html = self.body(ROWS[0], "fa")
        for key in ("set_name", "set_name_hint"):
            self.assertIn(self.text["fa"][key], html, key)
        self.assertRegex(html, r'<input type="text" class="name-field"[^>]*dir="ltr"')
        self.assertRegex(self.style, r"\.name-field \{[^}]*width:100%")
        for message in ("give the project a name", "another project already has this name",
                        "stop or wait for the running work first", rename.NOT_A_NAME, rename.KEPT,
                        "change the name in a call of its own", "project_renamed"):
            self.assertIn(message, self.text["values_fa"], message)

    # Beside the projects.

    def test_the_usage_limits_hold_new_work_with_a_minus_and_a_plus(self):
        html = self.run_js(self.given() + "return limitsHtml(s);")
        self.assertIn('data-step-path="/limits?session={n}&amp;week=90" data-step-from="80"', html)
        self.assertIn('data-step-path="/limits?session=80&amp;week={n}" data-step-from="90"', html)
        self.assertIn('data-step="5"', html)
        self.assertIn('data-step-most="100"', html)
        full = self.run_js(self.given(settings_state(limits={"session": 100, "week": 50})) + "return limitsHtml(s);")
        self.assertRegex(full, r'data-step="5" data-step-path="/limits\?session=\{n\}[^>]*disabled')
        self.assertIn(self.text["en"]["limit_session"], html)

    def test_a_stepper_stops_at_its_highest_value(self):
        self.assertIn("Math.min(Number(button.dataset.stepMost || Infinity)", self.function("changeStep"))

    def test_the_machines_show_their_state_and_add_opens_todays_flow(self):
        html = self.run_js(self.given() + "return machinesHtml(s);")
        self.assertIn("hive-box", html)
        self.assertIn(self.text["en"]["machine_answers"], html)
        self.assertIn("studio", html)
        self.assertIn("Not answering since", html)
        self.assertIn("data-add-machine", html)
        self.assertIn('if (hit("add-machine")) { openAdd(""); return; }', self.page)
        self.assertIn("machineOptions(machine)", self.function("openAdd"))

    def test_kandus_own_switches_and_pace_work_as_today(self):
        html = self.run_js(self.given() + "return kanduHtml(s);")
        self.assertIn('role="switch" aria-checked="true" data-switch="/clean-merged?enabled=false"', html)
        self.assertIn('role="switch" aria-checked="false" data-switch="/drop-blockers?enabled=true"', html)
        self.assertIn('role="switch" aria-checked="false" data-switch="/requests?enabled=true"', html)
        self.assertIn("data-backup", html)
        self.assertIn("Daily at 03:00", html)
        for path in ("/parallel?total={n}", "/pace?wake_every_minutes={n}", "/pace?max_cycle_minutes={n}"):
            self.assertIn(f'data-step-path="{path}"', html)
        self.assertIn('if (hit("switch")) { flipSwitch(hit("switch")); return; }', self.page)

    def test_the_default_look_shows_and_each_choice_saves_it_with_the_token(self):
        html = self.run_js(self.given(settings_state(default_look="classic")) + "return kanduHtml(s);")
        self.assertIn(self.text["en"]["default_look"], html)
        self.assertIn(f'<button type="button" data-switch="/default-look?look=classic" aria-pressed="true">'
                      f'{self.text["en"]["default_look_classic"]}</button>', html)
        self.assertIn(f'<button type="button" data-switch="/default-look?look=clean" aria-pressed="false">'
                      f'{self.text["en"]["default_look_clean"]}</button>', html)
        clean = self.run_js(self.given(settings_state(default_look="clean")) + "return kanduHtml(s);")
        self.assertIn('data-switch="/default-look?look=clean" aria-pressed="true"', clean)
        self.assertIn('data-switch="/default-look?look=classic" aria-pressed="false"', clean)
        self.assertIn("act(button, button.dataset.switch)", self.function("flipSwitch"))  # act sends the token
        self.assertIn("s.default_look", self.function("renderSettings"))
        self.assertEqual((self.text["en"]["default_look_classic"], self.text["en"]["default_look_clean"]),
                         ("Classic", "Kandu Clean"))
        fa = self.run_js(self.given(settings_state(default_look="clean")) + "return kanduHtml(s);", "fa")
        for key in ("default_look", "default_look_hint", "default_look_classic", "default_look_clean"):
            self.assertIn(key, self.text["fa"], key)
            self.assertNotEqual(self.text["fa"][key], self.text["en"][key], key)
        self.assertIn(self.text["fa"]["default_look"], fa)

    def test_the_side_reads_in_persian(self):
        html = self.run_js(self.given() + "return limitsHtml(s) + machinesHtml(s) + kanduHtml(s);", "fa")
        for key in ("limits_title", "machines_title", "kandu_itself", "clean_merged", "pace_wake"):
            self.assertIn(self.text["fa"][key], html, key)
        self.assertIn("۸۰٪", html)

    def test_the_page_stands_the_list_beside_the_side_and_stacks_them_on_a_phone(self):
        section = self.page.split('<section data-panel="settings"')[1].split("</section>")[0]
        for part in ('id="projectsettings"', 'id="limitsettings"', 'id="machinesettings"', 'id="kandusettings"'):
            self.assertIn(part, section)
        self.assertRegex(self.style, r"\.settings \{[^}]*flex-wrap:wrap")
        self.assertRegex(self.style, r"\.settings-main \{[^}]*min-width:0")
        self.assertRegex(self.style, r"\.settings-side \{[^}]*min-width:0")
        self.assertRegex(self.style, r'body\[data-page="settings"\] #stats, body\[data-page="settings"\] #asks \{ display:none; \}')

    def test_every_new_word_is_there_in_both_languages(self):
        for key in ("set_group_work", "set_group_merge", "set_group_queen", "set_running", "set_bees", "set_bees_one",
                    "set_run_label", "set_split_by", "set_one_way_by", "set_me", "set_the_queen", "set_merge_ask",
                    "set_merge_yes", "set_merge_no", "set_refused", "set_saved", "set_open", "set_danger",
                    "projects_title", "add_a_project", "limit_session", "limit_week", "machines_title", "machine_answers",
                    "machine_silent", "add_machine_short", "kandu_itself", "backup", "switch_drop_blockers",
                    "switch_requests", "set_name", "set_name_hint", "set_run_with", "set_run_with_hint", "cli_claude",
                    "cli_codex", "set_codex_why"):
            self.assertIn(key, self.text["en"], key)
            self.assertIn(key, self.text["fa"], key)


class SaveThroughTheApiTest(unittest.TestCase):
    """The calls a Save makes are today's calls: the API takes each, and logs it as today."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        local = root / "local"
        local.mkdir()
        shutil.copy(ROOT / "config.example" / "kandu.example.yaml", local / "kandu.yaml")
        shutil.copy(ROOT / "config.example" / "projects.example.yaml", local / "projects.yaml")
        self.store = StateStore(root / "state")
        self.api = Api(load_config(local), self.store, None, root / "logs", TOKEN, timer_on=lambda: True, local_dir=local)

    def row(self, name="website"):
        return next(r for r in dispatch(self.api, "GET", "/api/v1/state")[1]["project_settings"] if r["name"] == name)

    @unittest.skipUnless(NODE, "needs node to run the page's script")
    def test_every_call_a_save_makes_is_taken_and_logged(self):
        was = self.row()
        now = {k: v for k, v in was.items() if k != "paused_in_config"} | {
            "paused": True, "max_parallel_workers": was["max_parallel_workers"] + 1, "priority": was["priority"] + 1,
            "daily_token_limit": 300000, "auto_merge": not was["auto_merge"], "frozen": not was["frozen"],
            "split_approval": "queen", "queen_answers_one_way": not was["queen_answers_one_way"], "worker_cli": "codex"}
        page = Page()
        script = page.function("settingsDraft") + page.function("settingsCalls") + page.line("const SETTING_GROUPS = ")
        done = subprocess.run([NODE, "-"], input=script + f"console.log(JSON.stringify(settingsCalls('website', settingsDraft({json.dumps(was)}), {json.dumps(now)})));",
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        for call in json.loads(done.stdout):
            self.assertEqual(dispatch(self.api, "POST", "/api/v1" + call["path"], TOKEN)[0], 200, call)
        after = self.row()
        self.assertEqual({k: after[k] for k in now}, now)
        changed = {e["setting"]: (e["old"], e["new"]) for e in self.store.events(self.store.today())
                   if e["type"] == "project_setting_changed"}
        self.assertEqual(set(changed), {"priority", "daily_token_limit", "auto_merge", "frozen", "split_approval",
                                        "queen_answers_one_way", "worker_cli"})
        self.assertEqual(changed["worker_cli"], ("claude", "codex"))
        self.assertEqual(changed["priority"], (was["priority"], was["priority"] + 1))
        self.assertEqual(self.store.last_event("parallel_changed")["projects"], {"website": was["max_parallel_workers"] + 1})
        self.assertEqual(self.store.last_event("project_paused")["project"], "website")


if __name__ == "__main__":
    unittest.main()
