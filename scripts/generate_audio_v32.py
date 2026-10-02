"""Build original loop music and local Mandarin announcements (build-time only)."""

import argparse
import array
import json
import math
import os
import subprocess
import tempfile
import wave
from pathlib import Path

PUBLIC_LINES = {
    "welcome": "欢迎来到月下狼人杀。",
    "night": "天黑请闭眼。",
    "day": "天亮了。",
    "speech": "开始发言。",
    "vote": "开始投票。",
    "vote_end": "投票结束。",
    "gameover": "游戏结束。",
}
PRIVATE_LINES = {
    "wolf_discuss": "狼人请讨论。",
    "wolf_kill": "狼人请行动。",
    "seer_inspect": "预言家请选择查验目标。",
    "witch": "女巫请行动。",
    "guard_protect": "守卫请选择守护目标。",
    "wolf_beauty_charm": "狼美人请选择魅惑目标。",
    "hunter_shoot": "猎人请决定是否开枪。",
    "wolf_king_shoot": "狼王请决定是否开枪。",
    "dream_visit": "摄梦人请选择梦游目标。",
    "grave_inspect": "守墓人请选择已出局玩家。",
    "crow_mark": "乌鸦请选择标记目标。",
    "bear_watch": "驯熊师请观察相邻玩家。",
}


def music(path, notes, tempo):
    # An original, deterministic arpeggio over a soft drone, exactly eight bars.
    rate, beats = 22050, 32
    duration = beats * 60 / tempo
    values = array.array("h")
    for i in range(round(rate * duration)):
        t = i / rate
        beat = t * tempo / 60
        note = notes[int(beat * 2) % len(notes)]
        freq = 440 * 2 ** ((note - 69) / 12)
        envelope = math.exp(-6 * (beat * 2 % 1))
        melody = (math.sin(2 * math.pi * freq * t) + 0.18 * math.sin(4 * math.pi * freq * t)) * envelope
        drone = math.sin(2 * math.pi * 110 * t) * 0.18
        edge = min(1, t * 8, (duration - t) * 8)
        values.append(int((melody * 0.18 + drone) * edge * 20000))
    with wave.open(str(path), "wb") as w:
        w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        w.writeframes(values.tobytes())


def effects(root):
    for name, frequencies in {"death": (420, 270, 150), "skill": (500, 750, 1000)}.items():
        rate = 22050
        values = array.array("h")
        for frequency in frequencies:
            for i in range(round(rate * 0.13)):
                t = i / rate
                values.append(int(9000 * math.sin(2 * math.pi * frequency * t) * math.sin(math.pi * t / 0.13)))
        with wave.open(str(root / f"{name}.wav"), "wb") as w:
            w.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            w.writeframes(values.tobytes())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--espeak", default="espeak-ng")
    parser.add_argument("--espeak-data-parent")
    parser.add_argument("--output", default="app/static/assets/audio")
    args = parser.parse_args()
    root = Path(args.output)
    (root / "host").mkdir(parents=True, exist_ok=True)
    (root / "bgm").mkdir(exist_ok=True)
    effects(root)
    lines = {**PUBLIC_LINES, **PRIVATE_LINES, **{f"out-{i}": f"{i}号出局，请发表遗言。" for i in range(1, 17)}}
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "clip.wav"
        for name, text in lines.items():
            command = [args.espeak, "-v", "cmn", "-s", "160", "-w", str(wav), text]
            if args.espeak_data_parent:
                command.insert(1, "--path=" + args.espeak_data_parent)
            subprocess.run(command, check=True, env=os.environ)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav),
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    str(root / "host" / f"{name}.mp3"),
                ],
                check=True,
            )
        tracks = {
            "lobby": ([60, 64, 67, 72, 67, 64, 62, 67], 76),
            "night": ([45, 52, 57, 60, 57, 52, 48, 55], 60),
            "day": ([60, 67, 64, 69, 65, 72, 67, 64], 84),
            "vote": ([50, 57, 62, 65, 50, 57, 60, 64], 94),
            "settlement": ([60, 64, 67, 76, 72, 67, 64, 72], 72),
        }
        for name, (notes, tempo) in tracks.items():
            music(wav, notes, tempo)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(wav),
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "80k",
                    str(root / "bgm" / f"{name}.mp3"),
                ],
                check=True,
            )
    (root / "manifest-v32.json").write_text(
        json.dumps(
            {
                "host": lines,
                "bgm": list(tracks),
                "speech_source": "eSpeak NG 1.51 Mandarin, generated locally",
                "music_source": "Original deterministic synthesis in scripts/generate_audio_v32.py",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
