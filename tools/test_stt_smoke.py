# BlueMouse v1.2 冒烟测试: 云端 STT 适配层 + 配置热加载 (不启动 GUI, 不真实联网)
import base64
import io
import json
import sys
import time
import types
import wave
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, "app")
import bluemouse_app as bm

# --- 备份真实配置, 结束后还原 ---
snap = {}
for p in (bm.KEYMAP_FILE, bm.SETTINGS_FILE):
    snap[p] = open(p, "rb").read() if __import__("os").path.exists(p) else None

try:
    # 1) wav_bytes 往返
    mono = (0.3 * np.sin(np.linspace(0, 200, 8000))).astype(np.float32)
    raw = bm.wav_bytes(mono, 16000)
    with wave.open(io.BytesIO(raw)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 \
            and w.getframerate() == 16000, "wav 参数不对"
    print("1 wav_bytes OK:", len(raw), "bytes")

    # 2) OpenAI 兼容适配 (mock requests.post)
    captured = {}

    class FakeResp:
        status_code = 200
        text = "ok"

        def json(self):
            return {"text": " 你好 世界 "}

    def fake_post(url, **kw):
        captured.update(url=url, **kw)
        return FakeResp()

    sys.modules["requests"] = SimpleNamespace(post=fake_post)
    cfg = {"api_provider": "groq", "api_key": "k-test", "api_base": "",
           "api_model": ""}
    out = bm.api_transcribe(mono, 16000, cfg)
    assert out == "你好 世界", out
    assert captured["url"] == \
        "https://api.groq.com/openai/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer k-test"
    assert captured["files"]["file"][0] == "utterance.wav"
    assert captured["data"] == {"model": "whisper-large-v3-turbo"}
    print("2 openai-compatible OK ->", out)

    # 3) 火山引擎适配 (mock)
    captured.clear()

    class FakeRespV(FakeResp):
        def json(self):
            return {"result": {"text": " 火山测试 "}}

    sys.modules["requests"].post = lambda url, **kw: (
        captured.update(url=url, **kw), FakeRespV())[1]
    cfgv = {"volc_appid": "aid", "volc_token": "tok", "api_base": ""}
    out = bm.volc_transcribe(mono, 16000, cfgv)
    assert out == "火山测试", out
    assert captured["url"].endswith("/recognize/flash"), captured["url"]
    assert captured["headers"]["X-Api-App-Key"] == "aid"
    assert captured["headers"]["X-Api-Access-Key"] == "tok"
    assert captured["headers"]["X-Api-Resource-Id"] == "volc.bigasr.auc.duration"
    assert base64.b64decode(captured["json"]["audio"]["data"]) == raw
    print("3 volc OK ->", out)

    # 4) 未配 Key 的报错文案
    try:
        bm.api_transcribe(mono, 16000, {"api_provider": "groq", "api_key": "",
                                        "api_base": "", "api_model": ""})
        raise SystemExit("!! 应该报错却没报")
    except RuntimeError as e:
        print("4 no-key error OK:", e)

    # 5) 外部修改热加载
    bm.load_all()
    assert bm.config_changed() == [], "刚加载不应报变化"
    d = json.load(open(bm.SETTINGS_FILE, encoding="utf-8"))
    d["ptt_mode"] = "vad"
    json.dump(d, open(bm.SETTINGS_FILE, "w", encoding="utf-8"))
    time.sleep(0.02)
    assert bm.config_changed() == ["st"], "settings 改动未检测到"
    assert bm.settings.get("ptt_mode") == "vad"
    km = json.load(open(bm.KEYMAP_FILE, encoding="utf-8"))
    aid = km.get("active", "A")
    km.setdefault("profiles", {}).setdefault(aid, {}).setdefault(
        "keys", {})["0x0196"] = {"action": "text", "value": "热加载成功"}
    json.dump(km, open(bm.KEYMAP_FILE, "w", encoding="utf-8"))
    time.sleep(0.02)
    ch = bm.config_changed()
    assert "km" in ch, ch
    assert bm.active_keys().get("0x0196", {}).get("value") == "热加载成功"
    print("5 hot reload OK:", ch)

    # 6) 界面内部保存不应触发热加载误报
    bm.save_keymap()
    bm.save_settings()
    assert bm.config_changed() == [], "内部保存误报外部修改"
    print("6 ui-save no false positive OK")

    print("ALL TESTS PASSED")
finally:
    for p, data in snap.items():
        if data is None:
            __import__("os").remove(p)
        else:
            open(p, "wb").write(data)
    print("(真实配置已还原)")
