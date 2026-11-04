# Kandu's demo film

The films in [docs/video/](../docs/video/SCRIPT.md) are recorded here. Kandu's real dashboard runs on a demo world: two made-up projects, a GitHub in memory, and a director that writes the events a real run writes. Nothing reaches GitHub or Claude, and nothing of the recording machine is filmed.

| File | What it is |
|---|---|
| `config/` | the demo's Kandu config: two projects, `pocket-notes` and `tea-shop` |
| `world.py` | the made-up GitHub, yesterday's history, and the story step by step |
| `serve.py` | the dashboard on the demo world, for a look: `.venv/bin/python -m demo.serve [--lang fa]`, token `demo` |
| `terminal.html` | scene 2, a Didban session played from a script |
| `narration.en.txt`, `narration.fa.txt` | what is spoken, one paragraph per scene; the captions come from the same text |
| `voice.py` | reads the narration with Gemini TTS, voice `Kore`, one call per language, and cuts it into scenes |
| `film.py` | plays the scenes in Chrome on a virtual screen, records them, and lays in voice and captions |

## Make the film again

Needs `ffmpeg`, `Xvfb`, Google Chrome, Playwright for the system Python, and a Gemini key.

```
GEMINI_API_KEY=... .venv/bin/python -m demo.voice --lang en   # a paid call; --recut cuts the last take again for free
python3 -m demo.film --lang en
```

The result is `demo/out/kandu-en.mp4` with `kandu-en.srt` beside it; `demo/out/` is not committed. Listen to the voice before filming: a scene's share of the take should be close to its share of the text, and `voice.py` prints each clip's length. If two scenes ran together, take the voice again.
