# BlueMouse 遥控中心

把「蓝牙 + 2.4G 二合一飞鼠遥控器」变成 vibe coding 神器:躺沙发上按语音键说话,
文字自动粘贴到终端/输入框驱动 AI;其余按键可在图形界面里自由绑定动作。

## 使用方式(三选一)

| 方式 | 入口 | 说明 |
|---|---|---|
| 直接运行(推荐) | `dist\BlueMouse\BlueMouse.exe` | 打包好的绿色版,整个 `BlueMouse` 文件夹可拷去任意位置 |
| 脚本启动 | 双击 `启动遥控中心.bat` | 用源码 + 本机 Python 运行,改代码后即时生效 |
| 已安装 | 桌面/开始菜单快捷方式 | 运行过 `install.ps1` 后可用 |

应用启动后在系统托盘常驻:关掉窗口只是最小化,语音输入继续工作;
托盘左键=打开界面,右键=退出。

## 按键功能

- **语音键**: 按一下开始说话 → **再按一下结束**(默认 toggle 模式, 最长 2 分钟) →
  本地 Whisper(faster-whisper small, int8, 纯离线)识别 → 粘贴到当前焦点窗口 →
  按 OK/回车发给 AI。改回"停顿 1 秒自动结束"的旧模式:
  `settings.json` 里把 `ptt_mode` 改成 `"vad"`
- **浏览器键 (0x0196)** / **右键键 (menu)** / **模式键**: 在界面里点击后绑定动作
  (粘贴文本 / 发送按键组合【可现场捕获】/ 运行命令 / 打开网址),保存即时生效,
  配置存于 `%LOCALAPPDATA%\BlueMouse\keymap.json`
- **音量/静音/飞鼠/背面键盘**: 系统原生行为,不受影响

## 实现要点(踩坑记录)

1. 接收器是 USB 复合设备(VID_1915 北欧芯片): 接口0=USB 声卡,接口2/3=HID。
   麦克风是标准 UAC 设备,但**语音桥会在约 15 秒无操作后休眠**(输出数字零)。
   解法: 每次录音新开音频流(重发 SET_INTERFACE)即可踢活,无需唤醒遥控器。
2. 语音键是 Consumer 页 0x00CF,"秒按秒放",按住时长不上报 —— PTT 用
   "按下即录 + VAD 断句"实现。Windows 不映射此码,必须读原始 HID。
3. 右键键走键盘通道(Key.menu),浏览器键走消费通道(0x0196),两通道分开监听。
4. 隐私: 只读接收器 HID、只录遥控器麦克风、系统键监听只认绑定过的键名、
   不记录任何键盘字符内容。

## 常用路径

- 配置/日志/录音: `%LOCALAPPDATA%\BlueMouse\`
  (keymap.json / settings.json / app.log / last_utterance.wav)
- 界面里"日志目录"按钮直达
- 重新打包: `powershell -ExecutionPolicy Bypass -File build.ps1` → `dist\BlueMouse\`
- 卸载(若运行过 install.ps1): 开始菜单"卸载 BlueMouse"或 `uninstall.ps1`

## 识别质量升级路线

1. 换 CUDA 版 faster-whisper + medium/large-v3(机器有 N 卡,提升最明显)
2. 换 SenseVoice-small(FunASR,中文更快更准,依赖较重)
3. 云端 API(Groq whisper-large-v3-turbo / 讯飞 / 阿里云)——延迟最低但要联网

## 目录结构

- `app/` — 主应用: 托盘 GUI + 语音 PTT + HID 监听(`app/prep_assets.py` 是资产生成脚本)
- `legacy/` — v0.2 控制台版 `bluemouse.py` 及其 `keymap.json`(在 legacy 目录下运行)
- `tools/` — 调试工具: `analyzer.py` 录音分析 / `live_monitor.py` 全通道监视 / `testbench.py` 可视化测试台
- `probes/` — 硬件探针脚本, 记录逆向接收器(语音桥休眠、HID 码表)的过程
- `build.ps1` / `install.ps1` / `uninstall.ps1` / `BlueMouse.spec` — 打包与安装

## 已知边界

- 遥控器麦克风音质是窄带压缩,建议 30cm 内说话
- 首次加载模型约 10-30 秒;关"预热模型"可省 400MB 内存,代价是首次语音有等待
- 模型权重缓存在 `~\.cache\huggingface\hub`,国内换源用 `HF_ENDPOINT=https://hf-mirror.com`
