"""Opens a page from the worker's own folder in a headless browser, and changes nothing.

A worker cannot open a browser, so a criterion like "plays in the browser" used to stop the task.
This prints what the page did: JavaScript errors, failed requests, and pictures at the times asked
for, which the worker then looks at with Read. It stands alone so a project's own Python, the one
that has Playwright, can run it by path:

    <python with playwright> kandu/look.py page.html [--query play] [--at 2,4,7] [--play 3]

Only a file inside the current folder is opened. Pictures go to a new folder under the system's temp
folder, never into the worktree, so they cannot end up in a commit.
"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

VIEWPORT = {"width": 1080, "height": 1920}
# A system Chrome first, as the projects' own render scripts use; else the one Playwright installed.
CHROMES = ("/usr/bin/google-chrome", shutil.which("google-chrome") or "", str(Path.home() / ".local/bin/google-chrome"))


def page_path(name, root):
    """The page as a path inside `root`, or None if it lies outside or is not a file."""
    path = (root / name).resolve()
    if not path.is_file() or not path.is_relative_to(root.resolve()):
        return None
    return path


def times(text):
    return [float(t) for t in text.split(",") if t.strip()] if text else []


def look(path, query, at, play, out):
    from playwright.sync_api import sync_playwright

    chrome = next((c for c in CHROMES if c and Path(c).exists()), None)
    problems, shots, clock = [], [], None
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome, args=["--allow-file-access-from-files"])
        page = browser.new_page(viewport=VIEWPORT)
        page.on("pageerror", lambda e: problems.append(f"error: {e}"))
        page.on("console", lambda m: m.type == "error" and problems.append(f"console: {m.text}"))
        page.on("requestfailed", lambda r: problems.append(f"failed: {r.url}"))
        page.on("response", lambda r: r.status >= 400 and problems.append(f"{r.status}: {r.url}"))
        page.goto(path.as_uri() + (f"?{query}" if query else ""))
        page.wait_for_timeout(1000)
        if play and page.query_selector("#pp"):  # the motion kit's play button
            page.click("#pp")
            page.wait_for_timeout(play * 1000)
            clock = page.text_content("#tt")
            page.click("#pp")
        can_seek = page.evaluate("typeof window.render === 'function'")
        for t in at:
            if can_seek:
                page.evaluate(f"window.render({t})")
                page.wait_for_timeout(150)
            shot = out / f"{path.stem}-{t:g}s.png"
            page.screenshot(path=str(shot))
            shots.append(shot)
        if not at:
            shot = out / f"{path.stem}.png"
            page.screenshot(path=str(shot))
            shots.append(shot)
        browser.close()
    return problems, shots, clock, can_seek


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open a page from this folder headless and report what it did.")
    parser.add_argument("pages", nargs="+", help="HTML files inside the current folder")
    parser.add_argument("--query", default="", help="query string without '?', for example: play")
    parser.add_argument("--at", default="", help="seconds to draw with window.render, for example: 2,4,7")
    parser.add_argument("--play", type=float, default=0, help="press the motion kit's play button for this many seconds")
    args = parser.parse_args(argv)
    root = Path.cwd()
    out = Path(tempfile.mkdtemp(prefix="kandu-look-"))
    failed = False
    for name in args.pages:
        path = page_path(name, root)
        if path is None:
            print(f"{name}: not a file inside {root}")
            failed = True
            continue
        problems, shots, clock, can_seek = look(path, args.query, times(args.at), args.play, out)
        print(f"{name}:")
        if clock is not None:
            print(f"  clock after {args.play:g}s of play: {clock}")
        if args.at and not can_seek:
            print("  no window.render: every picture shows the page as it is")
        print("  " + ("\n  ".join(problems) if problems else "no errors, no failed requests"))
        for shot in shots:
            print(f"  picture: {shot}")
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
