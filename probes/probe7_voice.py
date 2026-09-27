"""决定性探测 v7：语音流到底走哪里？
1) 44.1k 端点照录
2) 强制尝试 16k 端点 [24] (MME) 与任何 WASAPI 同名设备
3) 厂商 TLC(0xff01) 全量逐条记录(不聚合) -> 若有 64B 包洪流 = 语音走HID
4) 读失败的键盘/系统控制 TLC 自动重连
5) 消费 TLC 继续抓语音键事件

用法: python -X utf8 probe7_voice.py [时长秒, 默认60]
按住语音键说两句话, 每句5秒, 间隔5秒。
"""
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 60
t0 = time.time()
run_flag = [True]
LOG = open("probe7.log", "w", encoding="utf-8")


def log(msg):
    line = f"[{time.time() - t0:7.2f}s] {msg}"
    print(line, flush=True)
    LOG.write(line + "\n")
    LOG.flush()


# ---------- 音频: 多端点 ----------
streams = {}
devs = sd.query_devices()
targets = []
for i, d in enumerate(devs):
    if d["max_input_channels"] > 0 and "mic device" in d["name"].lower():
        targets.append((i, int(d["default_samplerate"])))
# 也找 WASAPI 的同名设备
for hapi in sd.query_hostapis():
    if "wasapi" in hapi["name"].lower():
        for i in hapi["devices"]:
            d = devs[i]
            if d["max_input_channels"] > 0 and "mic device" in d["name"].lower():
                targets.append((i, int(d["default_samplerate"])))

buffers = {}
for idx, sr in targets:
    key = f"{idx}@{sr}"
    if key in buffers:
        continue
    buf = []
    try:
        ch = min(2, devs[idx]["max_input_channels"])
        s = sd.InputStream(device=idx, samplerate=sr, channels=ch, dtype="float32",
                           blocksize=1024,
                           callback=lambda ind, f, t_, st, b=buf: b.append(ind.copy()))
        s.start()
        streams[key] = (s, sr, ch)
        buffers[key] = buf
        log(f"AUD  端点[{idx}] @{sr}Hz x{ch} 已开录 ({devs[idx]['name']})")
    except Exception as e:
        log(f"AUD!! 端点[{idx}] @{sr}Hz 打不开: {e}")

# ---------- HID ----------
def hid_worker():
    import hid
    attempts = 0
    while run_flag[0] and attempts < 30:
        attempts += 1
        opened_any = False
        for h in hid.enumerate(0x1915, 0x1025):
            ifn = h["interface_number"]
            up = h.get("usage_page", 0)
            tag = {0xFF01: "VENDOR", 0x0C: "CONSUMER", 0x01: "GENERIC"}.get(up, "?")
            try:
                dev = hid.device()
                dev.open_path(h["path"])
            except Exception:
                continue
            opened_any = True
            log(f"HID  接口{ifn}({tag}) 打开成功")

            def rd(dev=dev, ifn=ifn, tag=tag):
                errs = 0
                while run_flag[0] and errs < 3:
                    try:
                        data = dev.read(512, timeout_ms=200)
                    except Exception as e:
                        errs += 1
                        log(f"HID!! 接口{ifn}({tag}) 读异常: {e}")
                        time.sleep(0.3)
                        continue
                    if data:
                        log(f"HID[{tag}] 接口{ifn} len={len(data)}  {bytes(data).hex(' ')}")

            threading.Thread(target=rd, daemon=True).start()
        if opened_any:
            return
        time.sleep(1)


threading.Thread(target=hid_worker, daemon=True).start()

print(f"== probe7 开始 {DUR}s: 请按住语音键说两句话(各5秒, 间隔5秒) ==", flush=True)
for step in range(DUR):
    time.sleep(1)
    if (step + 1) % 10 == 0:
        print(f"  ... t={step + 1}s", flush=True)
run_flag[0] = False
time.sleep(0.6)
for key, (s, sr, ch) in streams.items():
    s.stop()
    s.close()
    if buffers[key]:
        audio = np.concatenate(buffers[key])
        mono = audio[:, 0] if ch > 1 else audio
        zero = float(np.mean(np.abs(mono) < 1e-6))
        name = f"probe7_{key.replace('@', '_').replace(' ', '')}.wav"
        pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
        with wave.open(name, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        log(f"AUD  {name}: {len(mono) / sr:.1f}s 纯零占比{zero * 100:.0f}% "
            f"最大幅度{float(np.abs(mono).max()):.4f}")
LOG.close()
print("== probe7 结束 ==", flush=True)
