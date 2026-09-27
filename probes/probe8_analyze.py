"""分析 probe8.wav: 音频出现的时间窗 + 转写。"""
import wave

import numpy as np

with wave.open("probe8.wav", "rb") as w:
    sr = w.getframerate()
    frames = w.readframes(w.getnframes())
mono = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
print(f"== probe8.wav {len(mono) / sr:.1f}s @{sr}Hz ==")

hop = sr // 2
frms = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)))
                 for i in range(len(mono) // hop)])
print("== 包络 (全部帧) ==")
for i, v in enumerate(frms):
    print(f"  t={i * 0.5:05.1f}s {v:.4f} {'#' * int(min(v, 0.3) * 80)}")

above = frms > 0.005
segs, i = [], 0
while i < len(above):
    if above[i]:
        j = i
        while j < len(above) and (above[j] or (j + 4 < len(above) and above[j:j + 4].any())):
            j += 1
        segs.append((i * 0.5, j * 0.5))
        i = j
    else:
        i += 1
segs = [(a, b) for a, b in segs if b - a >= 1.0]
print(f"== 有声段: {segs} ==")

if segs:
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    for a, b in segs:
        part = mono[int(a * sr): int(b * sr)]
        n16 = int(len(part) * 16000 / sr)
        xi = np.linspace(0, 1, n16, endpoint=False)
        x = np.linspace(0, 1, len(part), endpoint=False)
        p16 = np.interp(xi, x, part).astype(np.float32)
        p16 = p16 / (float(np.max(np.abs(p16))) or 1.0) * 0.9
        out, info = model.transcribe(p16, language="zh", beam_size=5,
                                     condition_on_previous_text=False,
                                     initial_prompt="以下是普通话口述的编程指令。")
        text = "".join(s_.text for s_ in out)
        print(f"  段[{a:.1f}-{b:.1f}s] -> {text.strip() or '(空)'}", flush=True)
