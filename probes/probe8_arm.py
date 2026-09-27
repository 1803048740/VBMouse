"""布防实验：语音键按下时向厂商通道发送 ATVV 布防命令, 监听音频是否开始流动。

用法: python -X utf8 probe8_arm.py [时长秒, 默认50]
动作: 按住语音键凑近说5秒 -> 松开停3秒 -> 再按住说5秒
"""
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 50
t0 = time.time()
run_flag = [True]
LOG = open("probe8.log", "w", encoding="utf-8")


def log(msg):
    line = f"[{time.time() - t0:7.2f}s] {msg}"
    print(line, flush=True)
    LOG.write(line + "\n")
    LOG.flush()


# ---------- 音频 ----------
devs = sd.query_devices()
idx = next(i for i, d in enumerate(devs)
           if d["max_input_channels"] > 0 and "mic device" in d["name"].lower())
sr = int(devs[idx]["default_samplerate"])
buf = []
stream = sd.InputStream(device=idx, samplerate=sr, channels=1, dtype="float32",
                        blocksize=1024,
                        callback=lambda ind, f, t_, st: buf.append(ind.copy()))
stream.start()
log(f"AUD  端点[{idx}] @{sr}Hz 已开录")

# ---------- HID ----------
vendor_dev = [None]
ARM_CMDS = [
    ("write", [0x00, 0x50, 0x01]),
    ("write", [0x50, 0x01]),
    ("write", [0x00, 0x50, 0x02]),
    ("feature", [0x50, 0x01]),
    ("feature", [0x00, 0x50, 0x01]),
    ("write", [0x00, 0x4c, 0x01]),
]


def send_arm():
    dev = vendor_dev[0]
    if dev is None:
        log("ARM!! 厂商通道未就绪")
        return
    for kind, cmd in ARM_CMDS:
        try:
            if kind == "write":
                n = dev.write(cmd)
            else:
                n = dev.send_feature_report(cmd)
            log(f"ARM  {kind} {bytes(cmd).hex(' ')} -> {n}")
        except Exception as e:
            log(f"ARM!! {kind} {bytes(cmd).hex(' ')} -> {e}")


def hid_worker():
    import hid
    for h in hid.enumerate(0x1915, 0x1025):
        ifn = h["interface_number"]
        up = h.get("usage_page", 0)
        try:
            dev = hid.device()
            dev.open_path(h["path"])
        except Exception:
            continue
        if up == 0xFF01:
            vendor_dev[0] = dev
            log(f"HID  厂商通道就绪 接口{ifn}")

        def rd(dev=dev, ifn=ifn, up=up):
            errs = 0
            while run_flag[0] and errs < 2:
                try:
                    data = dev.read(512, timeout_ms=200)
                except Exception:
                    errs += 1
                    continue
                if data:
                    tag = {0xFF01: "VENDOR", 0x0C: "CONSUMER"}.get(up, "GEN")
                    log(f"HID[{tag}] len={len(data)}  {bytes(data).hex(' ')}")
                    if up == 0x0C and len(data) >= 2 and data[0] == 0x02 and data[1] == 0xCF:
                        log("KEY  语音键按下 -> 发送布防命令")
                        send_arm()

        threading.Thread(target=rd, daemon=True).start()


threading.Thread(target=hid_worker, daemon=True).start()

# 主动布防: 启动时与每15秒
send_arm()


def periodic_arm():
    while run_flag[0]:
        time.sleep(15)
        if run_flag[0]:
            send_arm()


threading.Thread(target=periodic_arm, daemon=True).start()

print(f"== probe8 开始 {DUR}s: 按住语音键说5秒 -> 停3秒 -> 再按住说5秒 ==", flush=True)
for step in range(DUR):
    time.sleep(1)
    if (step + 1) % 10 == 0:
        print(f"  ... t={step + 1}s", flush=True)
run_flag[0] = False
time.sleep(0.6)
stream.stop()
stream.close()

audio = np.concatenate(buf)
mono = audio[:, 0]
zero = float(np.mean(np.abs(mono) < 1e-6))
pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
with wave.open("probe8.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm.tobytes())
log(f"AUD  probe8.wav: {len(mono) / sr:.1f}s 纯零占比{zero * 100:.0f}% "
    f"最大幅度{float(np.abs(mono).max()):.4f}")
LOG.close()
print("== probe8 结束 ==", flush=True)
