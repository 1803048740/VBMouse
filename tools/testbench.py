"""BlueMouse 遥控器可视化测试台。

一个桌面窗口, 左边测试按钮从上往下点, 按提示操作;
所有按键码/音频统计/识别文字实时显示并完整落盘 guilog.txt, 录音存 wav。
用法: python -X utf8 testbench.py
"""
import queue
import threading
import time
import wave

import numpy as np
import sounddevice as sd
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

LOGF = open("guilog.txt", "a", encoding="utf-8")
qlog: "queue.Queue[str]" = queue.Queue()
qlvl: "queue.Queue[float]" = queue.Queue()
qmsg: "queue.Queue[str]" = queue.Queue()
qtxt: "queue.Queue[str]" = queue.Queue()
stop_all = threading.Event()
voice_key_evt = threading.Event()
consumer_events = []      # (t, 名称, hex)
sys_keys = []             # (t, label)
mouse_counter = [0]
busy = [False]
MIC_IDX = [None]
MIC_SR = [None]


def glog(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    qlog.put(line)
    try:
        LOGF.write(line + "\n")
        LOGF.flush()
    except Exception:
        pass


# ---------------- 设备 ----------------
def find_mic():
    devs = sd.query_devices()
    for i, d in enumerate(devs):
        if d["max_input_channels"] > 0 and "mic device" in d["name"].lower():
            MIC_IDX[0], MIC_SR[0] = i, int(d["default_samplerate"])
            return True
    return False


# ---------------- HID 监听 ----------------
CONSUMER = {0x0030: "电源", 0x0040: "菜单", 0x0041: "选择", 0x0042: "上",
            0x0043: "下", 0x0044: "左", 0x0045: "右", 0x008B: "Menu",
            0x00CF: "语音键", 0x00B0: "播放/暂停", 0x00B3: "快进",
            0x00B4: "快退", 0x00B7: "停止", 0x00CD: "播放/暂停",
            0x00E2: "静音", 0x00E9: "音量+", 0x00EA: "音量-",
            0x0196: "未知0x0196", 0x0221: "搜索", 0x0222: "主页",
            0x0223: "返回", 0x0224: "前进"}


def hid_loop():
    import hid
    glog("HID 监听线程启动 (VID_1915/PID_1025)")
    while not stop_all.is_set():
        try:
            infos = hid.enumerate(0x1915, 0x1025)
        except Exception as e:
            glog(f"HID!! 枚举失败: {e}")
            time.sleep(2)
            continue
        if not infos:
            glog("HID!! 未发现接收器, 2秒后重试")
            time.sleep(2)
            continue
        threads = []
        for h in infos:
            ifn = h["interface_number"]
            up = h.get("usage_page", 0)
            try:
                dev = hid.device()
                dev.open_path(h["path"])
            except Exception:
                continue
            tag = {0xFF01: "厂商", 0x0C: "消费"}.get(up, "通用")
            glog(f"HID 接口{ifn}({tag}) 已监听")
            threads.append(threading.Thread(
                target=hid_read, args=(dev, ifn, tag, up), daemon=True))
            threads[-1].start()
        while not stop_all.is_set() and any(t.is_alive() for t in threads):
            time.sleep(1)
        if not stop_all.is_set():
            glog("HID 全部读取线程退出, 重新枚举")


def hid_read(dev, ifn, tag, up):
    errs = 0
    while not stop_all.is_set() and errs < 2:
        try:
            data = dev.read(512, timeout_ms=300)
        except Exception as e:
            errs += 1
            glog(f"HID!! 接口{ifn}({tag}) 读异常: {e}")
            continue
        if not data:
            continue
        if up == 0x0C:
            if len(data) >= 3 and data[0] == 0x02:
                usage = data[1] | (data[2] << 8)
                if usage:
                    name = CONSUMER.get(usage, f"未知0x{usage:04X}")
                    consumer_events.append((time.time(), name, bytes(data).hex(" ")))
                    glog(f"KEY {name}  ({bytes(data).hex(' ')})")
                    if usage == 0x00CF:
                        voice_key_evt.set()
            else:
                glog(f"KEY? 消费通道原始 {bytes(data).hex(' ')}")
        elif up == 0x01:
            if len(data) >= 2 and data[0] == 0x03:
                if data[1]:
                    consumer_events.append((time.time(), "模式/开关脉冲",
                                            bytes(data).hex(" ")))
                    glog(f"KEY 模式/开关脉冲 ({bytes(data).hex(' ')})")
            else:
                mouse_counter[0] += 1  # 鼠标等高频包只计数
        else:
            glog(f"HID[{tag}] 接口{ifn} len={len(data)} {bytes(data).hex(' ')}")


def pynput_loop():
    try:
        from pynput import keyboard

        def on_press(key):
            char = getattr(key, "char", None)
            # 隐私: 字符内容一律不记录, 只记特殊键名
            sys_keys.append((time.time(), None if char is not None else str(key)))

        keyboard.Listener(on_press=on_press).start()
        glog("SYSKEY 监听已启动(系统级键盘事件, 字符内容不记录)")
    except Exception as e:
        glog(f"SYSKEY!! 不可用: {e}")


# ---------------- 录音与识别 ----------------
def record(seconds, tag):
    if MIC_IDX[0] is None:
        glog("AUD!! 没有可用麦克风")
        return np.zeros(1024, np.float32)
    buf = []

    def cb(ind, f, t_, st):
        buf.append(ind.copy())
        qlvl.put(float(np.sqrt(np.mean(ind ** 2))))

    # 每次新开流: 重发 SET_INTERFACE, 踢活语音桥
    with sd.InputStream(device=MIC_IDX[0], samplerate=MIC_SR[0], channels=1,
                        dtype="float32", blocksize=1024, callback=cb):
        end = time.time() + seconds
        while time.time() < end and not stop_all.is_set():
            time.sleep(0.05)
    mono = np.concatenate(buf)[:, 0] if buf else np.zeros(MIC_SR[0], np.float32)
    zero = float(np.mean(np.abs(mono) < 1e-6)) * 100
    glog(f"AUD {tag}: {len(mono) / MIC_SR[0]:.1f}s 纯零{zero:.0f}% "
         f"峰值{float(np.abs(mono).max()):.4f}")
    name = f"rec_{tag}.wav"
    pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2")
    with wave.open(name, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(MIC_SR[0])
        w.writeframes(pcm.tobytes())
    return mono


_model = [None]


def get_model():
    if _model[0] is None:
        glog("STT 加载 faster-whisper small ...")
        from faster_whisper import WhisperModel
        _model[0] = WhisperModel("small", device="cpu", compute_type="int8")
        glog("STT 模型就绪")
    return _model[0]


def transcribe(mono, sr, tag):
    if float(np.abs(mono).max()) < 1e-4:
        glog(f"STT {tag}: 全静音, 跳过")
        return "(无音频)"
    n16 = int(len(mono) * 16000 / sr)
    x = np.linspace(0, 1, len(mono), endpoint=False)
    xi = np.linspace(0, 1, n16, endpoint=False)
    p16 = np.interp(xi, x, mono).astype(np.float32)
    p16 = p16 / (float(np.abs(p16).max()) or 1.0) * 0.9
    try:
        segs, _ = get_model().transcribe(p16, language="zh", beam_size=5,
                                         condition_on_previous_text=False,
                                         initial_prompt="以下是普通话口述的编程指令。")
        text = "".join(s.text for s in segs).strip()
        glog(f"STT {tag}: {text or '(空)'}")
        return text or "(没听清)"
    except Exception as e:
        glog(f"STT!! {tag}: {e}")
        return f"(识别失败: {e})"


# ---------------- 测试 ----------------
def wait(seconds):
    t0 = time.time()
    while time.time() - t0 < seconds and not stop_all.is_set():
        time.sleep(0.2)


def test_keys():
    consumer_events.clear()
    glog("==== TEST1 按键全码 开始 ====")
    qmsg.put("TEST1: 30秒内把遥控器上每个键都按一遍(包括语音键), 每键停半秒")
    wait(30)
    seen = {}
    for _, name, hx in consumer_events:
        seen[name] = hx
    glog("==== TEST1 结果 ====")
    for name, hx in seen.items():
        glog(f"  {name}: {hx}")
    qmsg.put(f"TEST1 完成: 识别到 {len(seen)} 种按键, 见日志")


def test_keyboard():
    sys_keys.clear()
    glog("==== TEST2 遥控器键盘 开始 ====")
    qmsg.put("TEST2: 请用遥控器【背面键盘】打几个字(别用电脑键盘), 20秒")
    wait(20)
    glog("==== TEST2 结果 ====")
    specials = [l for _, l in sys_keys if l is not None]
    nchar = sum(1 for _, l in sys_keys if l is None)
    glog(f"  共 {len(sys_keys)} 个键事件 (其中字符按键 {nchar} 个, 内容不记录); "
         f"特殊键名: {specials[:40]}")
    qmsg.put(f"TEST2 完成: {len(sys_keys)} 个键事件")


def test_mouse():
    mouse_counter[0] = 0
    glog("==== TEST3 飞鼠 开始 ====")
    qmsg.put("TEST3: 晃动遥控器让光标画圈, 15秒")
    wait(15)
    glog(f"==== TEST3 结果: 鼠标包 {mouse_counter[0]} ====")
    qmsg.put(f"TEST3 完成: {mouse_counter[0]} 个鼠标包")


def test_voice():
    glog("==== TEST4 近讲语音→文字 开始 ====")
    for r in (1, 2, 3):
        if stop_all.is_set():
            break
        for k in (3, 2, 1):
            qmsg.put(f"TEST4 第{r}/3轮: {k} 秒后开始说话(凑近遥控器)")
            wait(1)
        qmsg.put(f"TEST4 第{r}/3轮: 请说话! (6秒)")
        mono = record(6, f"voice_r{r}")
        qtxt.put(f"第{r}轮识别: {transcribe(mono, MIC_SR[0], f'voice_r{r}')}")
    qmsg.put("TEST4 完成")


def test_ptt():
    glog("==== TEST5 语音键PTT演示 开始 ====")
    t0 = time.time()
    rounds = 0
    while time.time() - t0 < 120 and rounds < 3 and not stop_all.is_set():
        qmsg.put("TEST5: 按一下【语音键】, 然后立刻对它说一句话(6秒)")
        voice_key_evt.clear()
        if voice_key_evt.wait(timeout=5):
            qmsg.put("语音键按下! 录音中...")
            rounds += 1
            mono = record(6, f"ptt_r{rounds}")
            qtxt.put(f"PTT第{rounds}轮: {transcribe(mono, MIC_SR[0], f'ptt_r{rounds}')}")
            time.sleep(3)
    qmsg.put("TEST5 完成")


# ---------------- GUI ----------------
root = tk.Tk()
root.title("BlueMouse 遥控器测试台")
root.geometry("880x660")
root.protocol("WM_DELETE_WINDOW", lambda: (stop_all.set(), LOGF.close(), root.destroy()))

status_var = tk.StringVar(value="初始化...")
msg_var = tk.StringVar(value="请从左侧按顺序点击测试")
txt_var = tk.StringVar(value="(识别文字将显示在这里)")

top = ttk.Frame(root, padding=6)
top.pack(fill="x")
ttk.Label(top, textvariable=status_var, foreground="#555").pack(anchor="w")
msg_label = ttk.Label(top, textvariable=msg_var, font=("Microsoft YaHei", 13, "bold"),
                      foreground="#0a58ca", wraplength=840, justify="left")
msg_label.pack(fill="x", pady=4)

left = ttk.Frame(root, padding=6)
left.pack(side="left", fill="y")
btns = [("1. 按键全码 (30s)", test_keys),
        ("2. 遥控器键盘 (20s)", test_keyboard),
        ("3. 飞鼠测试 (15s)", test_mouse),
        ("4. 近讲语音→文字 (3轮)", test_voice),
        ("5. 语音键PTT演示 (2min)", test_ptt)]


def run_test(fn):
    if busy[0]:
        messagebox.showinfo("提示", "有测试正在运行, 请等它完成")
        return
    busy[0] = True

    def w():
        try:
            fn()
        except Exception as e:
            glog(f"TEST!! 异常: {e}")
        finally:
            busy[0] = False
    threading.Thread(target=w, daemon=True).start()


for label, fn in btns:
    ttk.Button(left, text=label, width=26,
               command=lambda f=fn: run_test(f)).pack(pady=4)

right = ttk.Frame(root, padding=6)
right.pack(side="left", fill="both", expand=True)
log_box = scrolledtext.ScrolledText(right, font=("Consolas", 9), width=70, height=26)
log_box.pack(fill="both", expand=True)

bottom = ttk.Frame(root, padding=6)
bottom.pack(fill="x")
ttk.Label(bottom, text="麦克风电平:").pack(side="left")
meter = ttk.Progressbar(bottom, length=300, maximum=100)
meter.pack(side="left", padx=6)
ttk.Label(bottom, textvariable=txt_var, foreground="#b30000",
          font=("Microsoft YaHei", 11, "bold"), wraplength=420).pack(side="left", fill="x")


def poll():
    while not qlog.empty():
        line = qlog.get_nowait()
        log_box.insert("end", line + "\n")
        log_box.see("end")
    try:
        while True:
            meter["value"] = min(qlvl.get_nowait() * 400, 100)
    except queue.Empty:
        pass
    try:
        while True:
            msg_var.set(qmsg.get_nowait())
    except queue.Empty:
        pass
    try:
        while True:
            txt_var.set(qtxt.get_nowait())
    except queue.Empty:
        pass
    if not stop_all.is_set():
        root.after(100, poll)


def init():
    glog("==== 会话开始 ====")
    devs = sd.query_devices()
    mics = [f"[{i}]{d['name']}" for i, d in enumerate(devs)
            if d["max_input_channels"] > 0 and "mic device" in d["name"].lower()]
    ok = find_mic()
    glog(f"麦克风: {'/'.join(mics) or '未找到 Mic Device'} -> 使用[{MIC_IDX[0]}] @{MIC_SR[0]}Hz")
    status_var.set(f"麦克风[{MIC_IDX[0]}] @{MIC_SR[0]}Hz | 接收器监听中 (日志: guilog.txt)")
    if not ok:
        qmsg.put("!! 没找到遥控器麦克风, 请确认接收器已插好")
    threading.Thread(target=hid_loop, daemon=True).start()
    pynput_loop()
    glog("STT 模型后台预加载中(首次约10-30秒)...")
    threading.Thread(target=lambda: get_model(), daemon=True).start()


threading.Thread(target=init, daemon=True).start()
poll()
root.mainloop()
glog("==== 会话结束 ====")
