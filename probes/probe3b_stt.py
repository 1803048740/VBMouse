"""多配置重试识别 probe2_dev1.wav 的语音段，诊断是配置问题还是录音质量问题。"""
import wave

import numpy as np

SRC = "probe2_dev1.wav"
T0, T1 = 44.5, 57.5

with wave.open(SRC, "rb") as w:
    sr = w.getframerate()
    ch = w.getnchannels()
    frames = w.readframes(w.getnframes())
audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
if ch > 1:
    audio = audio.reshape(-1, ch)[:, 0]
seg = audio[int(T0 * sr): int(T1 * sr)]
n16 = int(len(seg) * 16000 / sr)
xi = np.linspace(0, 1, n16, endpoint=False)
x = np.linspace(0, 1, len(seg), endpoint=False)
seg16 = np.interp(xi, x, seg).astype(np.float32)
seg16 = seg16 / (float(np.max(np.abs(seg16))) or 1.0) * 0.9

from faster_whisper import WhisperModel

model = WhisperModel("small", device="cpu", compute_type="int8")

configs = [
    dict(name="强制中文/无VAD", kw=dict(language="zh", vad_filter=False,
                                      condition_on_previous_text=False)),
    dict(name="强制中文/带VAD/中文提示词", kw=dict(language="zh", vad_filter=True,
                                                condition_on_previous_text=False,
                                                initial_prompt="以下是普通话口述的编程指令。")),
    dict(name="自动语言/带VAD", kw=dict(vad_filter=True,
                                      condition_on_previous_text=False)),
]
for c in configs:
    segs, info = model.transcribe(seg16, beam_size=5, **c["kw"])
    texts = [s.text.strip() for s in segs]
    print(f"== {c['name']} ->", "".join(texts) if texts else "(空)", flush=True)
