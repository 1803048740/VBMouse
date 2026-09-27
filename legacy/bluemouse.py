"""BlueMouse v0.2: 遥控器语音键 PTT → 本地 Whisper 识别 → 粘贴到焦点窗口。

- 语音键: 按一下 -> 说话 -> 停顿自动结束 -> 文字粘贴到焦点窗口
- 自定义键: keymap.json 里绑定 (text / key / cmd 三种动作)
- 未知按键: 日志会显示键码, 可直接抄进 keymap.json 绑定
- 控制台命令: k=30秒全通道按键诊断, q=退出
"""
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import pyperclip
import sounddevice as sd
import winsound
from pynput.keyboard import Controller as KbController, Key, KeyCode

DONGLE_VID, DONGLE_PID = 0x1915, 0x1025
VOICE_USAGE = 0x00CF
KEYMAP_FILE = "keymap.json"
MAX_SEC = 15.0
WAIT_SPEECH = 8.0
SILENCE_END = 1.0
MIN_SPEECH = 0.3

MIC_IDX = [None]
MIC_SR = [None]
voice_key_evt = threading.Event()
_model = [None]
kbc = KbController()
keymap = {}
diag_until = [0.0]          # 诊断窗口截止时间
diag_syskeys = []
DEFAULT_KEYMAP = {
    "_说明": "键码来自运行日志里的 [KEY] 行; action 可选 text(粘贴文本)/key(发送按键组合)/cmd(运行命令)",
    "0x0196": {"action": "text", "value": "（打开 keymap.json 配置我的动作）"},
    "0x00E9": {"action": "key", "value": "volumeup", "_注释": "示例: 覆盖默认音量键"},
}


LOGF = open("service.log", "a", encoding="utf-8")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOGF.write(line + "\n")
        LOGF.flush()
    except Exception:
        pass


def find_mic():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "mic device" in d["name"].lower():
            MIC_IDX[0], MIC_SR[0] = i, int(d["default_samplerate"])
            return True
    return False


def load_keymap():
    global keymap
    if not os.path.exists(KEYMAP_FILE):
        with open(KEYMAP_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_KEYMAP, f, ensure_ascii=False, indent=2)
    try:
        with open(KEYMAP_FILE, "r", encoding="utf-8") as f:
            keymap = {k.lower(): v for k, v in json.load(f).items()
                      if not k.startswith("_")}
        log(f"keymap.json 已加载: {list(keymap.keys())}")
    except Exception as e:
        log(f"keymap.json 加载失败: {e}")
        keymap = {}


def reload_keymap():
    load_keymap()
    log("keymap 已重载")


# ---------------- 动作 ----------------
def send_combo(combo):
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    mods = {"ctrl": Key.ctrl, "alt": Key.alt, "shift": Key.shift,
            "win": Key.cmd, "esc": Key.esc, "enter": Key.enter,
            "tab": Key.tab, "space": Key.space, "backspace": Key.backspace,
            "volumeup": Key.media_volume_up, "volumedown": Key.media_volume_down}
    keys = []
    for p in parts:
        if p in mods:
            keys.append(mods[p])
        elif len(p) == 1:
            keys.append(KeyCode.from_char(p))
        else:
            keys.append(KeyCode(0, {
                "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
                "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
                "f11": 0x7A, "f12": 0x7B}.get(p, 0), 0))
    held = []
    for k in keys:
        kbc.press(k)
        held.append(k)
    for k in reversed(held):
        kbc.release(k)


def run_action(cfg, usage_hex):
    if not isinstance(cfg, dict) or "action" not in cfg:
        log(f"[KEY] {usage_hex} 未配置有效动作")
        return
    act, val = cfg["action"], cfg.get("value", "")
    log(f"[KEY] {usage_hex} -> {act}: {val}")
    try:
        if act == "text":
            pyperclip.copy(val)
            kbc.press(Key.ctrl); kbc.press("v")
            kbc.release("v"); kbc.release(Key.ctrl)
        elif act == "key":
            send_combo(val)
        elif act == "cmd":
            subprocess.Popen(val, shell=True)
    except Exception as e:
        log(f"[!!] 动作执行失败: {e}")


# ---------------- STT ----------------
def get_model():
    if _model[0] is None:
        log("STT 加载 faster-whisper small ...")
        from faster_whisper import WhisperModel
        _model[0] = WhisperModel("small", device="cpu", compute_type="int8")
        log("STT 模型就绪")
    return _model[0]


def transcribe(mono, sr):
    n16 = int(len(mono) * 16000 / sr)
    x = np.linspace(0, 1, len(mono), endpoint=False)
    xi = np.linspace(0, 1, n16, endpoint=False)
    p16 = np.interp(xi, x, mono).astype(np.float32)
    p16 = p16 / (float(np.abs(p16).max()) or 1.0) * 0.9
    segs, _ = get_model().transcribe(p16, language="zh", beam_size=5,
                                     condition_on_previous_text=False,
                                     initial_prompt="以下是普通话口述的编程指令。")
    return "".join(s.text for s in segs).strip()


# ---------------- 录音 ----------------
def record_utterance():
    q = queue.Queue()
    started = time.time()

    def cb(ind, f, t_, st):
        q.put(ind[:, 0].copy())

    with sd.InputStream(device=MIC_IDX[0], samplerate=MIC_SR[0], channels=1,
                        dtype="float32", blocksize=1024, callback=cb):
        blocks, baseline = [], []
        state, speech_run, silence_run = "wait", 0.0, 0.0
        th_on = th_off = None
        dt = 1024 / MIC_SR[0]
        while time.time() - started < MAX_SEC:
            try:
                blk = q.get(timeout=0.5)
            except queue.Empty:
                continue
            t = time.time() - started
            rms = float(np.sqrt(np.mean(blk ** 2)))
            if t < 0.15:
                continue
            if t < 0.5:
                baseline.append(rms)
                continue
            if th_on is None:
                base = float(np.median(baseline)) or 0.001
                th_on = max(0.015, 5 * base)
                th_off = th_on * 0.4
            if state == "wait":
                blocks.append(blk)
                if rms > th_on:
                    state = "in"
                    log("  [录音] 检测到说话...")
                elif time.time() - started > 0.5 + WAIT_SPEECH:
                    log("  [录音] 没等到说话, 放弃")
                    return None
            else:
                blocks.append(blk)
                if rms > th_off:
                    speech_run += dt
                    silence_run = 0.0
                else:
                    silence_run += dt
                if speech_run >= MIN_SPEECH and silence_run >= SILENCE_END:
                    break
    mono = np.concatenate(blocks) if blocks else np.zeros(1, np.float32)
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    with wave.open("last_utterance.wav", "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_SR[0])
        w.writeframes(pcm.tobytes())
    return mono


# ---------------- 线程 ----------------
def ptt_loop():
    get_model()
    log("PTT 就绪: 按语音键 -> 说话 -> 自动粘贴")
    while True:
        voice_key_evt.wait()
        voice_key_evt.clear()
        log("[PTT] 语音键触发, 等你开口...")
        try:
            winsound.Beep(1200, 70)
        except Exception:
            pass
        mono = record_utterance()
        if mono is None or float(np.abs(mono).max()) < 1e-4:
            continue
        try:
            winsound.Beep(800, 70)
        except Exception:
            pass
        text = transcribe(mono, MIC_SR[0])
        if not text:
            log("[STT] (没听清)")
            continue
        log(f"[STT] {text}")
        try:
            pyperclip.copy(text)
            kbc.press(Key.ctrl); kbc.press("v")
            kbc.release("v"); kbc.release(Key.ctrl)
            log("[ -> ] 已粘贴到焦点窗口, 按 OK/回车 发送")
        except Exception as e:
            log(f"[!!] 粘贴失败: {e}")


def consumer_read(dev):
    while True:
        try:
            data = dev.read(512, timeout_ms=300)
        except Exception:
            return
        if not data:
            continue
        if len(data) >= 3 and data[0] == 0x02:
            usage = data[1] | (data[2] << 8)
            if usage == VOICE_USAGE:
                voice_key_evt.set()
                continue
            if usage:
                hx = f"0x{usage:04X}"
                if time.time() < diag_until[0]:
                    log(f"[DIAG] 消费键 {hx}  ({bytes(data).hex(' ')})")
                cfg = keymap.get(hx.lower())
                if cfg:
                    run_action(cfg, hx)
                else:
                    log(f"[KEY] 未绑定键 {hx}  ({bytes(data).hex(' ')}) "
                        f"(可写入 keymap.json 绑定)")
        else:
            if time.time() < diag_until[0]:
                log(f"[DIAG] 消费通道原始 {bytes(data).hex(' ')}")


def vendor_read(dev):
    while True:
        try:
            data = dev.read(512, timeout_ms=300)
        except Exception:
            return
        if data and time.time() < diag_until[0]:
            log(f"[DIAG] 厂商通道 len={len(data)} {bytes(data).hex(' ')}")


def hid_loop():
    import hid
    while True:
        try:
            infos = hid.enumerate(DONGLE_VID, DONGLE_PID)
            if not infos:
                time.sleep(2)
                continue
            threads = []
            for h in infos:
                up = h.get("usage_page", 0)
                if up == 0x0C:
                    target = consumer_read
                elif up == 0xFF01:
                    target = vendor_read
                else:
                    continue
                try:
                    dev = hid.device()
                    dev.open_path(h["path"])
                except Exception:
                    continue
                threads.append(threading.Thread(target=target, args=(dev,), daemon=True))
                threads[-1].start()
            while threads and any(t.is_alive() for t in threads):
                time.sleep(1)
            time.sleep(1)
        except Exception as e:
            log(f"[HID] 异常重启: {e}")
            time.sleep(2)


def diag_syskey_loop():
    from pynput import keyboard

    def on_press(key):
        if time.time() < diag_until[0]:
            diag_syskeys.append(str(key))
            log(f"[DIAG] 系统键 {key}")

    kl = keyboard.Listener(on_press=on_press)
    kl.start()
    return kl


# ---------------- 控制台 ----------------
def console():
    kl = None
    while True:
        line = sys.stdin.readline()
        if not line:  # EOF: 无交互控制台, 关闭命令模式但服务继续
            log("控制台输入不可用, 命令模式关闭 (服务继续运行)")
            while True:
                time.sleep(3600)
        cmd = line.strip().lower()
        if cmd == "q":
            log("退出")
            os._exit(0)
        elif cmd == "k":
            log("[DIAG] 30秒全通道诊断开始: 请交替按那个'没反应'的键和语音键各3次")
            diag_syskeys.clear()
            diag_until[0] = time.time() + 30
            if kl is None:
                kl = diag_syskey_loop()
            threading.Thread(target=lambda: _diag_end(kl), daemon=True).start()
        elif cmd == "r":
            reload_keymap()


def _diag_end(kl):
    while time.time() < diag_until[0]:
        time.sleep(1)
    log(f"[DIAG] 结束. 系统键捕获: {diag_syskeys}")
    log("[DIAG] 若'没反应'的键在上面任何 DIAG 行都没出现, 它的事件在系统层被吞掉了(如电源键)")


# ---------------- main ----------------
def main():
    print("=" * 56, flush=True)
    print(" BlueMouse v0.2 - 遥控器语音输入 for vibe coding", flush=True)
    print(" 语音键=说话输入 | 自定义键见 keymap.json | 控制台: k=诊断 q=退出 r=重载配置", flush=True)
    print("=" * 56, flush=True)
    if not find_mic():
        log("!! 没找到遥控器麦克风(Mic Device), 请确认接收器已插好")
        sys.exit(1)
    log(f"麦克风[{MIC_IDX[0]}] @{MIC_SR[0]}Hz | 接收器监听中")
    load_keymap()
    threading.Thread(target=hid_loop, daemon=True).start()
    threading.Thread(target=ptt_loop, daemon=True).start()
    console()


if __name__ == "__main__":
    main()
