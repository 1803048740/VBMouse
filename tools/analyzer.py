"""通用录音分析器: 包络 + 有声分段 + 中文转写。

用法: python -X utf8 analyzer.py <wav文件> [起始秒] [结束秒]
不给起止则分析全文件。
"""
import sys
import wave

import numpy as np

src = sys.argv[1] if len(sys.argv) > 1 else "probe9.wav"
T0 = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
T1 = float(sys.argv[3]) if len(sys.argv) > 3 else None

with wave.open(src, "rb") as w:
    sr = w.getframerate()
    frames = w.readframes(w.getnframes())
mono = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
if T1 is None:
    T1 = len(mono) / sr
mono = mono[int(T0 * sr): int(T1 * sr)]
print(f"== {src} [{T0:.1f}-{T1:.1f}s] @{sr}Hz 纯零{float(np.mean(np.abs(mono) < 1e-6)) * 100:.0f}% "
      f"最大{float(np.abs(mono).max()):.4f} ==")

hop = sr // 2
frms = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)))
                 for i in range(len(mono) // hop)])
print("== 包络 (>0.0003 的帧) ==")
for i, v in enumerate(frms):
    if v > 0.0003:
        print(f"  t={T0 + i * 0.5:07.1f}s {v:.4f} {'#' * int(min(v, 0.3) * 80)}")

above = frms > 0.004
segs, i = [], 0
while i < len(above):
    if above[i]:
        j = i
        while j < len(above) and (above[j] or (j + 4 < len(above) and above[j:j + 4].any())):
            j += 1
        segs.append((T0 + i * 0.5, T0 + j * 0.5))
        i = j
    else:
        i += 1
segs = [(a, b) for a, b in segs if b - a >= 1.0]
print(f"== 有声段: {[(round(a, 1), round(b, 1)) for a, b in segs]} ==")

if segs:
    from faster_whisper import WhisperModel
    model = WhisperModel("small", device="cpu", compute_type="int8")
    for a, b in segs:
        part = mono[int((a - T0) * sr): int((b - T0) * sr)]
        n16 = int(len(part) * 16000 / sr)
        xi = np.linspace(0, 1, n16, endpoint=False)
        x = np.linspace(0, 1, len(part), endpoint=False)
        p16 = np.interp(xi, x, part).astype(np.float32)
        p16 = p16 / (float(np.max(np.abs(p16))) or 1.0) * 0.9
        out, _ = model.transcribe(p16, language="zh", beam_size=5,
                                  condition_on_previous_text=False,
                                  initial_prompt="以下是普通话口述的编程指令。")
        text = "".join(s_.text for s_ in out)
        print(f"  段[{a:.1f}-{b:.1f}s] -> {text.strip() or '(空)'}", flush=True)
