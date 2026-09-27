"""BlueMouse 遥控中心 v1.2 - 双档案 + 按键学习 + 本地/云端语音识别。

- 界面顶部切换遥控器档案(G20 款 / Apple TV 款), 各自独立绑定
- 按键学习: 按下遥控器上任意键(消费通道或系统键), 界面显示码值并可绑定/设为语音键
- 语音键: 按一下开始说话, 再按一下结束 (settings.json ptt_mode=vad 切回自动断句)
- 语音识别: 本地 faster-whisper(cpu/cuda) 或云端 API(Groq/SiliconFlow/MiniMax/火山引擎),
  界面「识别设置」里配置; keymap.json / settings.json 手工改动会自动热加载
- 麦克风: 自动打开所有 Mic Device 端点, 录音时自动选用最响的一路(支持多接收器)
"""
import base64
import io
import json
import multiprocessing as mp
import os
import queue
import subprocess
import sys
import threading
import time
import wave
import winreg

import customtkinter as ctk
import numpy as np
import pyperclip
import pystray
import sounddevice as sd
import tkinter as tk
import winsound
from PIL import Image, ImageDraw
from pynput import keyboard as pk

APP_NAME = "BlueMouse"
DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                        APP_NAME)
os.makedirs(DATA_DIR, exist_ok=True)
KEYMAP_FILE = os.path.join(DATA_DIR, "keymap.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "app.log")

DONGLE_VID, DONGLE_PID = 0x1915, 0x1025
MAX_SEC, WAIT_SPEECH, SILENCE_END, MIN_SPEECH = 15.0, 8.0, 1.0, 0.3
TOGGLE_MAX = 120.0

qlog: "queue.Queue[str]" = queue.Queue()
qlvl: "queue.Queue[float]" = queue.Queue()
qstt: "queue.Queue[str]" = queue.Queue()
qdisc: "queue.Queue[tuple]" = queue.Queue()   # (来源, 码) 发现的按键
qrefresh = queue.Queue()                       # 学习完成刷新信号
stop_all = threading.Event()
voice_key_evt = threading.Event()
learning = [None]        # {"kind": "key"/"voice", "slot": 码} 或 None
capture_buf = []
capture_active = [False]
_model = [None]
MIC_IDX, MIC_SR = [None], [None]

LOGF = open(LOG_FILE, "a", encoding="utf-8")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
        LOGF.write(line + "\n")
        LOGF.flush()
    except Exception:
        pass
    qlog.put(line)


# ---------------- 配置: 双档案 ----------------
def default_keymap_v2():
    return {
        "version": 2,
        "active": "A",
        "profiles": {
            "A": {"name": "G20 款", "voice_key": "0x00CF",
                  "keys": {"0x0196": {"action": "cmd",
                                      "value": "start https://chatglm.cn"}}},
            "B": {"name": "Apple TV 款", "voice_key": "", "keys": {}},
        },
    }


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    save_json(path, default)
    return json.loads(json.dumps(default))


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


km_data = {}
settings = {}

DEFAULT_SETTINGS = {
    "autostart": False, "warmup_model": True, "ptt_mode": "toggle",
    # 语音识别
    "stt_backend": "local",      # local=本地 Whisper / api=云端 API
    "local_model": "small",      # small / medium / large-v3 / distil-medium.en ...
    "local_device": "cpu",       # cpu / cuda (cuda 失败自动回退 cpu)
    "api_provider": "groq",      # groq / siliconflow / minimax / volc / custom
    "api_key": "",
    "api_base": "",              # 留空用服务商预设, 可覆盖
    "api_model": "",             # 留空用服务商预设, 可覆盖
    "volc_appid": "",            # 火山引擎 AppID
    "volc_token": "",            # 火山引擎 Access Token
}

# 云端服务商预设: OpenAI 兼容 /audio/transcriptions, 火山走专用适配
API_PRESETS = {
    "groq": {"label": "Groq (免费额度, whisper-large-v3-turbo)",
             "base": "https://api.groq.com/openai/v1",
             "model": "whisper-large-v3-turbo"},
    "siliconflow": {"label": "SiliconFlow 硅基流动 (SenseVoice 中文免费)",
                    "base": "https://api.siliconflow.cn/v1",
                    "model": "FunAudioLLM/SenseVoiceSmall"},
    "minimax": {"label": "MiniMax (语音识别)",
                "base": "https://api.minimaxi.com/v1",
                "model": ""},
    "volc": {"label": "火山引擎 (豆包录音文件识别极速版)",
             "base": "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
             "model": ""},
    "custom": {"label": "自定义 (OpenAI 兼容)", "base": "", "model": ""},
}

cfg_mtime = {"km": 0.0, "st": 0.0}


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def load_all():
    global km_data, settings
    raw = load_json(KEYMAP_FILE, default_keymap_v2())
    if "profiles" not in raw:   # v1 旧格式迁移
        v2 = default_keymap_v2()
        v2["profiles"]["A"]["keys"] = {k: v for k, v in raw.items()
                                       if not k.startswith("_")}
        raw = v2
        log("keymap.json 已迁移到 v2 双档案格式")
    km_data = raw
    settings = {**DEFAULT_SETTINGS, **load_json(SETTINGS_FILE, DEFAULT_SETTINGS)}
    set_autostart(settings.get("autostart", False))
    cfg_mtime["km"], cfg_mtime["st"] = _mtime(KEYMAP_FILE), _mtime(SETTINGS_FILE)


def config_changed():
    """检测 keymap.json / settings.json 是否被外部修改(含界面保存), 有则热加载。"""
    changed = []
    for tag, path in (("km", KEYMAP_FILE), ("st", SETTINGS_FILE)):
        m = _mtime(path)
        if m != cfg_mtime[tag]:
            cfg_mtime[tag] = m
            changed.append(tag)
    if changed:
        load_all()
        log("配置文件已重新加载: " + ", ".join(changed))
    return changed


def active_profile():
    aid = km_data.get("active", "A")
    return aid, km_data["profiles"].get(aid, km_data["profiles"]["A"])


def save_keymap():
    save_json(KEYMAP_FILE, km_data)
    cfg_mtime["km"] = _mtime(KEYMAP_FILE)   # 界面保存不算外部修改


def save_settings():
    save_json(SETTINGS_FILE, settings)
    cfg_mtime["st"] = _mtime(SETTINGS_FILE)


def set_autostart(enable):
    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enable:
            exe = sys.executable if getattr(sys, "frozen", False) else \
                f'"{sys.executable}" "{os.path.abspath(__file__)}"'
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, exe)
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------- 设备 ----------------
def find_mic():
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and "mic device" in d["name"].lower():
            MIC_IDX[0], MIC_SR[0] = i, int(d["default_samplerate"])
            return True
    return False


def mic_stream_list():
    """枚举所有 Mic Device 端点(同一端点的多视图去重), 支持多个接收器。"""
    streams = []
    seen = set()
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0 or "mic device" not in d["name"].lower():
            continue
        sr = int(d["default_samplerate"])
        ch = min(2, d["max_input_channels"])
        key = (d["name"], sr, ch)
        if key in seen:
            continue
        seen.add(key)
        streams.append([i, sr, ch])
    return streams


# ---------------- 动作 ----------------
def send_combo(app, combo):
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    mods = {"ctrl": pk.Key.ctrl, "alt": pk.Key.alt, "shift": pk.Key.shift,
            "win": pk.Key.cmd, "esc": pk.Key.esc, "enter": pk.Key.enter,
            "tab": pk.Key.tab, "space": pk.Key.space,
            "backspace": pk.Key.backspace, "volumeup": pk.Key.media_volume_up,
            "volumedown": pk.Key.media_volume_down}
    fn = {"f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
          "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
          "f11": 0x7A, "f12": 0x7B}
    keys = [mods.get(p) or (pk.KeyCode.from_char(p) if len(p) == 1
            else pk.KeyCode(0, fn.get(p, 0), 0)) for p in parts]
    held = []
    for k in keys:
        app.kbc.press(k)
        held.append(k)
    for k in reversed(held):
        app.kbc.release(k)


def run_action(app, cfg, usage_hex):
    if not isinstance(cfg, dict) or "action" not in cfg:
        return
    act, val = cfg["action"], cfg.get("value", "")
    log(f"[KEY] {usage_hex} -> {act}: {val}")
    try:
        if act == "text":
            pyperclip.copy(val)
            app.kbc.press(pk.Key.ctrl); app.kbc.press("v")
            app.kbc.release("v"); app.kbc.release(pk.Key.ctrl)
        elif act == "key":
            send_combo(app, val)
        elif act == "cmd":
            subprocess.Popen(val, shell=True)
        elif act == "url":
            os.startfile(val)
    except Exception as e:
        log(f"[!!] 动作失败: {e}")


# ---------------- 按键分发(双通道 + 学习) ----------------
def handle_code(app, code):
    """统一的按键入口: code 形如 '0x0196' 或 'menu'。"""
    if learning[0] is not None:
        kind, slot = learning[0]["kind"], learning[0]["slot"]
        aid, prof = active_profile()
        if kind == "voice":
            prof["voice_key"] = code
            learning[0] = None
            save_keymap()
            qrefresh.put(("voice", code))
            log(f"[学习] 档案{aid} 语音键 <- {code}")
        else:
            prof.setdefault("learned", {})[slot] = code
            prof["keys"].setdefault(code, {"action": "none"})
            learning[0] = None
            save_keymap()
            qrefresh.put(("learned", slot, code))
            log(f"[学习] 档案{aid} {slot} <- {code}")
        return
    if code == prof_voice_key():
        voice_key_evt.set()
        return
    cfg = active_keys().get(code)
    if cfg:
        run_action(app, cfg, code)
    else:
        log(f"[KEY] 未绑定键 {code}")
        qdisc.put(code)


def prof_voice_key():
    return active_profile()[1].get("voice_key", "") or ""


def active_keys():
    return active_profile()[1].setdefault("keys", {})


def consumer_read(app, dev):
    while not stop_all.is_set():
        try:
            data = dev.read(512, timeout_ms=300)
        except Exception:
            return
        if not data:
            continue
        if len(data) >= 3 and data[0] == 0x02:
            usage = data[1] | (data[2] << 8)
            if usage:
                handle_code(app, f"0x{usage:04X}")
        elif data[0] == 0x03 and data[1]:
            handle_code(app, "mode")


def hid_loop(app):
    import hid
    while not stop_all.is_set():
        try:
            infos = hid.enumerate(DONGLE_VID, DONGLE_PID)
            if not infos:
                time.sleep(2)
                continue
            threads = []
            for h in infos:
                if h.get("usage_page", 0) != 0x0C:
                    continue
                try:
                    dev = hid.device()
                    dev.open_path(h["path"])
                except Exception:
                    continue
                threads.append(threading.Thread(
                    target=consumer_read, args=(app, dev), daemon=True))
                threads[-1].start()
            while threads and any(t.is_alive() for t in threads):
                time.sleep(1)
            time.sleep(1)
        except Exception as e:
            log(f"[HID] 异常重启: {e}")
            time.sleep(2)


def syskey_loop(app):
    """系统键监听: 处理绑定/学习中的键名, 其余一概忽略, 不记录内容。"""

    def norm(key):
        return str(key).replace("_l", "").replace("_r", "").replace("Key.", "")

    mods = {}

    def on_press(key):
        if isinstance(key, pk.Key):
            name = norm(key)
            is_mod = name in ("ctrl", "alt", "shift", "cmd")
        else:
            name = getattr(key, "char", None) or ""
            is_mod = False
        if capture_active[0]:
            if is_mod:
                mods[name] = True
                return
            combo = "+".join(list(mods) + [name.lower()])
            capture_buf.append(combo)
            mods.clear()
            return
        if not name:
            return
        if learning[0] is not None:
            handle_code(app, name)
            return
        if name in active_keys():
            run_action(app, active_keys()[name], name)
        elif name == prof_voice_key():
            voice_key_evt.set()

    def on_release(key):
        if capture_active[0] and isinstance(key, pk.Key):
            mods.pop(norm(key), None)

    try:
        pk.Listener(on_press=on_press, on_release=on_release).start()
        log("系统键监听已启动")
    except Exception as e:
        log(f"系统键监听不可用: {e}")


# ---------------- STT (独立进程, 不阻塞界面) ----------------
def wav_bytes(mono, sr):
    """float32 单声道 -> 16bit PCM wav 字节(云端上传用)。"""
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def api_transcribe(mono, sr, cfg):
    """云端识别入口: OpenAI 兼容服务商 + 火山引擎专用适配。"""
    import requests
    prov = cfg.get("api_provider") or "groq"
    preset = API_PRESETS.get(prov) or API_PRESETS["custom"]
    if prov == "volc":
        return volc_transcribe(mono, sr, cfg)
    base = (cfg.get("api_base") or preset["base"]).rstrip("/")
    if not base:
        raise RuntimeError("未配置 API 地址")
    model = (cfg.get("api_model") or preset.get("model") or "").strip()
    key = (cfg.get("api_key") or "").strip()
    if not key:
        raise RuntimeError(f"{preset['label']}: 未填 API Key")
    r = requests.post(
        base + "/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("utterance.wav", wav_bytes(mono, sr), "audio/wav")},
        data={"model": model} if model else {},
        timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"{preset['label']} HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    text = j.get("text") or (j.get("result") or {}).get("text") or ""
    return text.strip()


def volc_transcribe(mono, sr, cfg):
    """火山引擎大模型录音文件识别极速版(同步, base64 wav 直传)。"""
    import requests
    appid = (cfg.get("volc_appid") or "").strip()
    token = (cfg.get("volc_token") or "").strip()
    if not appid or not token:
        raise RuntimeError("火山引擎: 未填 AppID / Access Token")
    url = cfg.get("api_base") or API_PRESETS["volc"]["base"]
    r = requests.post(url, timeout=60, json={
        "user": {"uid": APP_NAME},
        "audio": {"format": "wav",
                  "data": base64.b64encode(wav_bytes(mono, sr)).decode()},
        "request": {"model_name": "bigmodel", "enable_punc": True},
    }, headers={"X-Api-App-Key": appid,
                "X-Api-Access-Key": token,
                "X-Api-Resource-Id": "volc.bigasr.auc.duration"})
    if r.status_code != 200:
        raise RuntimeError(f"火山引擎 HTTP {r.status_code}: {r.text[:200]}")
    j = r.json()
    text = (j.get("result") or {}).get("text") or ""
    return text.strip()


def stt_worker(task_q, result_q):
    """识别工作进程: 本地模型按配置懒加载/热切换, 云端直接调 API。"""
    model = [None, None]     # [WhisperModel, 指纹(模型,设备)]

    def get_local(cfg):
        fp = (cfg.get("local_model") or "small", cfg.get("local_device") or "cpu")
        if model[0] is not None and model[1] == fp:
            return model[0]
        if model[0] is not None:
            del model[0]
        from faster_whisper import WhisperModel
        dev, ct = fp[1], "int8"
        if dev == "cuda":
            ct = "float16"
            try:
                model[0] = WhisperModel(fp[0], device="cuda", compute_type=ct)
                model[1] = fp
                return model[0]
            except Exception:
                dev = "cpu"   # 无 N 卡/驱动异常 -> 自动回退 CPU
        model[0] = WhisperModel(fp[0], device=dev, compute_type=ct)
        model[1] = (fp[0], dev)
        return model[0]

    def transcribe_local(cfg, mono, sr):
        n16 = int(len(mono) * 16000 / sr)
        x = np.linspace(0, 1, len(mono), endpoint=False)
        xi = np.linspace(0, 1, n16, endpoint=False)
        p16 = np.interp(xi, x, mono).astype(np.float32)
        p16 = p16 / (float(np.abs(p16).max()) or 1.0) * 0.9
        segs, _ = get_local(cfg).transcribe(
            p16, language="zh", beam_size=5,
            condition_on_previous_text=False,
            initial_prompt="以下是普通话口述的编程指令。")
        return "".join(s.text for s in segs).strip()

    result_q.put({"event": "ready"})
    while True:
        item = task_q.get()
        if item is None:
            break
        try:
            cfg = item.get("cfg", {})
            if item.get("warmup"):
                if cfg.get("stt_backend", "local") == "local":
                    get_local(cfg)
                result_q.put({"event": "ready"})
                continue
            mono = np.frombuffer(item["audio"], dtype=np.float32)
            if cfg.get("stt_backend", "local") == "api":
                text = api_transcribe(mono, item["sr"], cfg)
            else:
                text = transcribe_local(cfg, mono, item["sr"])
            result_q.put({"event": "text", "text": text})
        except Exception as e:
            result_q.put({"event": "error", "text": str(e)})


_stt = {"task": None, "result": None, "started": False}


def ensure_stt():
    """按需启动识别工作进程。"""
    if _stt["started"]:
        return
    _stt["started"] = True
    ctx = mp.get_context("spawn")
    _stt["task"] = ctx.Queue()
    _stt["result"] = ctx.Queue()
    ctx.Process(target=stt_worker, args=(_stt["task"], _stt["result"]),
                daemon=True).start()
    log("STT 工作进程已启动(独立进程, 不阻塞界面)")


def stt_request(mono, sr, timeout=180.0):
    ensure_stt()
    _stt["task"].put({"audio": mono.tobytes(), "sr": sr, "cfg": dict(settings)})
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            res = _stt["result"].get(timeout=1.0)
        except queue.Empty:
            continue
        if res.get("event") == "ready":
            continue          # 懒加载场景: 先收到就绪标记
        if res.get("event") == "error":
            log(f"[STT!!] {res['text']}")
            return ""
        return res.get("text", "")
    log("[STT!!] 识别超时")
    return ""


# ---------------- 录音(多端点自动选路) ----------------
def save_wav(mono, sr):
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    with wave.open(os.path.join(DATA_DIR, "last_utterance.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


class MicCapture:
    """同时打开所有 Mic Device 端点录音, 结束后取最响的一路。"""

    def __enter__(self):
        self.handles = []      # [stream, sr, buf]
        self.latest = []
        seen = set()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] <= 0 or "mic device" not in d["name"].lower():
                continue
            sr = int(d["default_samplerate"])
            ch = min(2, d["max_input_channels"])
            key = (d["name"], sr, ch)
            if key in seen:
                continue
            seen.add(key)
            buf = []
            idx = len(self.handles)

            def cb(ind, f, t, st, buf=buf, idx=idx):
                buf.append(ind[:, 0].copy())
                r = float(np.sqrt(np.mean(ind ** 2)))
                self.latest[idx] = r
                qlvl.put(min(r * 8, 1.0))

            try:
                s = sd.InputStream(device=i, samplerate=sr, channels=ch,
                                   dtype="float32", blocksize=1024, callback=cb)
                s.start()
                self.handles.append([s, sr, buf])
                self.latest.append(0.0)
            except Exception:
                continue
        if not self.handles:
            log("[MIC] 没有可用的遥控器麦克风端点")
        return self

    def __exit__(self, *a):
        for s, *_ in self.handles:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    def live_rms(self):
        return max(self.latest) if self.latest else 0.0

    def best_mono(self):
        best, best_peak, best_sr = None, 0.0, 16000
        for s, sr, buf in self.handles:
            if not buf:
                continue
            mono = np.concatenate(buf)
            peak = float(np.abs(mono).max())
            if peak > best_peak:
                best, best_peak, best_sr = mono, peak, sr
        if best is not None:
            save_wav(best, best_sr)
        return best, best_sr


def trim_silence(mono, sr, th=0.004):
    hop = sr // 10
    n = len(mono) // hop
    frms = np.array([float(np.sqrt(np.mean(mono[i * hop:(i + 1) * hop] ** 2)))
                     for i in range(n)])
    idx = np.where(frms > th)[0]
    if len(idx) == 0:
        return None
    a = max(0, int(idx[0]) - 2) * hop
    b = min(n, int(idx[-1]) + 3) * hop
    return mono[a:b]


def record_toggle(cap, max_sec=TOGGLE_MAX):
    started = time.time()
    while time.time() - started < max_sec:
        if voice_key_evt.is_set():
            voice_key_evt.clear()
            log("  [录音] 再按语音键, 结束")
            break
        time.sleep(0.08)
    return cap.best_mono()


def record_vad(cap):
    started = time.time()
    state, speech_run, silence_run = "wait", 0.0, 0.0
    baseline = []
    th_on = th_off = None
    while time.time() - started < MAX_SEC:
        t = time.time() - started
        rms = cap.live_rms()
        if t < 0.15:
            time.sleep(0.05)
            continue
        if t < 0.5:
            baseline.append(rms)
        elif th_on is None:
            base = float(np.median(baseline)) or 0.001
            th_on = max(0.015, 5 * base)
            th_off = th_on * 0.4
            state = "wait"
        elif state == "wait":
            if rms > th_on:
                state = "in"
                log("  [录音] 检测到说话...")
            elif t > 0.5 + WAIT_SPEECH:
                return None, None
        else:
            if rms > th_off:
                speech_run += 0.05
                silence_run = 0.0
            else:
                silence_run += 0.05
            if speech_run >= MIN_SPEECH and silence_run >= SILENCE_END:
                break
        time.sleep(0.05)
    return cap.best_mono()


# ---------------- 后台线程 ----------------
def ptt_loop(app):
    if settings.get("stt_backend", "local") == "local" \
            and settings.get("warmup_model", True):
        ensure_stt()
        _stt["task"].put({"warmup": True, "cfg": dict(settings)})
    log("语音输入就绪 (toggle: 按一下开始/再按一下结束)")
    while not stop_all.is_set():
        voice_key_evt.wait()
        if stop_all.is_set():
            return
        voice_key_evt.clear()
        mode = settings.get("ptt_mode", "toggle")
        log("[PTT] 语音键触发" +
            (" (录音中, 再按一次结束)" if mode == "toggle" else ""))
        try:
            winsound.Beep(1200, 70)
        except Exception:
            pass
        with MicCapture() as cap:
            if mode == "toggle":
                mono, sr = record_toggle(cap)
            else:
                mono, sr = record_vad(cap)
        if mono is None or float(np.abs(mono).max()) < 1e-4:
            continue
        if mode == "toggle":
            mono = trim_silence(mono, sr)
            if mono is None:
                log("[PTT] 全程静音, 丢弃")
                continue
        try:
            winsound.Beep(800, 70)
        except Exception:
            pass
        text = stt_request(mono, sr)
        if not text:
            log("[STT] (没听清)")
            continue
        log(f"[STT] {text}")
        qstt.put(text)
        try:
            pyperclip.copy(text)
            app.kbc.press(pk.Key.ctrl); app.kbc.press("v")
            app.kbc.release("v"); app.kbc.release(pk.Key.ctrl)
        except Exception as e:
            log(f"[!!] 粘贴失败: {e}")


# ---------------- 托盘 ----------------
def make_icon_image():
    img = Image.new("RGB", (64, 64), (26, 26, 32))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=(90, 74, 134), outline=(138, 180, 248), width=2)
    d.rounded_rectangle((26, 14, 38, 34), radius=6, fill=(232, 232, 240))
    d.rectangle((29, 34, 35, 41), fill=(232, 232, 240))
    d.arc((21, 28, 43, 48), 20, 160, fill=(232, 232, 240), width=3)
    return img


def setup_tray(app):
    icon = pystray.Icon(APP_NAME, make_icon_image(),
                        f"{APP_NAME} 遥控中心 - 语音输入运行中")

    def show():
        app.show_req[0] = True

    def quit_app():
        log("托盘退出")
        try:
            LOGF.close()
        except Exception:
            pass
        icon.stop()
        os._exit(0)

    icon.menu = pystray.Menu(
        pystray.MenuItem("显示界面", show, default=True),
        pystray.MenuItem("退出", quit_app),
    )
    threading.Thread(target=icon.run, daemon=True).start()


# ---------------- GUI ----------------
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# 热区: (码, 名称, 类型, cx, cy, r) — cx/cy 为相对抠图比例, r 以宽度为基准
# 类型: voice=语音键(固定) / learn=可学习绑定 (电源/飞鼠锁/触摸板为硬件原生, 无热区)
HOTSPOT_A = [
    ("", "Del", "learn", 0.575, 0.154, 0.085),
    ("0x00E2", "静音", "learn", 0.825, 0.154, 0.085),
    ("", "主页", "learn", 0.373, 0.223, 0.10),
    ("menu", "菜单", "learn", 0.575, 0.223, 0.085),
    ("0x0196", "浏览器", "learn", 0.789, 0.223, 0.10),
    ("", "上", "learn", 0.575, 0.307, 0.075),
    ("", "左", "learn", 0.373, 0.374, 0.075),
    ("", "OK", "learn", 0.575, 0.374, 0.10),
    ("", "右", "learn", 0.781, 0.374, 0.075),
    ("", "下", "learn", 0.575, 0.440, 0.075),
    ("", "PG+", "learn", 0.342, 0.500, 0.085),
    ("voice", "语音", "voice", 0.575, 0.545, 0.09),
    ("0x00E9", "V+", "learn", 0.811, 0.500, 0.085),
    ("", "PG-", "learn", 0.342, 0.588, 0.085),
    ("0x00EA", "V-", "learn", 0.811, 0.588, 0.085),
]
HOTSPOT_B = [
    ("", "盘上", "learn", 0.483, 0.135, 0.13),
    ("", "盘左", "learn", 0.225, 0.216, 0.13),
    ("", "盘右", "learn", 0.742, 0.216, 0.13),
    ("", "盘下", "learn", 0.483, 0.306, 0.13),
    ("", "盘OK", "learn", 0.483, 0.216, 0.11),
    ("", "返回", "learn", 0.296, 0.372, 0.085),
    ("voice", "语音", "voice", 0.671, 0.372, 0.085),
    ("", "静音", "learn", 0.308, 0.494, 0.095),
    ("", "主页", "learn", 0.671, 0.484, 0.08),
    ("", "飞鼠键", "learn", 0.308, 0.604, 0.095),
    ("", "菜单", "learn", 0.671, 0.601, 0.08),
]
# 注: Apple TV 款长条键下半段实为飞鼠开关(按下激活六轴陀螺仪, 原生功能)
ACTION_LABELS = [("none", "无(保持原生行为)"), ("text", "粘贴文本"),
                 ("key", "发送按键组合"), ("cmd", "运行命令"),
                 ("url", "打开网址")]


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} 遥控中心 v1.2")
        self.geometry("980x700")
        self.minsize(920, 640)
        self.kbc = pk.Controller()
        self.selected = None
        self.show_req = [False]
        self.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        setup_tray(self)

        header = ctk.CTkFrame(self, height=52, corner_radius=0)
        header.pack(fill="x")
        ctk.CTkLabel(header, text=f"{APP_NAME} 遥控中心",
                     font=("Microsoft YaHei", 18, "bold")).pack(side="left", padx=16)
        self.status_lbl = ctk.CTkLabel(header, text="初始化...", text_color="#8ab4f8")
        self.status_lbl.pack(side="right", padx=16)

        body = ctk.CTkFrame(self, corner_radius=0)
        body.pack(fill="both", expand=True)

        left = ctk.CTkFrame(body, width=330, corner_radius=0)
        left.pack(side="left", fill="y", padx=8, pady=6)
        aid, prof = active_profile()
        names = [p["name"] for p in km_data["profiles"].values()]
        self.profile_var = ctk.StringVar(value=prof["name"])
        ctk.CTkSegmentedButton(left, values=names,
                               command=self.switch_profile,
                               variable=self.profile_var).pack(pady=(2, 4))
        self.canvas = tk.Canvas(left, width=300, height=560, bg="#1a1a20",
                                highlightthickness=0)
        self.canvas.pack(pady=2)

        right = ctk.CTkFrame(body, corner_radius=0)
        right.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        self.panel_title = ctk.CTkLabel(right, text="← 点击左侧遥控器按键进行绑定/学习",
                                        font=("Microsoft YaHei", 15, "bold"))
        self.panel_title.pack(pady=(4, 2), anchor="w", padx=10)
        self.panel_hint = ctk.CTkLabel(right, text="", justify="left",
                                       text_color="#9aa0a6", wraplength=520)
        self.panel_hint.pack(anchor="w", padx=10)
        self.bind_frame = ctk.CTkScrollableFrame(right, height=300)
        self.bind_frame.pack(fill="both", expand=True, padx=10, pady=6)
        ctk.CTkLabel(right, text="运行中发现的按键 (点按钮去绑定):", anchor="w",
                     text_color="#9aa0a6").pack(anchor="w", padx=10)
        self.discover_frame = ctk.CTkFrame(right, height=70)
        self.discover_frame.pack(fill="x", padx=10, pady=(2, 6))
        self.discover_lbl = ctk.CTkLabel(self.discover_frame, text="(暂无, 按遥控器试试)",
                                         anchor="w")
        self.discover_lbl.pack(anchor="w", padx=8, pady=4)

        bottom = ctk.CTkFrame(self, height=80, corner_radius=0)
        bottom.pack(fill="x", side="bottom")
        ctk.CTkLabel(bottom, text="最近识别:", font=("Microsoft YaHei", 12)).pack(
            side="left", padx=(12, 4))
        self.stt_lbl = ctk.CTkLabel(bottom, text="(还没有语音输入)", anchor="w",
                                    text_color="#b8e0ff")
        self.stt_lbl.pack(side="left", fill="x", expand=True)
        self.meter = ctk.CTkProgressBar(bottom, width=120)
        self.meter.set(0)
        self.meter.pack(side="left", padx=8)
        self.autostart_var = ctk.BooleanVar(value=settings.get("autostart", False))
        ctk.CTkSwitch(bottom, text="开机自启", command=self.toggle_autostart,
                      variable=self.autostart_var, width=90).pack(side="left", padx=6)
        self.warmup_var = ctk.BooleanVar(value=settings.get("warmup_model", True))
        ctk.CTkSwitch(bottom, text="预热模型", command=self.toggle_warmup,
                      variable=self.warmup_var, width=90).pack(side="left", padx=6)
        ctk.CTkButton(bottom, text="识别设置", width=80, fg_color="#3a3a44",
                      command=self.open_stt_settings).pack(side="left", padx=4)
        ctk.CTkButton(bottom, text="日志目录", width=80, fg_color="#3a3a44",
                      command=lambda: os.startfile(DATA_DIR)).pack(side="left", padx=4)

        self.load_remote_images()
        self.draw_remote()

    # ----- 档案切换与绘制 -----
    def switch_profile(self, name):
        for pid, prof in km_data["profiles"].items():
            if prof["name"] == name:
                km_data["active"] = pid
                break
        save_keymap()
        log(f"切换到档案: {name}")
        self.panel_title.configure(text="← 点击左侧遥控器按键进行绑定/学习")
        for w in self.bind_frame.winfo_children():
            w.destroy()
        self.draw_remote()

    def load_remote_images(self):
        self._photos = {}
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        for aid, file in (("A", "remoteA.png"), ("B", "remoteB.png")):
            p = os.path.join(base, "assets", file)
            if not os.path.exists(p):
                log(f"!! 缺少图片资源 {p}")
                continue
            img = Image.open(p).convert("RGBA")
            h = 540
            w = max(1, int(img.width * h / img.height))
            img = img.resize((w, h), Image.LANCZOS)
            b = io.BytesIO()
            img.save(b, "PNG")
            self._photos[aid] = (tk.PhotoImage(data=base64.b64encode(b.getvalue())), w, h)

    def draw_remote(self):
        c = self.canvas
        c.delete("all")
        aid, prof = active_profile()
        photo = self._photos.get(aid)
        self.hs_geom = []
        if not photo:
            c.create_text(150, 280, text="缺少遥控器图片资源\n(assets/remoteA.png)",
                          fill="#8a8a96", font=("Microsoft YaHei", 11))
            return
        img_tk, W, H = photo
        ox, oy = (300 - W) // 2, (560 - H) // 2
        c.create_image(ox, oy, image=img_tk, anchor="nw")
        if aid == "B":
            # 侧面实体音量键(在圆盘那一侧的右侧面, 画在机身右侧对应高度)
            c.create_text(262, oy + 0.115 * H, text="侧面", fill="#6c6c78",
                          font=("Microsoft YaHei", 8))
            for code, name, cy in (("0x00E9", "侧V+", 0.205), ("", "侧V-", 0.295)):
                code = prof.get("learned", {}).get(name, code)
                x, y, r = 262, oy + cy * H, 17
                c.create_oval(x - r, y - r, x + r, y + r, fill="#2f3237",
                              outline="#4a4e55", width=2, tags=(f"side_{name}",))
                c.create_text(x, y, text=name.replace("侧", ""), fill="#e8f4ff",
                              font=("Microsoft YaHei", 8, "bold"),
                              tags=(f"side_{name}",))
                self.hs_geom.append((code, name, "learn", x, y, r))
        for code, name, kind, cx, cy, r in (HOTSPOT_B if aid == "B" else HOTSPOT_A):
            if kind == "learn":
                code = prof.get("learned", {}).get(name, code)
            self.hs_geom.append((code, name, kind, ox + cx * W, oy + cy * H, r * W))
        self.hl = c.create_oval(0, 0, 0, 0, outline="#c8dcff", width=2,
                                state="hidden")
        self.hl_key = None
        c.bind("<Motion>", self.on_canvas_motion)
        c.bind("<Button-1>", self.on_canvas_click)
        c.bind("<Leave>", lambda e: (c.itemconfigure(self.hl, state="hidden"),
                                     setattr(self, "hl_key", None)))

    def hit_test(self, x, y):
        for item in reversed(self.hs_geom):
            code, name, kind, cx, cy, r = item
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                return item
        return None

    def on_canvas_motion(self, e):
        hit = self.hit_test(e.x, e.y)
        key = hit[1] if hit else None
        if key == self.hl_key:
            return
        c = self.canvas
        self.hl_key = key
        if hit is None:
            c.itemconfigure(self.hl, state="hidden")
        else:
            code, name, kind, cx, cy, r = hit
            c.coords(self.hl, cx - r, cy - r, cx + r, cy + r)
            c.itemconfigure(self.hl, state="normal")
            c.lift(self.hl)

    def on_canvas_click(self, e):
        hit = self.hit_test(e.x, e.y)
        if hit:
            code, name, kind, *_ = hit
            self.select_key(code, name, kind)

    def select_key(self, code, name, kind):
        aid, prof = active_profile()
        self.selected = (code, name, kind)
        self.panel_title.configure(text=f"{prof['name']} · {name}")
        for w in self.bind_frame.winfo_children():
            w.destroy()
        if kind == "native":
            self.panel_hint.configure(
                text="原生硬件功能(电源/飞鼠锁/触摸板), 不支持软件绑定")
            return
        if kind == "voice":
            vk = prof.get("voice_key", "")
            self.panel_hint.configure(
                text=f"语音键当前: {vk or '未学习'}。按一下开始说话, 再按一下结束。"
                     f" 点击下方学习按钮后, 按下遥控器上的语音键即可完成设定。")
            ctk.CTkButton(self.bind_frame,
                          text="🎙 学习语音键 (点击后按遥控器语音键)",
                          command=lambda: self.start_learning("voice", None)).pack(pady=10)
            return
        if not code:
            self.panel_hint.configure(
                text="这个键还没学习。点击【学习此键】后按下遥控器上的对应按键,"
                     " 程序自动捕获码值(消费通道和系统键都支持)。")
            ctk.CTkButton(self.bind_frame, text="🎓 学习此键",
                          command=lambda: self.start_learning("key", name)).pack(pady=10)
            return
        cfg = active_keys().get(code, {"action": "none", "value": ""})
        self.panel_hint.configure(text=f"码值 {code}。选择按下去执行的动作:")
        self.cur_action = ctk.StringVar(value=cfg.get("action", "none"))
        ctk.CTkLabel(self.bind_frame, text="动作类型:", anchor="w").pack(anchor="w")
        for val, lbl in ACTION_LABELS:
            ctk.CTkRadioButton(self.bind_frame, text=lbl, variable=self.cur_action,
                               value=val, command=self.refresh_hint).pack(anchor="w", pady=2)
        ctk.CTkLabel(self.bind_frame, text="动作内容:", anchor="w").pack(anchor="w", pady=(8, 2))
        self.value_box = ctk.CTkTextbox(self.bind_frame, height=70)
        self.value_box.pack(fill="x")
        self.value_box.insert("1.0", cfg.get("value", ""))
        cap_row = ctk.CTkFrame(self.bind_frame, fg_color="transparent")
        cap_row.pack(fill="x", pady=6)
        self.cap_btn = ctk.CTkButton(cap_row, text="🎧 捕获组合键", width=130,
                                     command=self.start_capture)
        self.cap_btn.pack(side="left")
        self.cap_lbl = ctk.CTkLabel(cap_row, text="", text_color="#8ab4f8")
        self.cap_lbl.pack(side="left", padx=8)
        ctk.CTkButton(self.bind_frame, text="保存绑定", command=self.save_binding).pack(pady=8)

    def refresh_hint(self):
        act = self.cur_action.get()
        self.panel_hint.configure(text={
            "none": "无动作 (保持该键的原生行为)",
            "text": "在下方填入要粘贴的文本",
            "key": "在下方填入按键组合 (如 ctrl+shift+p), 或用捕获按钮",
            "cmd": "在下方填入要运行的命令/程序路径",
            "url": "在下方填入要打开的网址",
        }.get(act, ""))

    def start_capture(self):
        capture_buf.clear()
        capture_active[0] = True
        self.cap_lbl.configure(text="请按下组合键...")
        self.cap_btn.configure(state="disabled")

        def waiter():
            t0 = time.time()
            while time.time() - t0 < 8:
                if capture_buf:
                    combo = capture_buf[0]
                    self.after(0, lambda: self.value_box.delete("1.0", "end"))
                    self.after(0, lambda: self.value_box.insert("1.0", combo))
                    self.after(0, lambda: self.cap_lbl.configure(text=f"已捕获: {combo}"))
                    break
                time.sleep(0.1)
            else:
                self.after(0, lambda: self.cap_lbl.configure(text="超时"))
            capture_active[0] = False
            self.after(0, lambda: self.cap_btn.configure(state="normal"))
        threading.Thread(target=waiter, daemon=True).start()

    def start_learning(self, kind, slot):
        learning[0] = {"kind": kind, "slot": slot}
        if kind == "voice":
            self.panel_hint.configure(text="🎙 等待中... 请按下遥控器上的【语音键】(10秒内)")
            self.panel_title.configure(text="学习语音键")

            def voice_wait():
                t0 = time.time()
                while time.time() - t0 < 10:
                    if learning[0] is None:
                        return
                    time.sleep(0.1)
                if learning[0] and learning[0]["kind"] == "voice":
                    learning[0] = None
                    self.after(0, lambda: self.panel_hint.configure(
                        text="学习超时, 未捕获到语音键", text_color="#ff7b72"))
            threading.Thread(target=voice_wait, daemon=True).start()
        else:
            self.panel_hint.configure(text=f"🎓 等待中... 请按下遥控器上的【{slot}】键")

    def save_binding(self):
        code, name, kind = self.selected
        if kind in ("native", "voice") or not code:
            return
        act = self.cur_action.get()
        keys = active_keys()
        if act == "none":
            keys.pop(code, None)
        else:
            keys[code] = {"action": act, "value": self.value_box.get("1.0", "end").strip()}
        save_keymap()
        log(f"[绑定] {name} ({code}) -> {act}")
        self.panel_hint.configure(text="✅ 已保存并即时生效", text_color="#7ee787")

    def toggle_autostart(self):
        settings["autostart"] = bool(self.autostart_var.get())
        save_settings()
        try:
            set_autostart(settings["autostart"])
            log(f"开机自启: {settings['autostart']}")
        except Exception as e:
            log(f"自启设置失败: {e}")

    def toggle_warmup(self):
        settings["warmup_model"] = bool(self.warmup_var.get())
        save_settings()
        log(f"预热模型: {settings['warmup_model']}")

    # ----- 识别设置对话框 -----
    def open_stt_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("识别设置")
        win.geometry("600x600")
        win.transient(self)
        win.after(120, win.grab_set)
        pad = {"padx": 18, "anchor": "w"}

        ctk.CTkLabel(win, text="识别后端", font=("Microsoft YaHei", 13, "bold")
                     ).pack(**pad, pady=(14, 2))
        backend_var = ctk.StringVar(value=settings.get("stt_backend", "local"))
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(**pad)
        ctk.CTkRadioButton(row, text="本地 Whisper (离线)", variable=backend_var,
                           value="local").pack(side="left", padx=(0, 18))
        ctk.CTkRadioButton(row, text="云端 API (需联网)", variable=backend_var,
                           value="api").pack(side="left")

        ctk.CTkLabel(win, text="本地模型 (cpu / 有 N 卡选 cuda, cuda 失败自动回退)",
                     font=("Microsoft YaHei", 13, "bold")).pack(**pad, pady=(12, 2))
        local_row = ctk.CTkFrame(win, fg_color="transparent")
        local_row.pack(**pad)
        model_var = ctk.StringVar(value=settings.get("local_model", "small"))
        ctk.CTkComboBox(local_row, width=200,
                        values=["small", "medium", "large-v3", "large-v3-turbo"],
                        variable=model_var).pack(side="left")
        dev_var = ctk.StringVar(value=settings.get("local_device", "cpu"))
        ctk.CTkComboBox(local_row, width=90, values=["cpu", "cuda"],
                        variable=dev_var).pack(side="left", padx=8)

        ctk.CTkLabel(win, text="云端服务商", font=("Microsoft YaHei", 13, "bold")
                     ).pack(**pad, pady=(12, 2))
        key_by_label = {p["label"]: k for k, p in API_PRESETS.items()}
        prov_var = ctk.StringVar(
            value=API_PRESETS.get(settings.get("api_provider", "groq"),
                                  API_PRESETS["groq"])["label"])
        base_var = ctk.StringVar(value=settings.get("api_base", ""))
        model_api_var = ctk.StringVar(value=settings.get("api_model", ""))

        def pick_prov(label):
            p = API_PRESETS.get(key_by_label.get(label, "custom"),
                                API_PRESETS["custom"])
            base_var.set(p["base"])
            model_api_var.set(p["model"])

        ctk.CTkOptionMenu(win, width=520, values=list(key_by_label),
                          variable=prov_var, command=pick_prov).pack(**pad, pady=2)

        ctk.CTkLabel(win, text="API Key (火山引擎不用这个, 用下面两栏)",
                     anchor="w", text_color="#9aa0a6").pack(**pad, pady=(8, 0))
        key_var = ctk.StringVar(value=settings.get("api_key", ""))
        ctk.CTkEntry(win, width=520, show="*", variable=key_var).pack(**pad, pady=2)
        ctk.CTkLabel(win, text="API 地址(留空用预设) / 模型名(留空用预设)",
                     anchor="w", text_color="#9aa0a6").pack(**pad, pady=(8, 0))
        addr_row = ctk.CTkFrame(win, fg_color="transparent")
        addr_row.pack(**pad, pady=2)
        ctk.CTkEntry(addr_row, width=300, textvariable=base_var).pack(side="left")
        ctk.CTkEntry(addr_row, width=210, placeholder_text="模型名",
                     textvariable=model_api_var).pack(side="left", padx=8)

        ctk.CTkLabel(win, text="火山引擎 AppID / Access Token (极速版录音文件识别)",
                     anchor="w", text_color="#9aa0a6").pack(**pad, pady=(8, 0))
        volc_row = ctk.CTkFrame(win, fg_color="transparent")
        volc_row.pack(**pad, pady=2)
        appid_var = ctk.StringVar(value=settings.get("volc_appid", ""))
        tok_var = ctk.StringVar(value=settings.get("volc_token", ""))
        ctk.CTkEntry(volc_row, width=240, placeholder_text="AppID",
                     textvariable=appid_var).pack(side="left")
        ctk.CTkEntry(volc_row, width=270, placeholder_text="Access Token", show="*",
                     textvariable=tok_var).pack(side="left", padx=8)

        ctk.CTkLabel(win, text="免费推荐: Groq (whisper-large-v3-turbo) 或\n"
                              "SiliconFlow (SenseVoiceSmall, 中文友好, 免费)。\n"
                              "火山引擎: 控制台开通「大模型录音文件识别极速版」后\n"
                              "在应用管理里拿 AppID + Access Token。",
                     justify="left", text_color="#9aa0a6").pack(**pad, pady=(10, 0))

        def do_save():
            settings["stt_backend"] = backend_var.get()
            settings["local_model"] = model_var.get().strip() or "small"
            settings["local_device"] = dev_var.get().strip() or "cpu"
            settings["api_provider"] = key_by_label.get(prov_var.get(), "groq")
            settings["api_key"] = key_var.get().strip()
            settings["api_base"] = base_var.get().strip()
            settings["api_model"] = model_api_var.get().strip()
            settings["volc_appid"] = appid_var.get().strip()
            settings["volc_token"] = tok_var.get().strip()
            save_settings()
            log(f"识别设置已保存: backend={settings['stt_backend']}"
                + (f", {settings['api_provider']}" if settings["stt_backend"] == "api"
                   else f", {settings['local_model']}/{settings['local_device']}"))
            self.panel_hint.configure(text="✅ 识别设置已保存, 下一句语音即生效",
                                      text_color="#7ee787")
            win.destroy()

        ctk.CTkButton(win, text="保存", width=140, command=do_save).pack(pady=14)

    def reload_config(self, km_changed):
        """配置文件被外部修改后的热加载。"""
        self.autostart_var.set(bool(settings.get("autostart", False)))
        self.warmup_var.set(bool(settings.get("warmup_model", True)))
        if km_changed:
            for w in self.bind_frame.winfo_children():
                w.destroy()
            self.panel_title.configure(text="配置已从磁盘重新加载")
            self.panel_hint.configure(text="keymap.json 被修改并已热加载, 点击左侧按键继续绑定。",
                                      text_color="#8ab4f8")
            self.profile_var.set(active_profile()[1]["name"])
            self.draw_remote()

    def hide_to_tray(self):
        self.withdraw()
        log("窗口已最小化到托盘, 语音输入继续运行")


def poll_gui(app):
    changed = config_changed()
    if changed:
        app.reload_config("km" in changed)
    if app.show_req[0]:
        app.show_req[0] = False
        app.deiconify()
        app.lift()
    try:
        while True:
            item = qrefresh.get_nowait()
            app.draw_remote()
            if item[0] == "learned":
                app.panel_hint.configure(text=f"✅ 已学习: {item[1]} = {item[2]}, "
                                              f"继续在面板里配置动作",
                                         text_color="#7ee787")
            else:
                app.panel_hint.configure(text=f"✅ 语音键已设定: {item[1]}",
                                         text_color="#7ee787")
    except queue.Empty:
        pass
    try:
        while True:
            code = qdisc.get_nowait()
            cur = app.discover_lbl.cget("text")
            cur = "" if "暂无" in cur else cur
            if code not in cur:
                app.discover_lbl.configure(
                    text=(cur + "  " if cur else "") + code, text_color="#ffb86b")
    except queue.Empty:
        pass
    try:
        while True:
            app.meter.set(qlvl.get_nowait())
    except queue.Empty:
        app.meter.set(0)
    try:
        while True:
            app.stt_lbl.configure(text=qstt.get_nowait())
    except queue.Empty:
        pass
    if not stop_all.is_set():
        app.after(120, lambda: poll_gui(app))


def main():
    load_all()
    app = App()
    sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    sys.excepthook = lambda t, v, tb: log("!! 未捕获异常:\n" +
                                          "".join(__import__("traceback").format_exception(t, v, tb)))
    app.report_callback_exception = lambda t, v, tb: log(
        "!! Tk回调异常:\n" + "".join(__import__("traceback").format_exception(t, v, tb)))
    if not find_mic():
        app.status_lbl.configure(text="❌ 未找到遥控器麦克风(接收器未插?)", text_color="#ff7b72")
        log("!! 没找到 Mic Device")
    else:
        aid, prof = active_profile()
        app.status_lbl.configure(
            text=f"🟢 服务运行中 | 当前档案: {prof['name']} | 麦克风[{MIC_IDX[0]}] @ {MIC_SR[0]}Hz")
    threading.Thread(target=hid_loop, args=(app,), daemon=True).start()
    threading.Thread(target=syskey_loop, args=(app,), daemon=True).start()
    threading.Thread(target=ptt_loop, args=(app,), daemon=True).start()
    try:
        poll_gui(app)
        app.mainloop()
    except Exception:
        import traceback
        log("!! 主循环崩溃:\n" + traceback.format_exc())
        raise
    log("==== 应用退出 ====")


if __name__ == "__main__":
    mp.freeze_support()
    main()
