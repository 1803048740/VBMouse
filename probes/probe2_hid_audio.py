"""联合探测 v2：同时监测遥控器所有疑似麦克风端点 + 接收器原始 HID 报告。

用法: python -X utf8 probe2_hid_audio.py [时长秒, 默认60]

60 秒内建议动作:
  1. 按住语音键对遥控器说一句话(约5秒)
  2. 松开, 停2秒, 不按任何键直接对遥控器说一句话
  3. 依次按 OK / 返回 / 音量+ / 音量- / 主页, 键盘面随便敲几个字母
"""
import sys
import time
import threading
import wave

import numpy as np
import sounddevice as sd

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 60
t_start = time.time()
run_flag = [True]

# ---------- 1. 音频端点 ----------
devs = sd.query_devices()
inputs = [(i, d) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
print("== 输入设备 ==")
for i, d in inputs:
    print(f"  [{i}] {d['name']}  @{d['default_samplerate']:.0f}Hz ch={d['max_input_channels']}")

cands, seen = [], set()
for i, d in inputs:
    if "mic device" in d["name"].lower():
        key = (d["name"], round(d["default_samplerate"]), d["max_input_channels"])
        if key not in seen:
            seen.add(key)
            cands.append(i)

audio_buf = {i: [] for i in cands}
meta = {}
streams = []
print(f"== 同时录音端点: {cands} ==")
for i in cands:
    d = devs[i]
    sr = int(d["default_samplerate"])
    ch = min(2, d["max_input_channels"])

    def cb(indata, frames, t_, status, i=i):
        audio_buf[i].append(indata.copy())

    try:
        s = sd.InputStream(device=i, samplerate=sr, channels=ch, dtype="float32",
                           blocksize=1024, callback=cb)
        s.start()
        streams.append(s)
        meta[i] = (sr, ch)
        print(f"  端点[{i}] {d['name']} @{sr}Hz x{ch} 已开录")
    except Exception as e:
        print(f"  !! 端点[{i}] 打开失败: {e}")

# ---------- 2. 原始 HID 监听 (接收器 VID_1915/PID_1025) ----------
hid_reports = {}  # (接口, 首字节, 长度) -> 统计


def hid_worker():
    import hid
    infos = hid.enumerate(0x1915, 0x1025)
    if not infos:
        print("  !! 没找到 VID_1915/PID_1025 的 HID 接口")
        return
    for h in infos:
        ifn = h.get("interface_number")
        try:
            dev = hid.device()
            dev.open_path(h["path"])
        except Exception as e:
            print(f"  !! HID 打不开 接口{ifn}: {e}")
            continue
        print(f"  >> HID 监听中: 接口{ifn} usage_page=0x{h.get('usage_page', 0):04x} "
              f"usage=0x{h.get('usage', 0):04x}")
        threading.Thread(target=hid_reader, args=(dev, ifn), daemon=True).start()


def hid_reader(dev, ifn):
    while run_flag[0]:
        try:
            data = dev.read(256, timeout_ms=200)
        except Exception:
            break
        if not data:
            continue
        key = (ifn, data[0], len(data))
        ent = hid_reports.setdefault(key, {"n": 0, "samples": [], "first": 0.0, "last": 0.0})
        ent["n"] += 1
        now = time.time() - t_start
        if ent["first"] == 0.0:
            ent["first"] = now
        ent["last"] = now
        if len(ent["samples"]) < 3:
            ent["samples"].append((now, bytes(data).hex(" ")))


try:
    import hid  # noqa: F401
    threading.Thread(target=hid_worker, daemon=True).start()
except Exception as e:
    print(f"  !! hidapi 不可用: {e}")

# ---------- 3. 普通键盘钩子 ----------
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

# ---------- 4. 主循环 ----------
print(f"== 联合监测开始, {DUR}s ==")
for step in range(DUR):
    time.sleep(1)
    if (step + 1) % 10 == 0:
        print(f"  ... t={step + 1}s / {DUR}s", flush=True)
run_flag[0] = False
for s in streams:
    s.stop()
    s.close()
if kl:
    kl.stop()
print("== 监测结束 ==")

# ---------- 5. 分析 ----------
print("== 各端点每秒 RMS ==")
for i in cands:
    if i not in meta or not audio_buf[i]:
        print(f"  端点[{i}]: 无数据")
        continue
    sr, ch = meta[i]
    audio = np.concatenate(audio_buf[i])
    mono = audio[:, 0] if ch > 1 else audio
    n_sec = len(mono) // sr
    wav_name = f"probe2_dev{i}.wav"
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(wav_name, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    print(f"  -- 端点[{i}] @{sr}Hz 已存 {wav_name}")
    for s_ in range(n_sec):
        seg = mono[s_ * sr : (s_ + 1) * sr]
        rms = float(np.sqrt(np.mean(seg**2))) if len(seg) else 0.0
        if rms > 0.003 or s_ % 5 == 0:  # 有声就打, 无声每5秒打一行
            print(f"    t={s_:02d}s  {rms:.4f}  {'#' * int(min(rms, 0.5) * 100)}")

print("== 接收器原始 HID 报告 (按 接口/报告ID/长度 聚合) ==")
if hid_reports:
    for (ifn, rid, ln), ent in sorted(hid_reports.items()):
        print(f"  接口{ifn} 报告ID0x{rid:02x} 长度{ln}: {ent['n']} 次  "
              f"t={ent['first']:.1f}s ~ {ent['last']:.1f}s")
        for ts, hexs in ent["samples"]:
            print(f"      t={ts:6.1f}s  {hexs}")
else:
    print("  (没有收到任何 HID 报告)")

print("== pynput 按键事件 ==")
if key_events:
    for t, label in key_events:
        print(f"  t={t:6.1f}s  {label}")
else:
    print("  (没有捕获到)")
