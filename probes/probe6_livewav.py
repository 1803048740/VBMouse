"""分析 live.wav：重点看按键唤醒后的语音窗口。"""
import wave

import numpy as np

with wave.open("live.wav", "rb") as w:
    sr = w.getframerate()
    frames = w.readframes(w.getnframes())
mono = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
print(f"== live.wav {len(mono) / sr:.1f}s @{sr}Hz ==")

hop = sr // 2
frms = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)))
                 for i in range(len(mono) // hop)])
print("== 全程包络 (>0.0003 的帧) ==")
for i, v in enumerate(frms):
    if v > 0.0003:
        print(f"  t={i * 0.5:06.1f}s {v:.4f} {'#' * int(min(v, 0.3) * 80)}")

# 全程统计
zero_frac = float(np.mean(np.abs(mono) < 1e-6))
print(f"== 纯零样本占比: {zero_frac * 100:.1f}%  全程最大幅度: {float(np.abs(mono).max()):.4f} ==")

# 对 99s 之后的窗口做转写(不管有没有标记)
from faster_whisper import WhisperModel
model = WhisperModel("small", device="cpu", compute_type="int8")
part = mono[99 * sr:]
if float(np.abs(part).max()) > 1e-4:
    n16 = int(len(part) * 16000 / sr)
    xi = np.linspace(0, 1, n16, endpoint=False)
    x = np.linspace(0, 1, len(part), endpoint=False)
    p16 = np.interp(xi, x, part).astype(np.float32)
    p16 = p16 / (float(np.max(np.abs(p16))) or 1.0) * 0.9
    out, info = model.transcribe(p16, language="zh", beam_size=5,
                                 condition_on_previous_text=False,
                                 initial_prompt="以下是普通话口述的编程指令。")
    texts = [s_.text for s_ in out]
    print(f"== 99s~ 转写({info.language}) ->", "".join(texts).strip() or "(空)")
else:
    print("== 99s之后全程数字静音：语音流从未打开 ==")
