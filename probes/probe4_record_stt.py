"""受控录音+自动分段转写：判断遥控器麦克风是否门控、语音质量是否可用。

流程: 15s 倒计时 -> 录 40s -> 每秒RMS -> 自动切出有声段 -> 逐段强制中文转写
用法: python -X utf8 probe4_record_stt.py
"""
import time
import wave

import numpy as np
import sounddevice as sd

DUR, LEAD = 40, 15

devs = sd.query_devices()
idx = next(i for i, d in enumerate(devs)
           if d["max_input_channels"] > 0 and "mic device" in d["name"].lower())
sr = int(devs[idx]["default_samplerate"])
print(f"== 设备[{idx}] {devs[idx]['name']} @{sr}Hz ==", flush=True)
for k in range(LEAD, 0, -1):
    print(f"  倒计时 {k}s", flush=True)
    time.sleep(1)
print("== 录音开始 ==", flush=True)
audio = sd.rec(DUR * sr, samplerate=sr, channels=1, device=idx, dtype="float32")
sd.wait()
print("== 录音结束 ==", flush=True)
mono = audio[:, 0]

pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
with wave.open("probe4.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm.tobytes())

# 0.5s 粒度 RMS
hop = sr // 2
frms = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)))
                 for i in range(len(mono) // hop)])
th = max(0.004, 3 * float(np.median(frms)))
print(f"== 响度包络 (阈值 {th:.4f}) ==")
for i, v in enumerate(frms):
    print(f"  t={i * 0.5:04.1f}s {v:.4f} {'#' * int(min(v, 0.3) * 80)}")

# 有声分段(合并 2s 内的空隙)
above = frms > th
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
print(f"== 检测到 {len(segs)} 个有声段: {[(a, b) for a, b in segs]} ==")

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
        text = "".join(s.text for s in out)
        print(f"  段[{a:.1f}-{b:.1f}s] -> {text.strip() or '(空)'}", flush=True)
print("== 完成 ==")
