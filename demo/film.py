"""Records the demo film: the real dashboard on the demo world, driven scene by scene, timed to the narration.

    python3 -m demo.film --lang en

runs with the system Python, which has Playwright, Chrome, Xvfb and ffmpeg; demo/voice.py must have made the
clips first. The screen is a virtual one, so nothing of the machine it runs on is filmed. The result is
demo/out/kandu-<lang>.mp4, with captions burnt in and a .srt beside it.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.sync_api import sync_playwright  # noqa: E402

from demo.serve import TOKEN, start  # noqa: E402
from demo.world import NOTES, TEA  # noqa: E402

HERE = Path(__file__).parent
OUT = HERE / "out"
SIZE = (1920, 1080)
SCALE = 1.25
DISPLAY = ":99"
LEAD, TAIL = 0.6, 0.9  # silence before and after each scene's voice

TITLE = {"en": ("Kandu", "Kandu · open source\ngithub.com/25mordad/kandu"),
         "fa": ("کندو", "کندو · متن‌باز\ngithub.com/25mordad/kandu")}

# Captions and titles sit on top of every page the film opens, the dashboard and the terminal alike.
OVERLAY = """
window.__film = (() => {
  const css = `#film-cap { position:fixed; left:50%; bottom:34px; transform:translateX(-50%); max-width:78%; z-index:99999;
      padding:12px 26px; border-radius:12px; background:#000000c4; color:#fff; font:600 27px/1.5 Vazirmatn, system-ui, sans-serif;
      text-align:center; pointer-events:none; transition:opacity .25s; }
    #film-title { position:fixed; inset:0; display:flex; align-items:center; justify-content:center; z-index:99998; pointer-events:none;
      font:800 120px Vazirmatn, system-ui, sans-serif; color:#2a1d00; text-shadow:0 4px 30px #fff; transition:opacity .8s; }
    #film-title.small { font-size:46px; max-width:80%; text-align:center; line-height:1.5; white-space:pre-line; }`;
  const make = () => {
    if (document.getElementById("film-cap")) return;
    const style = document.createElement("style"); style.textContent = css; document.head.appendChild(style);
    for (const id of ["film-cap", "film-title"]) { const el = document.createElement("div"); el.id = id; el.style.opacity = 0; document.body.appendChild(el); }
  };
  return {
    captions(lines, dir) {  // [text, from ms, to ms] from now
      make();
      const cap = document.getElementById("film-cap");
      cap.dir = dir;
      for (const [text, from, to] of lines) {
        setTimeout(() => { cap.textContent = text; cap.style.opacity = 1; }, from);
        setTimeout(() => { if (cap.textContent === text) cap.style.opacity = 0; }, to);
      }
    },
    title(text, small, ms) {
      make();
      const el = document.getElementById("film-title");
      el.textContent = text; el.className = small ? "small" : ""; el.style.opacity = 1;
      if (ms) setTimeout(() => { el.style.opacity = 0; }, ms);
    },
  };
})();
"""


def sentences(text, lang):
    parts = re.split(r"(?<=[.?!؟])\s+", text.strip())
    return [p for p in parts if p]


class Film:
    def __init__(self, lang):
        self.lang = lang
        self.texts = [p.strip() for p in (HERE / f"narration.{lang}.txt").read_text(encoding="utf-8").split("\n\n")
                      if p.strip()]
        self.clips = json.loads((OUT / f"voice-{lang}.json").read_text())
        self.starts = []  # each scene's start on the recording's clock
        self.subtitles = []

    def now(self):
        return time.monotonic() - self.t0

    def scene(self, n):
        """Starts scene n: its captions run in the page, timed as its voice will be laid in."""
        self.scene_start = self.now()
        self.starts.append(self.scene_start)
        seconds, parts = self.clips[n - 1]["seconds"], sentences(self.texts[n - 1], self.lang)
        total, at, lines = sum(len(p) for p in parts), LEAD, []
        for part in parts:
            span = seconds * len(part) / total
            lines.append([part, int(at * 1000), int((at + span) * 1000) - 80])
            self.subtitles.append((self.scene_start + at, self.scene_start + at + span, part))
            at += span
        self.page.evaluate("([lines, dir]) => __film.captions(lines, dir)", [lines, "rtl" if self.lang == "fa" else "ltr"])
        self.scene_end = self.scene_start + LEAD + seconds + TAIL

    def at(self, offset):
        """Waits until `offset` seconds into the scene, keeping the page current as the cycle buttons would."""
        while self.now() < self.scene_start + offset:
            try:
                self.page.evaluate("refresh()")
            except Exception:
                pass
            time.sleep(min(0.7, max(0, self.scene_start + offset - self.now())))

    def end(self):
        self.at(self.scene_end - self.scene_start)

    def js(self, code, arg=None):
        return self.page.evaluate(code, arg)

    def dashboard(self):
        self.page.goto(self.url)
        self.page.wait_for_load_state("load")
        time.sleep(1.5)
        self.js(f"token = {json.dumps(TOKEN)}")
        self.js("refresh()")

    def play(self, page, world, url):
        self.page, self.world, self.url = page, world, url
        task21, task7 = f"{NOTES}#21", f"{TEA}#7"
        self.dashboard()
        time.sleep(2)

        # 1. The problem: the quiet meadow.
        self.scene(1)
        self.js("([t]) => __film.title(t, false, 3500)", [TITLE[self.lang][0]])
        self.end()

        # 2. Say what you want: Didban, played from a script on a page of its own.
        seconds = self.clips[1]["seconds"] + LEAD + TAIL
        self.page.goto(f"{url}didban-demo?lang={self.lang}&ms={int((seconds - 1.5) * 1000)}")
        self.scene(2)
        self.at(seconds - 1.2)
        world.new_task()
        self.end()

        # 3. The hive wakes: a new flower, the splitter, the queen, two tasks released.
        self.dashboard()
        self.scene(3)
        world.wakes.append(world.wake)
        self.at(2.5)
        self.page.click("#wake", force=True)
        self.at(7)
        world.size()
        self.at(11)
        world.release()
        self.at(12)
        self.js("document.getElementById('queenbox')?.scrollIntoView({behavior: 'smooth', block: 'center'})")
        self.at(16)
        self.js("window.scrollTo({top: 0, behavior: 'smooth'})")
        self.end()

        # 4. Workers build: two bees, and one of them watched live.
        self.scene(4)
        world.build()
        self.at(2.5)
        self.js("id => showRun(id)", world.worker_21)
        while self.now() < self.scene_end - 1.2:
            self.js("id => showRun(id)", world.worker_21)
            time.sleep(0.8)
        self.js("document.getElementById('modal').close()")
        self.end()

        # 5. A fresh review: sent back with one blocker, fixed, reviewed again.
        self.scene(5)
        world.review()
        self.at(3.5)
        world.blocker()
        self.at(5)
        self.js("task => { confirming = false; showReviews(task); }", task21)
        self.at(11.5)
        self.js("document.getElementById('modal').close()")
        self.at(12)
        world.second_review()
        self.at(15.5)
        world.approve_21()
        self.end()

        # 6. Done means live: merge, deploy, the scout, then a cell in the comb.
        self.scene(6)
        world.ship_21()
        self.at(4)
        world.done_21()
        self.at(6.5)
        self.js("document.getElementById('comb').scrollIntoView({behavior: 'smooth', block: 'center'})")
        self.at(7.5)
        self.page.click(f'.cell[data-review="{task21}"]', force=True)
        self.at(self.scene_end - self.scene_start - 1.0)
        self.js("document.getElementById('modal').close(); window.scrollTo({top: 0, behavior: 'smooth'})")
        self.end()

        # 7. When it needs you: the one-way door in tea-shop, and the user's yes.
        self.scene(7)

        def ship_7():
            world.ship_7()
            time.sleep(3)
            world.deploy_7()
            time.sleep(4)
            world.done_7()
        world.wakes.append(ship_7)
        self.at(1.5)
        self.js("task => { confirming = false; showReviews(task); }", task7)
        self.at(7)
        self.page.click("#modalbody [data-sure]", force=True)
        self.at(9)
        self.page.click("#modalbody [data-approve]", force=True)
        self.at(11)
        self.js("document.getElementById('modal').close()")
        self.end()

        # 8. You stay in control: the day's cost, a project paused, the stop button.
        self.scene(8)
        self.page.click('[data-tab="days"]', force=True)
        self.at(5.5)
        self.page.click('[data-tab="meadow"]', force=True)
        self.at(7)
        self.page.click('#scene .switch[data-pause="tea-shop"]', force=True)
        self.at(10)
        self.page.hover("#stop")
        self.end()

        # 9. Close.
        self.scene(9)
        self.page.mouse.move(SIZE[0] - 5, SIZE[1] - 5)
        self.at(self.scene_end - self.scene_start - 5)
        self.js("([t]) => __film.title(t, true, 0)", [TITLE[self.lang][1]])
        self.end()
        time.sleep(1.5)


def srt(subtitles, offset, path):
    stamp = lambda s: f"{int(s // 3600):02}:{int(s % 3600 // 60):02}:{int(s % 60):02},{int(s % 1 * 1000):03}"
    path.write_text("\n".join(f"{i}\n{stamp(a - offset)} --> {stamp(b - offset)}\n{text}\n"
                              for i, (a, b, text) in enumerate(subtitles, 1)), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Record Kandu's demo film.")
    parser.add_argument("--lang", default="en", choices=("en", "fa"))
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    film = Film(args.lang)
    world, url, server = start(args.lang)
    terminal = (HERE / "terminal.html").read_text(encoding="utf-8")
    screen = subprocess.Popen(["Xvfb", DISPLAY, "-screen", "0", f"{SIZE[0]}x{SIZE[1]}x24", "-nolisten", "tcp"])
    time.sleep(1)
    raw = OUT / f"raw-{args.lang}.mp4"
    try:
        with sync_playwright() as p:
            # A persistent context opens the browser's own window, so --kiosk takes it: no tabs, no address bar.
            # The window is measured in CSS pixels, which the scale factor turns into the screen's.
            context = p.chromium.launch_persistent_context(
                tempfile.mkdtemp(prefix="kandu-film-"), channel="chrome", headless=False, no_viewport=True,
                env={**os.environ, "DISPLAY": DISPLAY}, locale="fa-IR" if args.lang == "fa" else "en-GB",
                ignore_default_args=["--enable-automation"],
                args=["--kiosk", "--window-position=0,0", f"--window-size={int(SIZE[0] / SCALE)},{int(SIZE[1] / SCALE)}",
                      f"--force-device-scale-factor={SCALE}", "--disable-infobars", "--no-first-run",
                      "--hide-scrollbars", f"--lang={args.lang}"])
            context.add_init_script(OVERLAY)
            context.route("**/didban-demo*", lambda route: route.fulfill(body=terminal, content_type="text/html"))
            page = context.pages[0] if context.pages else context.new_page()
            recorder = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "x11grab", "-framerate", "30",
                                         "-video_size", f"{SIZE[0]}x{SIZE[1]}", "-i", DISPLAY, "-c:v", "libx264",
                                         "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", str(raw)],
                                        stdin=subprocess.PIPE)
            film.t0 = time.monotonic() + 0.3  # ffmpeg's first frame comes a moment after it starts
            film.play(page, world, url)
            recorder.communicate(b"q")
            context.close()
    finally:
        screen.terminate()
        server.shutdown()

    begin = film.starts[0] - 0.4
    clips = [OUT / c["file"] for c in film.clips]
    inputs = [x for clip in clips for x in ("-i", str(clip))]
    delays = "".join(f"[{i + 1}:a]adelay={int((start - begin + LEAD) * 1000)}:all=1[a{i}];"
                     for i, start in enumerate(film.starts))
    mix = "".join(f"[a{i}]" for i in range(len(clips))) + f"amix=inputs={len(clips)}:normalize=0[voice]"
    final = OUT / f"kandu-{args.lang}.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{begin:.3f}", "-i", str(raw), *inputs,
                    "-filter_complex", delays + mix, "-map", "0:v", "-map", "[voice]", "-c:v", "libx264", "-preset",
                    "slow", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(final)], check=True)
    srt(film.subtitles, begin, final.with_suffix(".srt"))
    print(final)


if __name__ == "__main__":
    main()
