# bot-console · 小鲸鱼 QQ Bot 管理面板

一个跑在本机的 QQ 机器人**网页面板**：把 AstrBot、SnowLuma（OneBot 协议端）、NapCat、语音引擎、翻唱等一堆命令行服务收进一个页面，点点鼠标就能开关、看状态、改配置、截图扫码登录。

后端是**单文件 Python 标准库 HTTP 服务**（`server.py`，无 Web 框架），前端是**单文件 HTML**（`web/index.html`，无构建步骤）。**代码只有 4 个文件**（`server.py` + 两个 html + 一个 vbs），克隆下来填一个 `config.json` 就能跑。

> 定位：**不修改任何上游代码**。面板只是 AstrBot 官方 HTTP API、SnowLuma WebUI API、OneBot v11 WebSocket 的转发层与补充层。

---

## 目录

- [它能干什么](#它能干什么)
- [界面](#界面)
- [架构](#架构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [config.json 配置说明](#configjson-配置说明)
- [页面与接口](#页面与接口)
- [开机自启](#开机自启)
- [安全提醒](#安全提醒)
- [常见问题](#常见问题)
- [许可](#许可)

---

## 它能干什么

| 页面 | 功能 |
| --- | --- |
| 🏠 概览 | AstrBot / SnowLuma / QQ 登录 / 宿主程序四张状态卡；一键上线、下线、重启；启动机器人 QQ 并挂载；截取登录二维码 |
| 🖼 表情包 | 浏览表情包分类与图片、上传、删除、改分类；配「自动收录」——指定几个 QQ 号发的图自动入库 |
| 🧠 人格 | 直接编辑 AstrBot 人格（system prompt），保存前自动把旧版本备份到 `backups/`，可一键回滚 |
| ⚙️ 模型 | 查看/切换 LLM Provider，改 base_url、key、模型名；调思考等级（off / low / medium / high / max） |
| 👥 群管理 | 走 OneBot 直连协议端：列群、列成员、改群名片、发群消息、设管理、禁言、踢人；**分群参数**（上下文条数、回复概率）单独存盘 |
| 💬 主动聊天 | 定时/按概率主动找人说话；设定目标会话、静默时段、每日上限、空闲判定、是否配表情包 |
| 🧩 长期记忆 | 读取长期记忆插件的统计与会话分布，看/重建记忆图谱 |
| 🧰 扩展插件 | AstrBot 插件配置读写 |
| 🔊 语音 | 三套 TTS 引擎（肥鱼 / CosyVoice2 / GPT-SoVITS）的启停、地址、音色、触发方式；**互斥保护**——同时开会爆显存，所以启动一个会自动拒绝另一个 |
| 🎤 唱歌 | 搜歌 → 下载 → RVC 翻唱 → 混音，全程进度可见，产出可试听 |
| 📊 Token 用量 | 直接读 AstrBot 的 `data_v4.db`：Token 总量、调用次数、平均延迟、成功率、按 Provider 分布、时间曲线 |
| 🎛 参数调整 | 把 AstrBot 的常用配置项（唤醒词、分段回复、上下文等）做成表单，不用再翻 YAML |
| 🌌 QQ空间 | 查看 QQ 空间插件状态、发布说说、改定时/评论策略 |
| 🩺 掉线监控 | 定期检查 QQ 是否在线，掉线自动把二维码贴到页面上并弹 Windows 通知 |
| 📜 日志 | 七个日志文件（AstrBot / SnowLuma / NapCat / 监听器 / 肥鱼 / CosyVoice2 / GPT-SoVITS）在线 tail |

另外还有几张「顺手做的」卡片：

- **游戏模式**：一键停掉所有吃显存/内存的语音服务，并把语音与唱歌总开关关掉；开着时每 30 秒复查一次。
- **自动登录**：QQ 掉线后自动重新点登录 / 刷新二维码。
- **自动清理**：按天数清理翻唱产出和语音临时文件。

---

## 界面

深色主题，左侧 15 项导航，右侧单页视图切换（`location.hash` 路由，刷新不丢当前位置）。

- 状态卡每 5 秒轮询 `/api/status`，端口号从后端配置读取，改 `config.json` 后页面显示跟着变。
- 掉线横幅每 20 秒轮询 `/api/monitor`，掉线时自动出现，点进去就是扫码页。
- 登录态用 `qp_session` cookie（`HttpOnly` + `SameSite=Lax`），勾「记住账号密码」有效期 30 天，否则 12 小时。

---

## 架构

```
┌──────────────┐
│  web/index.html │  单文件前端（无构建）
└───────┬──────┘
        │  fetch /api/*（qp_session cookie 鉴权）
┌───────▼──────────────┐
│   server.py           │  ThreadingHTTPServer + 路由表（91 个 /api/* 接口）
│   仅标准库             │  会话 / 鉴权 / 进程管理 / 日志 tail
└───┬────────┬───────┬─┘
    │HTTP    │HTTP   │WebSocket
    ▼        ▼       ▼
 AstrBot  SnowLuma  OneBot v11
 :6185    :5099     :3001
             │
             ▼
        QQ 客户端（独立 user-data-dir 的机器人实例）
```

进程管理全部走 Windows 原生命令：

- `Get-CimInstance Win32_Process` / `Get-NetTCPConnection` 找进程与端口占用
- `taskkill /F /PID` 结束进程
- `netstat -ano`、`tasklist` 做交叉验证
- 拉起服务时用 `subprocess.Popen(..., creationflags=CREATE_NO_WINDOW)`，不弹黑框

所以：**这套东西只能在 Windows 上跑**。

---

## 环境要求

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11（依赖 PowerShell、taskkill、netstat） |
| Python | 3.8+（实测 3.10 / 3.13；代码没用 `match`、海象运算符等新语法） |
| 浏览器 | 任意现代浏览器 |
| 可选依赖 | 见下 |

面板**本体零第三方依赖**。以下库只在对应功能可用时按需 import，缺了就在该页面给出提示，不影响其他功能：

| 库 | 装它才能用 |
| --- | --- |
| `websockets` | 群管理（直连 OneBot v11 WS） |
| `Pillow` | 截取 QQ 登录窗口二维码 |
| `opencv-python` + `numpy` | 从截图里自动裁出二维码（没有就返回整窗截图） |
| `sqlite3` | Token 用量（Python 自带） |

```bash
pip install websockets Pillow opencv-python numpy
```

被管理的服务（AstrBot、SnowLuma、TTS 引擎等）需要你自己先装好，面板不负责安装它们。

---

## 快速开始

### 1. 拿到代码

```bash
git clone https://github.com/rainforest-11/bot-console.git
cd bot-console
```

### 2. 放好位置

面板默认把 `config.json` 所在目录的**上一级**当作「AstrBot 根目录」。推荐的目录结构：

```
C:\AstrBot\
├── AstrBot\                 # AstrBot 源码
├── venv\Scripts\python.exe  # AstrBot 的虚拟环境
├── qbot_group_settings.json # 分群参数（面板生成）
├── data\                    # 面板运行时数据（自动创建）
└── panel\                   # ← 本仓库放这里
    ├── server.py
    ├── config.json
    └── web\
```

放在别处也能用，只要把 `config.json` 里的 `base_dir`、`astrbot_dir` 指对。

### 3. 写配置

```bash
copy config.example.json config.json
```

然后用编辑器打开 `config.json`，至少把这几项填上：`base_dir`、`astrbot_dir`、`venv_python`、`astrbot_url`、`astrbot_user`、`astrbot_pass`、`panel_password`、`qq_exe`。

完整字段见下一节。

### 4. 启动

```bash
python server.py
```

看到 `QQ Bot 管理面板: http://127.0.0.1:8899` 就好了，浏览器打开这个地址。

**第一次启动会自动生成登录密码并打印在控制台**：

```
============================================================
已生成本面板登录密码：xxxxxxx
账号：admin
（想换密码：删掉 panel\auth.json 重启，或直接改 config.json 的 panel_password）
============================================================
```

不想看控制台就双击 `open-panel.vbs`（无窗口启动 + 自动开浏览器）。

### 5. 登录

用上一步打印的账号密码登录。忘记密码：删掉 `panel\auth.json` 重启，会重新生成并打印。

---

## config.json 配置说明

`config.json` 与 `server.py` **同目录**。留空（`""`）等于没配，对应功能会在页面上提示未配置。

### 路径

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `base_dir` | AstrBot 根目录，所有相对路径的基准 | `server.py` 所在目录的上一级 |
| `astrbot_dir` | AstrBot 源码目录 | `base_dir\AstrBot` |
| `venv_python` | AstrBot 的 python 解释器 | `base_dir\venv\Scripts\python.exe` |
| `panel_dir` | 面板自身目录：存 `auth.json`、`panel.log`、`backups/`、`qq_login.json`、`sl_token.txt` | `server.py` 所在目录 |

### 面板

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `port` | 面板端口（也可用环境变量 `PANEL_PORT` 覆盖） | `8899` |
| `panel_user` | 面板登录账号 | `astrbot` |
| `panel_password` | 面板登录密码；留空则首次启动随机生成 | `""` |

### AstrBot

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `astrbot_url` | AstrBot WebUI 地址 | `http://127.0.0.1:6185` |
| `astrbot_user` / `astrbot_pass` | AstrBot 登录账号密码（面板用它调 `/api/auth/login` 拿 token） | `astrbot` / `""` |
| `astrbot_ws_port` | AstrBot 的 OneBot WS 端口（判断连接数用） | `6199` |

### QQ

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `qq_number` | 机器人 QQ 号；**留空则自动取 SnowLuma 报告的第一个已注入实例的 uin** | `""` |
| `qq_exe` | QQ 可执行文件路径；留空则「启动 QQ」功能禁用 | `""` |
| `qq_user_data_dir` | 机器人 QQ 的独立数据目录（与你自己登录的 QQ 互不干扰） | `""` |
| `owner_qq` | 主人 QQ 号，用于「私聊自己」按钮和默认主动聊天目标；留空则隐藏 | `""` |

### SnowLuma / OneBot

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `snowluma_dir` | SnowLuma 目录 | `""` |
| `snowluma_node` | node 可执行文件 | `snowluma_dir\node.exe` |
| `snowluma_main` | 入口脚本 | `snowluma_dir\index.mjs` |
| `snowluma_url` | SnowLuma WebUI 地址 | `http://127.0.0.1:5099` |
| `snowluma_pass_file` | 存 SnowLuma WebUI 密码的文件（面板读它来登录） | `""` |
| `onebot_ws` | OneBot v11 WebSocket 地址 | `ws://127.0.0.1:3001` |
| `snowluma_http_port` | SnowLuma 的 OneBot HTTP 端口 | `3000` |
| `napcat_dir` | NapCat 目录（另一种协议端方案，仅在用 NapCat 时配） | `""` |

### 语音 / 翻唱（全部可选，不配则对应页面提示未配置）

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `embedding_dir` / `embedding_python` / `embedding_port` | 本地向量服务 | `""` / `venv_python` / `8912` |
| `cosyvoice2_dir` / `cosyvoice2_python` / `cosyvoice2_port` | CosyVoice2 | `""` / `""` / `8910` |
| `gptsovits_dir` / `gptsovits_python` / `gptsovits_port` | GPT-SoVITS | `""` / `""` / `9880` |
| `feiyu_dir` / `feiyu_python` / `feiyu_port` | 肥鱼语音 | `""` / `""` / `8911` |
| `sing_dir` / `sing_python` | RVC 翻唱工程目录与解释器 | `""` / `venv_python` |

### 其他

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `persona_id` | 默认操作的人格 id | `default` |
| `persona_file` | 人格镜像文件（保存时同步一份到本地） | `base_dir\persona.md` |
| `host_process` | 「宿主程序」状态卡要检测的进程名；留空则该卡片恒为未运行 | `""` |

### 最小可用示例

```json
{
  "base_dir": "C:/AstrBot",
  "astrbot_dir": "C:/AstrBot/AstrBot",
  "venv_python": "C:/AstrBot/venv/Scripts/python.exe",
  "astrbot_url": "http://127.0.0.1:6185",
  "astrbot_user": "astrbot",
  "astrbot_pass": "你的 AstrBot 密码",
  "panel_user": "admin",
  "panel_password": "换个强一点的",
  "qq_exe": "C:/Program Files/Tencent/QQNT/QQ.exe",
  "qq_user_data_dir": "C:/AstrBot/qqbot-data",
  "snowluma_dir": "C:/AstrBot/snowluma",
  "snowluma_pass_file": "C:/AstrBot/sl_pass.txt"
}
```

---

## 页面与接口

前端每个页面都只调 `/api/*`，共 91 个接口路径（同一个路径往往有 GET/POST 两种方法），按前缀分组：

| 前缀 | 用途 |
| --- | --- |
| `/api/auth/*` | 登录、登出、当前用户 |
| `/api/status`、`/api/game/*` | 总状态、游戏模式 |
| `/api/bot/*` | 上线、下线、重启 AstrBot |
| `/api/sl/*` | SnowLuma 状态、重启、注入、显示窗口、截图、二维码、重新登录 |
| `/api/qq/*` | QQ 账号列表、截登录二维码、登录配置、快速登录 |
| `/api/qqprofile/*` | 机器人资料读取与修改 |
| `/api/persona/*` | 人格读写、回滚 |
| `/api/providers/*` | 模型 Provider 列表、启用、保存、思考等级 |
| `/api/groups/*` | 群列表、成员、群名片、发消息、设管理、禁言、踢人、分群参数 |
| `/api/memes/*`、`/api/meme-autosave`、`/api/pokepro/*` | 表情包与自动收录、戳一戳 |
| `/api/voice/*` | 语音配置与三套引擎的启停 |
| `/api/sing/*` | 搜歌、开始/中止翻唱、试听、混音 |
| `/api/tokens/*` | Token 用量与明细 |
| `/api/params`、`/api/plugin-config` | AstrBot 参数与插件配置 |
| `/api/qzone/*` | QQ 空间 |
| `/api/proactive/*` | 主动聊天 |
| `/api/monitor/*` | 掉线与二维码监控 |
| `/api/autologin`、`/api/clean`、`/api/logs`、`/api/livingmemory/*` | 自动登录、自动清理、日志、长期记忆 |

约定：所有接口返回 `{"status": "ok" | "error", ...}`；未登录的接口请求返回 `401 {"need_login": true}`，页面访问则 302 到 `/login.html`。

---

## 开机自启

把 `open-panel.vbs` 的快捷方式丢进 `shell:startup`（Win+R 输入即可打开启动目录）就行。它做的事：

1. 从脚本位置推导目录，优先找 `<上一级>\venv\Scripts\pythonw.exe`，再找 `.venv`，最后退回 PATH 里的 `pythonw`
2. 无窗口启动 `server.py`
3. 等 1.5 秒，自动打开 `http://127.0.0.1:8899/`

---

## 安全提醒

- 面板**只监听 127.0.0.1**，没有内置 HTTPS，也不要做端口映射/公网暴露。
- `config.json`、`auth.json`、`qq_login.json`、`sl_token.txt`、`backups/`、`data/`、`*.log` 全部在 `.gitignore` 里，**别提交自己的配置和日志**。
- `qq_login.json` 里的 QQ 密码是 base64 编码存的，不是加密。共享机器上请只勾「记住账号」不勾「记住密码」。
- 面板能直接结束进程、改 AstrBot 配置、发群消息——就等于你本机的机器人控制台，别把登录态借给别人。

---

## 常见问题

**启动后页面 401 / 一直跳登录页**
cookie 没种上。确认浏览器允许 `127.0.0.1` 的 cookie；或者把地址从头敲一遍而不是走代理。

**登录密码忘了**
删掉 `panel\auth.json` 重启，会重新生成随机密码并打印在控制台/`panel.log`。也可以直接在 `config.json` 里设 `panel_password` 再删 auth.json。

**群管理页提示「缺少 websockets 依赖」**
`pip install websockets`。面板只在群管理这里用到它，其他功能不受影响。

**「启动 QQ」按了没反应**
检查 `qq_exe` 路径对不对（QQNT 一般是 `C:\Program Files\Tencent\QQNT\QQ.exe`），以及 `qq_user_data_dir` 不要和你日常用的 QQ 数据目录相同。

**二维码一直是旧图**
二维码是从机器人 QQ 的登录窗口截的，窗口最小化/被遮挡会截到旧画面。点「重新登录（重开 QQ）」再试，或者把 QQ 窗口先显示出来。

**语音服务起不来 / 说爆显存**
肥鱼、CosyVoice2、GPT-SoVITS 三个引擎吃显存，面板故意做成互斥：启动一个会把另外的停掉。先关掉不用的那个。

**端口冲突**
改 `config.json` 里的 `port`（或用环境变量 `PANEL_PORT`），页面上的端口文案会自动跟着更新。

**换机器怎么迁移**
只拷 `panel\config.json`、`panel\auth.json`（可选，拷了免重设密码）、`data\`、`qbot_group_settings.json`。日志和 `backups/` 不用带。

---

## 许可

MIT，见 [LICENSE](LICENSE)。

面板依赖的 AstrBot、SnowLuma、NapCat、CosyVoice2、GPT-SoVITS 等各有自己的许可证，本项目不包含它们的代码。
