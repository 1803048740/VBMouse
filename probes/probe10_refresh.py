"""刷新流实验: 连续多次 sd.rec(每次新开流=SET_INTERFACE循环), 验证能否踢活语音桥。

用法: python -X utf8 probe10_refresh.py
动作: 全程让房间有声音(视频播放); 窗口1-4期间不要碰遥控器;
     窗口5时拿起遥控器按住语音键说"帮我写一个hello world程序"。
"""
import time
import wave

import numpy as np
import sounddevice as sd

devs = sd.query_devices()
idx = next(i for i, d in enumerate(devs)
           if d["max_input_channels"] > 0 and "mic device" in d["name"].lower())
sr = int(devs[idx]["default_samplerate"])
print(f"== 设备[{idx}] @{sr}Hz ==", flush=True)
print("== 现在开始: 全程房间保持有声音; 窗口1-4(共约50秒)不要碰遥控器 ==", flush=True)


def rec(sec, tag):
    a = sd.rec(int(sec * sr), samplerate=sr, channels=1, device=idx, dtype="float32")
    sd.wait()
    m = a[:, 0]
    zero = float(np.mean(np.abs(m) < 1e-6)) * 100
    print(f"  {tag}: 纯零{zero:.0f}%  峰值{float(np.abs(m).max()):.4f}", flush=True)
    return m


parts = [rec(10, f"窗口{k + 1} (新开流)") for k in range(4)]

print("== 现在拿起遥控器, 按住语音键凑近说: 帮我写一个hello world程序 ==", flush=True)
parts.append(rec(12, "窗口5 (语音键+说话)"))

mono = np.concatenate(parts)
pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
with wave.open("probe10.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm.tobytes())
print("== 已存 probe10.wav ==", flush=True)
