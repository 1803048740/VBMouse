"""保活实验: 音频管道全零超时后自动 close/reopen, 看能否踢活语音桥。

用法: python -X utf8 probe9_keepalive.py [时长秒, 默认75]
动作: 前40秒别碰遥控器(让房间有声音, 如视频); 最后35秒拿起遥控器按住语音键说一句话。
"""
import collections
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd

DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 75
t0 = time.time()
run_flag = [True]
LOG = open("probe9.log", "w", encoding="utf-8")


def log(msg):
    line = f"[{time.time() - t0:7.2f}s] {msg}"
    print(line, flush=True)
    LOG.write(line + "\n")
    LOG.flush()


devs = sd.query_devices()
idx = next(i for i, d in enumerate(devs)
           if d["max_input_channels"] > 0 and "mic device" in d["name"].lower())
sr = int(devs[idx]["default_samplerate"])

buf = []
recent = collections.deque(maxlen=200)   # 最近块的最大幅度
last_nz = [time.time()]                  # 最后一个非零块时刻
opened_at = [time.time()]
stream = [None]


def open_stream():
    stream[0] = sd.InputStream(device=idx, samplerate=sr, channels=1, dtype="float32",
                               blocksize=1024,
                               callback=lambda ind, f, t_, st: on_block(ind))
    stream[0].start()
    opened_at[0] = time.time()
    log("AUD  流已打开")


def on_block(ind):
    buf.append(ind.copy())
    peak = float(np.abs(ind).max())
    recent.append(peak)
    if peak > 0:
        last_nz[0] = time.time()


open_stream()


def keeper():
    while run_flag[0]:
        time.sleep(1)
        alive_s = time.time() - opened_at[0]
        silent_s = time.time() - last_nz[0]
        if alive_s > 6 and silent_s > 4:
            log(f"AUD  全零 {silent_s:.0f}s -> 重启管道 (第{kicks[0] + 1}次)")
            try:
                stream[0].stop()
                stream[0].close()
            except Exception as e:
                log(f"AUD!! 关流失败: {e}")
            time.sleep(0.3)
            open_stream()
            kicks[0] += 1
            last_nz[0] = time.time()  # 防止连发


kicks = [0]
threading.Thread(target=keeper, daemon=True).start()

print(f"== probe9 开始 {DUR}s: 前40s别碰遥控器, 后35s按住语音键说一句话 ==", flush=True)
for step in range(DUR):
    time.sleep(1)
    if (step + 1) % 10 == 0:
        print(f"  ... t={step + 1}s", flush=True)
run_flag[0] = False
time.sleep(0.6)
stream[0].stop()
stream[0].close()

audio = np.concatenate(buf)
mono = audio[:, 0]
zero = float(np.mean(np.abs(mono) < 1e-6))
pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
with wave.open("probe9.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm.tobytes())
log(f"AUD  probe9.wav: {len(mono) / sr:.1f}s 纯零占比{zero * 100:.0f}% "
    f"最大幅度{float(np.abs(mono).max()):.4f} 管道重启{kicks[0]}次")
LOG.close()
print("== probe9 结束 ==", flush=True)
