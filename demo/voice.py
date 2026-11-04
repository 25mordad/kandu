"""Reads the narration aloud with Gemini, one call per language, and cuts it into one clip per scene.

    .venv/bin/python -m demo.voice --lang en

needs GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment, and ffmpeg. One call keeps the delivery even from
scene to scene; the scenes are then found again by the longest silences. Listen to the result: a word the model
adds, or a pause it takes inside a scene, shows up here and not in the text.
"""
import argparse
import base64
import json
import os
import re
import subprocess
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / "out"
MODEL = "gemini-2.5-pro-preview-tts"
VOICE = "Kore"
RATE = 24000
STYLE = {
    "en": "Read this as the calm, warm narrator of a short product film. Clear, unhurried, friendly, never salesy. "
          "Between paragraphs, pause for a full two seconds. Read all of it, to the last word, and only the text below.\n\n",
    "fa": "Read this colloquial Tehrani Persian text like a friend explaining something to a friend: relaxed, warm, conversational, "
          "never formal or bookish, never salesy. The name کَندو is the Persian word for beehive, said kan-DOO, never Kondo. "
          "Between paragraphs, pause for a full two seconds. Read all of it, to the last word, and only the text below.\n\n",
}


def scenes(lang):
    return [p.strip() for p in (HERE / f"narration.{lang}.txt").read_text(encoding="utf-8").split("\n\n") if p.strip()]


def speak(text, path):
    key = os.environ.get("GEMINI_API_KEY") or os.environ["GOOGLE_API_KEY"]
    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "temperature": 0.8,
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}}}}
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={key}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=600) as response:
        part = json.load(response)["candidates"][0]["content"]["parts"][0]
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(base64.b64decode(part["inlineData"]["data"]))


def silences(path):
    log = subprocess.run(["ffmpeg", "-i", str(path), "-af", "silencedetect=noise=-40dB:d=0.6", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", log)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", log)]
    return list(zip(starts, ends))


def length(path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def cut(path, texts, prefix):
    """Each boundary is the silence nearest to where the scene's share of the characters says it should be."""
    total = length(path)
    gaps = [g for g in silences(path) if g[0] > 0.5 and g[1] < total - 0.2]
    chars = [len(t) for t in texts]
    bounds, done = [], 0
    for n in chars[:-1]:
        done += n
        expected = total * done / sum(chars)
        gap = min(gaps, key=lambda g: abs((g[0] + g[1]) / 2 - expected) - (g[1] - g[0]))
        gaps.remove(gap)
        bounds.append(gap)
    bounds.sort()
    edges = [0.0] + [b for gap in bounds for b in gap] + [total]
    clips = []
    for i in range(len(texts)):
        start, end = edges[2 * i], edges[2 * i + 1]
        clip = OUT / f"{prefix}-{i + 1}.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-ss", f"{max(0, start - 0.05):.3f}",
                        "-to", f"{end + 0.15:.3f}", str(clip)], check=True)
        clips.append({"scene": i + 1, "file": clip.name, "seconds": round(length(clip), 2)})
    return clips


def main():
    parser = argparse.ArgumentParser(description="Narration for the demo film, with Gemini.")
    parser.add_argument("--lang", default="en", choices=("en", "fa"))
    parser.add_argument("--recut", action="store_true", help="cut the last take again, without a new call")
    args = parser.parse_args()
    OUT.mkdir(exist_ok=True)
    texts = scenes(args.lang)
    whole = OUT / f"voice-{args.lang}.wav"
    if not args.recut:
        speak(STYLE[args.lang] + "\n\n".join(texts), whole)
    clips = cut(whole, texts, f"voice-{args.lang}")
    (OUT / f"voice-{args.lang}.json").write_text(json.dumps(clips, indent=1))
    for clip in clips:
        print(clip)


if __name__ == "__main__":
    main()
