"""从 probe2_dev1.wav 截取说话段并做本地语音识别，验证 说话→文字 全链路。

用法: python -X utf8 probe3_stt.py
"""
import wave

import numpy as np

SRC = "probe2_dev1.wav"
T0, T1 = 44.0, 58.0

with wave.open(SRC, "rb") as w:
    sr = w.getframerate()
    ch = w.getnchannels()
    assert w.getsampwidth() == 2
    frames = w.readframes(w.getnframes())
audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
if ch > 1:
    audio = audio.reshape(-1, ch)[:, 0]
seg = audio[int(T0 * sr): int(T1 * sr)]

# 重采样到 16k（whisper 的输入格式）
n16 = int(len(seg) * 16000 / sr)
x = np.linspace(0, 1, len(seg), endpoint=False)
xi = np.linspace(0, 1, n16, endpoint=False)
seg16 = np.interp(xi, x, seg).astype(np.float32)
peak = float(np.max(np.abs(seg16))) or 1.0
seg16 = seg16 / peak * 0.8
print(f"== 截取 {T0}-{T1}s -> 16kHz, 原始峰值 {peak:.3f}, 时长 {len(seg16) / 16000:.1f}s ==")

from faster_whisper import WhisperModel

print("== 加载模型 small/int8 (首次会下载权重) ==")
model = WhisperModel("small", device="cpu", compute_type="int8")
segs, info = model.transcribe(seg16, beam_size=5, vad_filter=True)
print(f"== 检测语言: {info.language} (p={info.language_probability:.2f}) ==")
texts = []
for s in segs:
    print(f"  [{s.start:5.1f}-{s.end:5.1f}s] {s.text}")
    texts.append(s.text.strip())
print("== 识别结果 ==")
print("".join(texts) if texts else "  (空)")
