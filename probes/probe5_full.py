"""全通道关联监测 v3：蓝牙键盘钩子 + 2.4G 接收器原始 HID + 麦克风录音，90 秒。

用法: python -X utf8 probe5_full.py [时长秒, 默认90]
"""
import sys
import time
import threading
import wave

import numpy as np
import sounddevice as sd

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 90
t_start = time.time()
run_flag = [True]

# ---------- 音频 ----------
devs = sd.query_devices()
idx = next(i for i, d in enumerate(devs)
           if d["max_input_channels"] > 0 and "mic device" in d["name"].lower())
sr = int(devs[idx]["default_samplerate"])
buf = []
s = sd.InputStream(device=idx, samplerate=sr, channels=1, dtype="float32",
                   blocksize=1024, callback=lambda ind, f, t_, st: buf.append(ind.copy()))
s.start()
print(f"== 音频[{idx}] {devs[idx]['name']} @{sr}Hz 已开录 ==", flush=True)

# ---------- 2.4G 原始 HID ----------
key_reports = []   # 小报告(疑似按键): (t, 接口, 报告ID, hex)
flood = {}         # 大报告(疑似鼠标): (接口,报告ID,长度) -> [n, first, last]


def hid_reader(dev, ifn):
    while run_flag[0]:
        try:
            data = dev.read(256, timeout_ms=200)
        except Exception:
            break
        if not data:
            continue
        now = time.time() - t_start
        if len(data) <= 8:
            key_reports.append((now, ifn, data[0], bytes(data).hex(" ")))
        else:
            k = (ifn, data[0], len(data))
            e = flood.setdefault(k, [0, now, now])
            e[0] += 1
            e[2] = now


def hid_worker():
    import hid
    for h in hid.enumerate(0x1915, 0x1025):
        try:
            dev = hid.device()
            dev.open_path(h["path"])
        except Exception as e:
            print(f"  !! HID 打不开 接口{h.get('interface_number')}: {e}")
            continue
        threading.Thread(target=hid_reader, args=(dev, h["interface_number"]),
                         daemon=True).start()


try:
    import hid  # noqa: F401
    threading.Thread(target=hid_worker, daemon=True).start()
except Exception as e:
    print(f"  !! hidapi 不可用: {e}")

# ---------- 蓝牙/系统键盘钩子 ----------
key_events = []
try:
    from pynput import keyboard

    def on_press(key):
        label = getattr(key, "char", None) or str(key)
        key_events.append((time.time() - t_start, label))

    kl = keyboard.Listener(on_press=on_press)
    kl.start()
except Exception as e:
    kl = None
    print(f"  !! pynput 不可用: {e}")

print(f"== 联合监测开始 {DUR}s ==", flush=True)
for step in range(DUR):
    time.sleep(1)
    if (step + 1) % 10 == 0:
        print(f"  ... t={step + 1}s / {DUR}s", flush=True)
run_flag[0] = False
s.stop()
s.close()
if kl:
    kl.stop()
print("== 监测结束 ==", flush=True)

# ---------- 分析 ----------
audio = np.concatenate(buf) if buf else np.zeros((1, 1), dtype="float32")
mono = audio[:, 0]
pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
with wave.open("probe5.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm.tobytes())

hop = sr // 2
frms = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)))
                 for i in range(len(mono) // hop)])
th = max(0.004, 3 * float(np.median(frms)))
print(f"== 音频包络 (阈值 {th:.4f}, 只列 >0.0005 的帧) ==")
for i, v in enumerate(frms):
    if v > 0.0005:
        print(f"  t={i * 0.5:05.1f}s {v:.4f} {'#' * int(min(v, 0.3) * 80)}")

print("== 2.4G 原始 HID 小报告(疑似按键, 全列表) ==")
if key_reports:
    for t, ifn, rid, hx in key_reports:
        print(f"  t={t:6.2f}s 接口{ifn} ID0x{rid:02x}  {hx}")
else:
    print("  (无)")
print("== 2.4G 原始 HID 大报告(疑似鼠标, 聚合) ==")
if flood:
    for (ifn, rid, ln), (n, f0, f1) in sorted(flood.items()):
        print(f"  接口{ifn} ID0x{rid:02x} 长度{ln}: {n} 次  t={f0:.1f}~{f1:.1f}s")
else:
    print("  (无)")

print("== pynput 按键(蓝牙/系统层) ==")
if key_events:
    for t, label in key_events:
        print(f"  t={t:6.2f}s  {label}")
else:
    print("  (无)")
