"""实时按键/语音监视器：监听 2.4G 接收器所有 HID 接口原始报告 + 麦克风响度。

- 按键类小报告(<=8字节)逐条落盘 monitor.log（带时间戳）
- 鼠标类大报告按秒聚合，避免刷屏
- 麦克风: 越过阈值标记 [语音开始/结束]；心跳显示链路 活跃/休眠(全零)
- 全程音频存 live.wav

用法: python -X utf8 live_monitor.py [时长秒, 默认240]
"""
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 240
t0 = time.time()
run_flag = [True]
LOG = open("monitor.log", "a", encoding="utf-8")


def log(msg):
    line = f"[{time.time() - t0:7.2f}s] {msg}"
    print(line, flush=True)
    LOG.write(line + "\n")
    LOG.flush()


log(f"== live monitor 启动, 时长 {DUR}s ==")

# ---------- 2.4G 原始 HID ----------
mouse_counts = {}
mouse_lock = threading.Lock()


def hid_worker():
    import hid
    infos = hid.enumerate(0x1915, 0x1025)
    log(f"HID  发现 {len(infos)} 个接口")
    for h in infos:
        try:
            dev = hid.device()
            dev.open_path(h["path"])
        except Exception as e:
            log(f"HID!! 接口{h.get('interface_number')} 打不开: {e}")
            continue
        log(f"HID  接口{h['interface_number']} 已监听 "
            f"(usage_page=0x{h.get('usage_page', 0):04x}/0x{h.get('usage', 0):04x})")

        def rd(dev=dev, ifn=h["interface_number"]):
            while run_flag[0]:
                try:
                    data = dev.read(256, timeout_ms=200)
                except Exception as e:
                    log(f"HID!! 接口{ifn} 读取异常: {e}")
                    return
                if not data:
                    continue
                if len(data) <= 8:
                    log(f"KEY  接口{ifn} ID0x{data[0]:02x} len={len(data)}  {bytes(data).hex(' ')}")
                else:
                    with mouse_lock:
                        k = (ifn, data[0], len(data))
                        mouse_counts[k] = mouse_counts.get(k, 0) + 1

        threading.Thread(target=rd, daemon=True).start()


threading.Thread(target=hid_worker, daemon=True).start()

# ---------- 麦克风 ----------
devs = sd.query_devices()
idx = next(i for i, d in enumerate(devs)
           if d["max_input_channels"] > 0 and "mic device" in d["name"].lower())
sr = int(devs[idx]["default_samplerate"])
buf = []
speech_state = [False]
TH_ON, TH_OFF = 0.005, 0.002


def acb(ind, frames, t_, status):
    buf.append(ind.copy())
    rms = float(np.sqrt(np.mean(ind**2)))
    if not speech_state[0] and rms > TH_ON:
        speech_state[0] = True
        log("MIC  [语音开始]")
    elif speech_state[0] and rms < TH_OFF:
        speech_state[0] = False
        log("MIC  [语音结束]")


stream = sd.InputStream(device=idx, samplerate=sr, channels=1, dtype="float32",
                        blocksize=1024, callback=acb)
stream.start()
log(f"MIC  设备[{idx}] {devs[idx]['name']} @{sr}Hz 已开录")


def ticker():
    last_active = None
    while run_flag[0]:
        time.sleep(5)
        if not buf:
            continue
        with mouse_lock:
            mc = dict(mouse_counts)
            mouse_counts.clear()
        recent = np.concatenate(buf[-10:])
        asleep = float(np.abs(recent).max()) == 0.0
        state = "休眠(全零)" if asleep else "活跃"
        if state != last_active:
            log(f"TICK 语音链路{state}  近5s响度={float(np.sqrt(np.mean(recent**2))):.5f}"
                + (f"  鼠标包{mc}" if mc else ""))
            last_active = state


threading.Thread(target=ticker, daemon=True).start()

# ---------- 主循环 ----------
for step in range(DUR):
    time.sleep(1)
    if not run_flag[0]:
        break
run_flag[0] = False
time.sleep(0.5)
stream.stop()
stream.close()

if buf:
    audio = np.concatenate(buf)
    mono = audio[:, 0]
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    with wave.open("live.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    log(f"== 结束, 已存 live.wav ({len(mono) / sr:.1f}s) ==")
LOG.close()
