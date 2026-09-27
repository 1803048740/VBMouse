"""探测遥控器麦克风与语音键行为。

用法: python -X utf8 probe1_audio.py [录音秒数, 默认25]

流程: 找到遥控器的 USB 麦克风 -> 录音 N 秒(同时全局记录按键)
     -> 输出设备清单 / 每秒响度(RMS) / 按键时间线 / 保存 probe1.wav
"""
import sys
import time
import wave

import numpy as np
import sounddevice as sd

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 25
OUT_WAV = "probe1.wav"

devs = sd.query_devices()
inputs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
print("== 可用输入设备 ==")
for i, d in inputs:
    print(f"  [{i}] {d['name']}  @{d['default_samplerate']:.0f}Hz  ch={d['max_input_channels']}")


def pick_device():
    # 优先匹配 PnP 里看到的端点名 "Mic Device"，其次退回任何 USB 名字的设备
    for pat in ("mic device", "usb"):
        hits = [i for i, d in inputs if pat in d["name"].lower()]
        if hits:
            return hits[0], pat
    return None, None


idx, pat = pick_device()
if idx is None:
    print("!! 没找到遥控器麦克风(候选: 'Mic Device'/'USB')，请确认接收器已插好")
    sys.exit(1)

d = devs[idx]
sr = int(d["default_samplerate"])
ch = min(2, d["max_input_channels"])
print(f"== 选定 [{idx}] {d['name']}  @ {sr}Hz x{ch}ch  (匹配规则: {pat}) ==")

# ---- 全局按键监听(独立线程) ----
events = []
try:
    from pynput import keyboard

    def on_press(key):
        label = getattr(key, "char", None) or str(key)
        events.append((time.time(), label))

    kl = keyboard.Listener(on_press=on_press)
    kl.start()
    print("== 键盘钩子已启动 ==")
except Exception as e:
    kl = None
    print(f"!! pynput 不可用, 本次只测音频: {e}")

LEAD = int(sys.argv[2]) if len(sys.argv) > 2 else 10
print("== 准备: ① 按住语音键说5秒 ② 停2秒 ③ 不按键说3秒 ==", flush=True)
for k in range(LEAD, 0, -1):
    print(f"== 录音将在 {k} 秒后开始 ...", flush=True)
    time.sleep(1)

print(f"== 开始录音 {DUR}s ... (对遥控器说话) ==")
t0 = time.time()
try:
    audio = sd.rec(int(DUR * sr), samplerate=sr, channels=ch, device=idx, dtype="float32")
    sd.wait()
finally:
    if kl:
        kl.stop()
print("== 录音结束 ==")

# ---- 保存 wav ----
pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
with wave.open(OUT_WAV, "wb") as w:
    w.setnchannels(ch)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm.tobytes())
print(f"== 已保存 {OUT_WAV} ({DUR}s @ {sr}Hz x{ch}ch) ==")

# ---- 每秒响度 ----
mono = audio[:, 0]
print("== 每秒响度 RMS (#越多越响) ==")
for s in range(DUR):
    seg = mono[s * sr : (s + 1) * sr]
    rms = float(np.sqrt(np.mean(seg**2))) if len(seg) else 0.0
    print(f"  t={s:02d}s  {rms:.4f}  {'#' * int(min(rms, 0.5) * 100)}")

# ---- 按键时间线 ----
print("== 录音期间捕获的按键 ==")
if events:
    for t, label in events:
        print(f"  t={t - t0:6.2f}s  {label}")
else:
    print("  (没有捕获到任何按键事件)")
