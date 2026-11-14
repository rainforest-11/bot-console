# -*- coding: utf-8 -*-
"""QQ Bot 管理面板 —— 本地单文件后端（仅用标准库 + 可选 PIL）。

功能：
  * 概览/上线/下线/重启/二维码
  * 表情包：浏览、上传（含新建分类）、删除、改分类描述
  * 人格：读写 system_prompt（带自动备份）
  * 模型：查看/切换/修改 provider
  * QQ空间：状态、发说说、看动态、定时任务配置
  * 日志：astrbot / snowluma / watcher

后端只是 AstrBot 官方 HTTP API + SnowLuma API 的转发与补充，不直接改业务代码。
"""
import base64
import datetime
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 配置
# 所有和机器相关的东西都从本文件同目录的 config.json 读（见 config.example.json）。
# 什么都没配也能起：此时按“面板放在 <AstrBot 根>/panel/”的默认布局推导路径，
# 外部服务（SnowLuma / 语音 / Embedding）留空，对应页面会提示未配置。
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_SELF_DIR, "config.json")


def _load_raw_cfg():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


_RAW = _load_raw_cfg()


def _c(key, default=None):
    """读顶层配置项；空字符串视作没配。"""
    v = _RAW.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v


def _j(*parts):
    """os.path.join 的容错版：空片段直接跳过，全空返回空串。"""
    parts = [p for p in parts if p]
    return os.path.join(*parts) if parts else ""


def _uport(url, default):
    """从 http://host:port 之类的地址里取端口。"""
    try:
        p = urllib.parse.urlparse(url).port
        return int(p) if p else int(default)
    except Exception:
        return int(default)


# AstrBot 根目录：默认取面板目录的上一级（<AstrBot>\panel\server.py）
BASE = os.path.abspath(str(_c("base_dir") or os.path.dirname(_SELF_DIR)))
ASTRBOT_DIR = os.path.abspath(str(_c("astrbot_dir") or os.path.join(BASE, "AstrBot")))
VENV_PY = str(_c("venv_python") or os.path.join(BASE, "venv", "Scripts", "python.exe"))
# 机器人 QQ 号；留空则运行时向 SnowLuma 要当前已注入实例的 uin
QQ_NUMBER = str(_c("qq_number") or "")

# ---------------- SnowLuma（QQ 协议端） ----------------
SL_DIR = str(_c("snowluma_dir") or "")
SL_NODE = str(_c("snowluma_node") or _j(SL_DIR, "node.exe"))
SL_MAIN = str(_c("snowluma_main") or _j(SL_DIR, "index.mjs"))
SL_URL = str(_c("snowluma_url") or "http://127.0.0.1:5099")
SL_PASS_FILE = str(_c("snowluma_pass_file") or "")
if not SL_PASS_FILE and SL_DIR:
    SL_PASS_FILE = os.path.join(os.path.dirname(SL_DIR.rstrip("\\/")), "sl_pass.txt")
SL_TOKEN_FILE = os.path.join(_SELF_DIR, "sl_token.txt")
SL_LOG = os.path.join(BASE, "snowluma.log")
SL_LOG_SRC = _j(SL_DIR, "logs")
# 机器人专用 QQ（独立 user-data-dir，和你自己登录的 QQ 互不干扰）
QQ_BOT_EXE = str(_c("qq_exe") or "")
QQ_BOT_DATA = str(_c("qq_user_data_dir") or "")
# 残留 NapCat 进程的安装目录（面板见到就清掉，避免和 SnowLuma 抢号）
NAPCAT_DIR = str(_c("napcat_dir") or "")

# 本地 Embedding 服务（LivingMemory 向量检索依赖；常驻约 100MB 内存）
EMB_DIR = str(_c("embedding_dir") or "")
EMB_PY = str(_c("embedding_python") or "")
EMB_MAIN = _j(EMB_DIR, "service.py")
EMB_PORT = int(_c("embedding_port") or 8912)
EMB_LOG = os.path.join(BASE, "embed_svc.log")

# CosyVoice2 声音克隆
CV2_DIR = str(_c("cosyvoice2_dir") or "")
CV2_PY = str(_c("cosyvoice2_python") or "")
CV2_MAIN = _j(CV2_DIR, "service.py")
CV2_PORT = int(_c("cosyvoice2_port") or 8910)
CV2_LOG = os.path.join(BASE, "cosyvoice2.log")

# GPT-SoVITS（备选克隆引擎；与 CosyVoice2 抢显存，两者不要同时开）
GSV_DIR = str(_c("gptsovits_dir") or "")
GSV_PY = str(_c("gptsovits_python") or _j(GSV_DIR, "runtime", "python.exe"))
GSV_MAIN = _j(GSV_DIR, "api.py")
GSV_PORT = int(_c("gptsovits_port") or 9880)
GSV_LOG = os.path.join(BASE, "gptsovits.log")

# 肥鱼声音服务（CosyVoice3 + 自训声库）
FY_DIR = str(_c("feiyu_dir") or "")
FY_PY = str(_c("feiyu_python") or "")
FY_MAIN = _j(FY_DIR, "service.py")
FY_PORT = int(_c("feiyu_port") or 8911)
FY_LOG = os.path.join(BASE, "feiyu.log")

PANEL_DIR = str(_c("panel_dir") or _SELF_DIR)
WEB_DIR = os.path.join(PANEL_DIR, "web")
BACKUP_DIR = os.path.join(PANEL_DIR, "backups")
PERSONA_MIRROR = str(_c("persona_file") or os.path.join(BASE, "persona.md"))
# 面板“人格”页操作的是 AstrBot 里的哪个人格（persona_id）
PERSONA_ID = str(_c("persona_id") or "default")
PAUSE_FLAG = os.path.join(BASE, "bot_off.flag")
WATCHER_PID = os.path.join(BASE, "watcher.pid")
QR_CACHE = os.path.join(BASE, "qrcode_login.png")

# ---------------- 面板登录 ----------------
import hashlib
import secrets

AUTH_FILE = os.path.join(PANEL_DIR, "auth.json")
DEFAULT_USER = str(_c("panel_user") or "astrbot")
# 面板登录密码：只在首次生成 auth.json 时用一次。留空表示“不设密码”，
# 首次启动会随机生成一个并打印到日志/控制台（登录页也能看到提示）。
DEFAULT_PASS = str(_c("panel_password") or "")
_sessions = {}  # token -> (expire_ts, username)


def _hash_pw(password, salt):
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def load_auth():
    try:
        with open(AUTH_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        if d.get("username") and d.get("salt") and d.get("hash"):
            return d
    except Exception:
        pass
    pw = DEFAULT_PASS or secrets.token_urlsafe(9)
    salt = secrets.token_hex(8)
    d = {"username": DEFAULT_USER, "salt": salt, "hash": _hash_pw(pw, salt)}
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    if not DEFAULT_PASS:
        log("=" * 56)
        log("已生成面板登录密码：%s   账号：%s" % (pw, DEFAULT_USER))
        log("改密码请删掉 %s 后重启（或直接在 config.json 里设 panel_password）" % AUTH_FILE)
        log("=" * 56)
    return d


def verify_login(username, password):
    d = load_auth()
    if (username or "").strip() != d.get("username"):
        return False
    return secrets.compare_digest(_hash_pw(password or "", d["salt"]), d["hash"])


def session_ok(token):
    if not token:
        return False
    item = _sessions.get(token)
    if not item:
        return False
    if item[0] < time.time():
        _sessions.pop(token, None)
        return False
    return True


ASTRBOT_URL = str(_c("astrbot_url") or "http://127.0.0.1:6185")
ASTRBOT_PORT = _uport(ASTRBOT_URL, 6185)
SL_PORT = _uport(SL_URL, 5099)
NO_WINDOW = 0x08000000

# ONEBOT：AstrBot / SnowLuma 侧的 OneBot 接入点
ONEBOT_WS = str(_c("onebot_ws") or "ws://127.0.0.1:3001")
ONEBOT_WWW_PORT = int(_c("snowluma_http_port") or 3000)
# AstrBot 的 OneBot WS 服务端口（SnowLuma 作为客户端连上去的那个）
ASTRBOT_WS_PORT = int(_c("astrbot_ws_port") or 6199)

DEFAULT_CFG = {
    "port": int(_c("port") or 8899),
    "astrbot_user": str(_c("astrbot_user") or "astrbot"),
    "astrbot_pass": str(_c("astrbot_pass") or ""),
}

os.makedirs(BACKUP_DIR, exist_ok=True)

# pythonw.exe 启动时没有控制台，stdout 为 None，print 会炸；统一重定向到日志
if sys.stdout is None or sys.stderr is None:
    try:
        _lg = open(os.path.join(PANEL_DIR, "panel.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or _lg
        sys.stderr = sys.stderr or _lg
    except Exception:
        pass

LOG_FILE = os.path.join(PANEL_DIR, "panel.log")


def log(msg):
    try:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)
    except Exception:
        pass



# ---------------------------------------------------------------- 基础设施
def load_cfg():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


CFG = load_cfg()


def _startupinfo():
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def run_hidden(cmd, timeout=60):
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="ignore",
            timeout=timeout,
            creationflags=NO_WINDOW,
            startupinfo=_startupinfo(),
        )
    except Exception:
        return None


def port_open(port, host="127.0.0.1", timeout=1.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def http_json(url, method="GET", body=None, headers=None, timeout=30, raw=False):
    """返回 (status, data)。data 为解析后的 JSON 或原始 bytes（raw=True）。"""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            buf = r.read()
            status = r.status
            if raw:
                return status, buf
            try:
                return status, json.loads(buf.decode("utf-8", "replace"))
            except Exception:
                return status, {"status": "error", "message": buf.decode("utf-8", "replace")[:400]}
    except urllib.error.HTTPError as e:
        buf = e.read()
        if raw:
            return e.code, buf
        try:
            return e.code, json.loads(buf.decode("utf-8", "replace"))
        except Exception:
            return e.code, {"status": "error", "message": buf.decode("utf-8", "replace")[:400]}
    except Exception as e:
        return 0, {"status": "error", "message": repr(e)}


# ---------------------------------------------------------------- AstrBot 鉴权
_ab_lock = threading.Lock()
_ab_token = {"v": None, "ts": 0.0}


def ab_login(force=False):
    with _ab_lock:
        if _ab_token["v"] and not force and time.time() - _ab_token["ts"] < 1800:
            return _ab_token["v"]
        st, data = http_json(
            ASTRBOT_URL + "/api/auth/login",
            "POST",
            {"username": CFG["astrbot_user"], "password": CFG["astrbot_pass"]},
            timeout=15,
        )
        tok = ((data or {}).get("data") or {}).get("token")
        if tok:
            _ab_token["v"] = tok
            _ab_token["ts"] = time.time()
        return tok


def ab(method, path, params=None, body=None, raw=False, timeout=60, retry=True):
    tok = ab_login()
    if not tok:
        return 0, {"status": "error", "message": "AstrBot 未启动或登录失败"}
    url = ASTRBOT_URL + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    st, data = http_json(
        url, method, body, {"Authorization": "Bearer " + tok}, timeout=timeout, raw=raw
    )
    if st == 401 and retry:
        ab_login(force=True)
        return ab(method, path, params, body, raw=raw, timeout=timeout, retry=False)
    return st, data


# ---------------------------------------------------------------- 进程控制
def ps_pids(filter_ps):
    r = run_hidden(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Where-Object { %s } | Select-Object -ExpandProperty ProcessId"
            % filter_ps,
        ]
    )
    pids = []
    for line in (r.stdout or "").splitlines() if r else []:
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def port_owner_pids(port):
    r = run_hidden(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-NetTCPConnection -LocalPort %d -State Listen -ErrorAction SilentlyContinue).OwningProcess" % port,
        ]
    )
    out = (r.stdout or "") + (r.stderr or "")
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def astrbot_pids():
    pids = ps_pids(
        "($_.ProcessName -like '*python*' -and $_.CommandLine -like '*main.py*' -and $_.CommandLine -like '*AstrBot*')"
    )
    if ASTRBOT_PORT:
        pids += port_owner_pids(ASTRBOT_PORT)
    return sorted(set(pids))


def _psq(s):
    """把字符串塞进 PowerShell 单引号里（单引号本身要写两遍）。"""
    return str(s).replace("'", "''")


def snowluma_pids():
    """SnowLuma 是 node.exe 跑 index.mjs。
    必须限定 Name=node.exe：查询命令自身的命令行里也含 'snowluma*index.mjs'，
    不限定的话 powershell 会把自己也算进去，导致误杀/误判实例数。"""
    main = os.path.basename(SL_MAIN or "index.mjs") or "index.mjs"
    return ps_pids("($_.Name -eq 'node.exe' -and $_.CommandLine -like '*%s*')" % _psq(main))


def qqbot_pids():
    """机器人专用 QQ 实例的主进程（独立 user-data-dir，不影响用户主号）。
    只取主进程：渲染/GPU 子进程（带 --type=）注入会失败，也会把 PID 列表搞乱。"""
    if not QQ_BOT_EXE:
        return []
    cond = "$_.ExecutablePath -eq '%s'" % _psq(QQ_BOT_EXE)
    if QQ_BOT_DATA:
        cond += " -and $_.CommandLine -like '*%s*'" % _psq(os.path.basename(QQ_BOT_DATA.rstrip("\\/")))
    cond += " -and $_.CommandLine -notlike '*--type=*'"
    return ps_pids("(%s)" % cond)


def napcat_pids():
    """残留的 NapCat 进程。NapCat 与 SnowLuma 抢同一个 QQ 号会让 QQ 反复被踢下线，
    面板见到就清掉。未配置 napcat_dir 时视为没有。"""
    if not NAPCAT_DIR:
        return []
    return ps_pids("($_.ExecutablePath -like '%s*')" % _psq(NAPCAT_DIR.rstrip("\\/") + os.sep))


def kill_pids(pids):
    if not pids:
        return
    run_hidden(["taskkill", "/F"] + sum([["/PID", str(p)] for p in pids], []))


def stop_astrbot():
    kill_pids(astrbot_pids())
    for _ in range(40):
        if not port_open(ASTRBOT_PORT):
            break
        time.sleep(0.5)


def start_embed(wait=False):
    """起本地 Embedding 服务。LivingMemory 的向量检索全靠它，
    没起的话每次记忆写入都会 Connection error 并卡在重建索引。"""
    if port_open(EMB_PORT):
        return True, "Embedding 服务已在运行"
    try:
        logf = open(EMB_LOG, "a", encoding="utf-8", errors="ignore")
        subprocess.Popen(
            [EMB_PY, EMB_MAIN], cwd=EMB_DIR,
            creationflags=NO_WINDOW, startupinfo=_startupinfo(),
            stdout=logf, stderr=subprocess.STDOUT,
        )
    except Exception as e:
        return False, "Embedding 服务启动失败: %r" % (e,)
    if not wait:
        return True, "已下发启动 Embedding 服务"
    for _ in range(60):
        if port_open(EMB_PORT):
            return True, "Embedding 服务已就绪"
        time.sleep(1)
    return False, "Embedding 服务启动超时（60s 内 %d 未监听）" % EMB_PORT


def start_astrbot(wait=True):
    if port_open(ASTRBOT_PORT):
        return True, "AstrBot 已在运行"
    start_embed(wait=False)
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 让 AstrBot 能调用随包 ffmpeg（Edge TTS 需要 mp3 -> wav/amr 转换）
    env["PATH"] = os.path.join(BASE, "bin") + ";" + env.get("PATH", "")
    logf = open(os.path.join(BASE, "astrbot_run.log"), "a", encoding="utf-8", errors="ignore")
    try:
        subprocess.Popen(
            [VENV_PY, "main.py"],
            cwd=ASTRBOT_DIR,
            env=env,
            creationflags=NO_WINDOW,
            startupinfo=_startupinfo(),
            stdout=logf,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        return False, "启动失败: %r" % (e,)
    if not wait:
        return True, "已下发启动"
    for _ in range(120):
        if port_open(ASTRBOT_PORT):
            return True, "AstrBot 已就绪"
        time.sleep(1)
    return False, "启动超时（120s 内 AstrBot(%d) 未就绪），请看日志" % ASTRBOT_PORT


def restart_astrbot():
    """只重启 AstrBot，SnowLuma/QQ 不动（不掉 QQ 登录）。"""
    stop_astrbot()
    time.sleep(1)
    return start_astrbot()


def stop_snowluma():
    kill_pids(snowluma_pids())
    for _ in range(40):
        if not port_open(SL_PORT):
            break
        time.sleep(0.5)


def _sl_log_ready():
    """SnowLuma 日志文件可能还不存在（比如是手动启动的），先建一个空的，免得日志页 404。"""
    try:
        if not os.path.exists(SL_LOG):
            with open(SL_LOG, "a", encoding="utf-8"):
                pass
    except Exception:
        pass
    return SL_LOG


def port_owner(port):
    """哪个 PID 在 LISTEN 这个端口（取不到返回 None）。"""
    r = run_hidden(["netstat", "-ano"])
    for line in (r.stdout or "").splitlines() if r else []:
        if ":%d " % port in line and "LISTENING" in line:
            parts = line.split()
            if parts and parts[-1].isdigit():
                return int(parts[-1])
    return None


def snowluma_broken():
    """两个 SnowLuma 实例各占一半端口时，面板问到的实例永远看不到真实 QQ。
    返回 True 表示端口分叉（需要全部杀掉重来）。"""
    o_web, o_ws = port_owner(SL_PORT), port_owner(ONEBOT_WWW_PORT)
    if o_ws and o_web and o_ws != o_web:
        return True
    return False


_sl_start_lock = threading.Lock()


def start_snowluma(wait=True):
    if port_open(SL_PORT) and not snowluma_broken():
        return True, "SnowLuma 已在运行"
    with _sl_start_lock:
        # 并发调用（自动登录 + 面板按钮 + watcher）会同时 Popen，起出两个实例，
        # 端口分叉后 WebUI 看不到真实 QQ —— 整个“查重 + 启动”都必须在锁里。
        if port_open(SL_PORT) and not snowluma_broken():
            return True, "SnowLuma 已在运行"
        stale = snowluma_pids()
        if stale:
            kill_pids(stale)
            for _ in range(40):
                if not snowluma_pids():
                    break
                time.sleep(0.5)
        env = os.environ.copy()
        env["SNOWLUMA_ACCEPT_EULA"] = "1"
        env["SNOWLUMA_ACCEPT_PRIVACY"] = "1"
        env["SNOWLUMA_WEBUI_PORT"] = str(SL_PORT)
        env["SNOWLUMA_ONEBOT_WS_PORT"] = str(_uport(ONEBOT_WS, 3001))
        env["SNOWLUMA_ONEBOT_HTTP_PORT"] = str(ONEBOT_WWW_PORT)
        # 只注入手动 load 的实例：否则它会把你自己登录的 QQ 也挂上去
        env["SNOWLUMA_HOOK_AUTOLOAD"] = "0"
        _sl_log_ready()
        logf = open(SL_LOG, "a", encoding="utf-8", errors="ignore")
        try:
            subprocess.Popen(
                [SL_NODE, SL_MAIN],
                cwd=SL_DIR,
                env=env,
                creationflags=NO_WINDOW,
                startupinfo=_startupinfo(),
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            return False, "启动失败: %r" % (e,)
    if not wait:
        return True, "已下发启动"
    for _ in range(150):
        if port_open(SL_PORT) and port_open(ONEBOT_WWW_PORT):
            if not snowluma_broken():
                return True, "SnowLuma 已就绪"
        time.sleep(1)
    return False, "启动超时（150s 内 %d/%d 未就绪），请看面板日志" % (SL_PORT, _uport(ONEBOT_WS, 3001))


_cv2_lock = threading.Lock()


def cv2_pids():
    return ps_pids("($_.CommandLine -like '*cosyvoice2*service.py*')")


def stop_cv2():
    kill_pids(cv2_pids())
    for _ in range(30):
        if not port_open(CV2_PORT):
            break
        time.sleep(0.5)


def start_cv2(wait=True):
    """CosyVoice2 模型加载慢（约 40~60 秒），后台起，不阻塞调用方。"""
    if port_open(CV2_PORT):
        return True, "CosyVoice2 已在运行"
    with _cv2_lock:
        if port_open(CV2_PORT):
            return True, "CosyVoice2 已在运行"
        stale = cv2_pids()
        if stale:
            kill_pids(stale)
            time.sleep(1)
        logf = open(CV2_LOG, "a", encoding="utf-8", errors="ignore")
        try:
            subprocess.Popen(
                [CV2_PY, CV2_MAIN],
                cwd=CV2_DIR,
                creationflags=NO_WINDOW,
                startupinfo=_startupinfo(),
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            return False, "启动失败: %r" % (e,)
    if not wait:
        return True, "已下发启动"
    for _ in range(180):
        if port_open(CV2_PORT):
            return True, "CosyVoice2 已就绪"
        time.sleep(1)
    return False, "启动超时（180s 内 %d 未就绪），请看 cosyvoice2.log" % CV2_PORT


_gsv_lock = threading.Lock()


def gsv_pids():
    return ps_pids("($_.CommandLine -like '*gptsovits*api.py*')")


def stop_gsv():
    kill_pids(gsv_pids())
    for _ in range(30):
        if not port_open(GSV_PORT):
            break
        time.sleep(0.5)


def start_gsv(wait=True):
    """GPT-SoVITS 首次加载模型较慢，后台起。"""
    if port_open(GSV_PORT):
        return True, "GPT-SoVITS 已在运行"
    with _gsv_lock:
        if port_open(GSV_PORT):
            return True, "GPT-SoVITS 已在运行"
        stale = gsv_pids()
        if stale:
            kill_pids(stale)
            time.sleep(1)
        logf = open(GSV_LOG, "a", encoding="utf-8", errors="ignore")
        try:
            subprocess.Popen(
                [
                    GSV_PY,
                    GSV_MAIN,
                    "-s",
                    "GPT_SoVITS/pretrained_models/gsv-v4-pretrained/s2Gv4.pth",
                    "-g",
                    "GPT_SoVITS/pretrained_models/s1bert25hz-2kh-longer-epoch=68e-step=50232.ckpt",
                    "-a",
                    "127.0.0.1",
                    "-p",
                    str(GSV_PORT),
                ],
                cwd=GSV_DIR,
                creationflags=NO_WINDOW,
                startupinfo=_startupinfo(),
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            return False, "启动失败: %r" % (e,)
    if not wait:
        return True, "已下发启动"
    for _ in range(240):
        if port_open(GSV_PORT):
            return True, "GPT-SoVITS 已就绪"
        time.sleep(1)
    return False, "启动超时（240s 内 %d 未就绪），请看 gptsovits.log" % GSV_PORT


_fy_lock = threading.Lock()


def fy_pids():
    # 命令行是 "python.exe service.py"（不含目录名），只能按端口找占用进程
    owner = port_owner(FY_PORT)
    if owner:
        return [owner]
    return ps_pids("($_.CommandLine -like '*service.py*' -and $_.CommandLine -notlike '*cosyvoice2*')")


def stop_fy():
    kill_pids(fy_pids())
    for _ in range(30):
        if not port_open(FY_PORT):
            break
        time.sleep(0.5)


def start_fy(wait=True):
    """肥鱼声音服务（CosyVoice3），首次加载约 15~40 秒。"""
    if port_open(FY_PORT):
        return True, "肥鱼声音服务已在运行"
    with _fy_lock:
        if port_open(FY_PORT):
            return True, "肥鱼声音服务已在运行"
        stale = fy_pids()
        if stale:
            kill_pids(stale)
            time.sleep(1)
        logf = open(FY_LOG, "a", encoding="utf-8", errors="ignore")
        try:
            subprocess.Popen(
                [FY_PY, FY_MAIN],
                cwd=FY_DIR,
                creationflags=NO_WINDOW,
                startupinfo=_startupinfo(),
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        except Exception as e:
            return False, "启动失败: %r" % (e,)
    if not wait:
        return True, "已下发启动"
    for _ in range(180):
        if port_open(FY_PORT):
            return True, "肥鱼声音服务已就绪"
        time.sleep(1)
    return False, "启动超时（180s 内 %d 未就绪），请看 feiyu.log" % FY_PORT


_qqbot_start_lock = threading.Lock()


def start_qqbot(wait=True):
    """用独立 user-data-dir 拉起机器人专用 QQ（不干扰主号）。"""
    if qqbot_pids():
        return True, "机器人 QQ 已在运行"
    with _qqbot_start_lock:
        if qqbot_pids():
            return True, "机器人 QQ 已在运行"
        try:
            subprocess.Popen(
                [QQ_BOT_EXE, "--user-data-dir=" + QQ_BOT_DATA],
                creationflags=NO_WINDOW,
                startupinfo=_startupinfo(),
            )
        except Exception as e:
            return False, "启动失败: %r" % (e,)
        if not wait:
            return True, "已拉起机器人 QQ"
        for _ in range(60):
            if qqbot_pids():
                return True, "机器人 QQ 已启动"
            time.sleep(1)
        return False, "未能拉起机器人 QQ"


def watcher_pid():
    try:
        with open(WATCHER_PID, "r", encoding="utf-8-sig") as f:
            pid = int((f.read().strip() or "0"))
    except Exception:
        return None
    if not pid:
        return None
    r = run_hidden(["tasklist", "/FI", "PID eq %d" % pid, "/NH"])
    return pid if r and str(pid) in (r.stdout or "") else None


# 可选的“宿主程序”探测：config.json 里写 host_process（如 "DeepSeek Harness.exe"），
# 面板概览会显示它是否在跑；不配就恒为 False，不影响其它功能。
HOST_PROCESS = str(_c("host_process") or "")


def dsh_running():
    if not HOST_PROCESS:
        return False
    r = run_hidden(["tasklist", "/FI", "IMAGENAME eq %s" % HOST_PROCESS, "/NH"])
    return bool(r and HOST_PROCESS.lower() in (r.stdout or "").lower())


def tail(path, lines=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 200000))
            buf = f.read().decode("utf-8", "replace")
        return "\n".join(buf.splitlines()[-lines:])
    except Exception as e:
        return "(读取失败: %r)" % (e,)


# ---------------------------------------------------------------- 业务操作
# 二维码：SnowLuma 没有二维码接口，登录只能截 QQ 登录窗口的图。
QR_FILE = QR_CACHE

QQ_LOGIN_FILE = os.path.join(PANEL_DIR, "qq_login.json")


def sl_password():
    try:
        with open(SL_PASS_FILE, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def sl_read_token():
    try:
        with open(SL_TOKEN_FILE, "r", encoding="utf-8-sig") as f:
            return f.read().strip()
    except Exception:
        return ""


def sl_save_token(tok):
    try:
        with open(SL_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok or "")
    except Exception:
        pass


_sl_token = {"v": None, "ts": 0.0}


def _sl_http(path, body, token=None, timeout=20, method=None):
    data = None if body is None else json.dumps(body).encode()
    headers = {}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["x-snowluma-token"] = token
    if method is None:
        method = "POST" if data is not None else "GET"
    req = urllib.request.Request(SL_URL + path, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def sl_login(force=False):
    """登录 SnowLuma WebUI，返回 token（结果缓存）。"""
    if not force and _sl_token["v"] and time.time() - _sl_token["ts"] < 1800:
        return _sl_token["v"]
    pw = sl_password()
    if not pw:
        return None
    try:
        d = _sl_http("/api/login", {"password": pw})
    except Exception as e:
        log("[SnowLuma] 登录失败: %r" % (e,))
        return None
    tok = (d or {}).get("token")
    if tok:
        _sl_token["v"] = tok
        _sl_token["ts"] = time.time()
        sl_save_token(tok)
    return tok


def sl_api(path, body=None, timeout=20, method=None):
    """调 SnowLuma API，返回 (ok, data_or_message)。
    注意：load/unload/refresh 是 POST 且无 body，必须显式传 method="POST"，否则会被当成 GET。"""
    tok = sl_login()
    if not tok:
        return False, "SnowLuma 未启动或 WebUI 登录失败（检查 snowluma_pass_file：%s）" % (SL_PASS_FILE or "未配置")
    try:
        d = _sl_http(path, body, tok, timeout, method)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            tok = sl_login(force=True)
            if not tok:
                return False, "SnowLuma WebUI 认证失败"
            try:
                d = _sl_http(path, body, tok, timeout, method)
            except Exception as e2:
                return False, "连不上 SnowLuma WebUI：%r" % (e2,)
        else:
            try:
                return False, e.read().decode("utf-8", "replace")[:300]
            except Exception:
                return False, "HTTP %d" % e.code
    except Exception as e:
        return False, "连不上 SnowLuma WebUI：%r" % (e,)
    return True, d


def sl_processes():
    ok, d = sl_api("/api/processes")
    if not ok:
        return []
    return (d or {}).get("list") or []


def target_uin():
    """机器人 QQ 号：config.json 里配了 qq_number 就用它；
    没配就从 SnowLuma 当前已注入的实例里取第一个（本面板的常见用法就是只挂一个号）。"""
    if QQ_NUMBER:
        return QQ_NUMBER
    for p in sl_processes():
        u = str(p.get("uin") or "")
        if u and p.get("injected"):
            return u
    return ""


def sl_bot_proc():
    """找到机器人 QQ 实例（按 uin）。"""
    want = target_uin()
    for p in sl_processes():
        if not p.get("injected"):
            continue
        if not want:
            return p
        if str(p.get("uin") or "") == want:
            return p
    return None


def sl_hook_bot():
    """让 SnowLuma 手动 hook 机器人 QQ 实例。"""
    if sl_bot_proc():
        return True, "已连接"
    for p in qqbot_pids():
        ok, d = sl_api("/api/processes/%d/load" % p, None, timeout=120, method="POST")
        if ok:
            return True, "已请求注入机器人 QQ（PID %d）" % p
        msg = str(d)
        if "already" in msg.lower():
            return True, "已在注入中"
        return False, msg
    return False, "没找到机器人 QQ 实例，先启动它"



def qq_login_cfg():
    try:
        with open(QQ_LOGIN_FILE, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        pw = d.get("pw_b64") or ""
        d["password"] = base64.b64decode(pw).decode() if pw else ""
        d.pop("pw_b64", None)
        if not d.get("uin"):
            d["uin"] = target_uin()
        return d
    except Exception:
        return {"uin": target_uin(), "password": "", "remember": False, "auto": False}


def qq_login_cfg_save(d):
    cur = {"uin": d.get("uin") or target_uin(), "remember": bool(d.get("remember")), "auto": bool(d.get("auto"))}
    if cur["remember"] and d.get("password"):
        cur["pw_b64"] = base64.b64encode(d["password"].encode()).decode()
    elif cur["remember"]:
        old = qq_login_cfg()
        if old.get("password"):
            cur["pw_b64"] = base64.b64encode(old["password"].encode()).decode()
    else:
        cur["auto"] = False
    tmp = QQ_LOGIN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    os.replace(tmp, QQ_LOGIN_FILE)
    return {"status": "ok", "message": "已保存", "uin": cur["uin"], "remember": cur["remember"], "auto": cur["auto"]}


def qq_password_login(uin, password):
    return False, "SnowLuma 不支持账密登录，只能用手机 QQ 扫码（二维码在“扫码登录”卡片）"


def qq_quick_login(uin):
    return False, "SnowLuma 不支持快速登录，只能扫码；扫码一次后 QQ 会记住登录，下次开 QQ 通常直接进"


def qq_wait_login(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = qq_state()
        if st.get("isLogin"):
            return True, st
        time.sleep(2)
    return False, qq_state()



def ws_established(port=None):
    """SnowLuma 作为客户端连到 AstrBot 的 OneBot WS；有 ESTABLISHED 说明 QQ 已登录且 WS 通了。"""
    port = int(port or ASTRBOT_WS_PORT)
    r = run_hidden(["netstat", "-ano"])
    n = 0
    for line in (r.stdout or "").splitlines() if r else []:
        if ":%d" % port in line and "ESTABLISHED" in line:
            n += 1
    return n


def bot_nickname():
    """机器人昵称：优先取 SnowLuma 报告的账号昵称。"""
    p = sl_bot_proc()
    if p:
        acc = p.get("account") or {}
        nk = acc.get("nickname") or ""
        if nk:
            return nk
    want = target_uin()
    try:
        ok, d = sl_api("/api/qq-list")
        if ok:
            for it in (d or {}).get("list") or []:
                if want and str(it.get("uin")) == want:
                    return it.get("nickname") or ""
    except Exception:
        pass
    return ""


def qq_state():
    sl_up = port_open(SL_PORT)
    proc = sl_bot_proc() if sl_up else None
    conns = ws_established() if sl_up else 0
    qr_fresh = False
    try:
        qr_fresh = (time.time() - os.path.getmtime(QR_FILE)) < 240
    except Exception:
        pass
    online = bool(proc) and bool(proc.get("loggedIn")) and conns > 0
    # 兜底：SnowLuma 的进程视图可能只认到某一个实例（以前起过双实例就会这样），
    # 但 OneBot 在监听且已经有连到 AstrBot 的 WS，说明账号确实在线。
    if not online and conns > 0 and port_open(ONEBOT_WWW_PORT):
        online = True
    if online:
        state = "在线"
    elif proc and proc.get("injected"):
        state = "等待扫码"
    elif qqbot_pids() and _qq_login_window(qqbot_pids())[0]:
        state = "等待点登录"
    elif qr_fresh:
        state = "等待扫码"
    else:
        state = "未登录"
    nick = ""
    if online:
        nick = bot_nickname() or _nick_cache_read() or "机器人"
        if nick and nick != "机器人":
            _nick_cache_write(nick)
    return {
        "up": sl_up,
        "isLogin": online,
        "nickname": nick,
        "uin": target_uin() if online else "",
        "conns": conns,
        "state": state,
        "qr_fresh": qr_fresh,
        "phase": "" if online else state,
    }


NICK_CACHE = os.path.join(BASE, "data", "bot_nickname.txt")


def _nick_cache_read():
    try:
        with open(NICK_CACHE, encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def _nick_cache_write(nick):
    try:
        with open(NICK_CACHE, "w", encoding="utf-8") as f:
            f.write(nick)
    except Exception:
        pass


def persona_get():
    st, data = ab("POST", "/api/persona/detail", body={"persona_id": PERSONA_ID})
    d = (data or {}).get("data") or {}
    return {
        "persona_id": d.get("persona_id") or PERSONA_ID,
        "system_prompt": d.get("system_prompt") or "",
        "status": (data or {}).get("status"),
        "message": (data or {}).get("message") or "",
    }


def backup_text(name, text):
    safe = re.sub(r"[^0-9A-Za-z_.-]", "_", name)
    path = os.path.join(BACKUP_DIR, "%s-%s.txt" % (safe, time.strftime("%Y%m%d-%H%M%S")))
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    files = sorted(
        (os.path.join(BACKUP_DIR, x) for x in os.listdir(BACKUP_DIR)), key=os.path.getmtime
    )
    for old in files[:-40]:
        try:
            os.remove(old)
        except Exception:
            pass
    return path


def providers_list():
    st, data = ab("GET", "/api/config/provider/list", params={"provider_type": "chat_completion"})
    items = (data or {}).get("data") or []
    out = []
    for p in items:
        keys = p.get("key")
        if isinstance(keys, list):
            keys = keys[0] if keys else ""
        out.append(
            {
                "id": p.get("id"),
                "model": p.get("model"),
                "api_base": p.get("api_base") or "",
                "provider": p.get("provider"),
                "type": p.get("type"),
                "enable": bool(p.get("enable")),
                "key_masked": ("%s***%s" % (keys[:6], keys[-4:])) if keys and len(keys) > 12 else ("已设置" if keys else ""),
                "thinking": _thinking_level(p),
                "raw": p,
            }
        )
    return {"status": (data or {}).get("status"), "items": out, "raw": items}


# ---- 思考强度（reasoning effort）
# deepseek-v4.1-flash 实测：reasoning_effort 接受 minimal/low/medium/high/max，
# 思考链长度随档位明显增长（low≈324 → max≈1336 reasoning tokens）；
# thinking={type:disabled} 可直接关掉思考（reasoning_tokens 归零、响应更快）。
THINKING_LEVELS = ["off", "low", "medium", "high", "max"]
THINKING_LABELS = {
    "off": "关闭思考（最快最省）",
    "low": "低（想一下下）",
    "medium": "中",
    "high": "高",
    "max": "最高（最慢最贵）",
    "": "跟随模型默认",
}


def _thinking_level(prov):
    eb = (prov or {}).get("custom_extra_body") or {}
    if not isinstance(eb, dict):
        return ""
    th = eb.get("thinking")
    if isinstance(th, dict) and str(th.get("type") or "").lower() in ("disabled", "none"):
        return "off"
    eff = eb.get("reasoning_effort")
    if eff:
        return str(eff)
    return ""


def _apply_thinking(cfg, level):
    """把档位写进 provider 的 custom_extra_body（保留其它自定义参数）。"""
    eb = cfg.get("custom_extra_body")
    eb = dict(eb) if isinstance(eb, dict) else {}
    eb.pop("thinking", None)
    eb.pop("reasoning_effort", None)
    eb.pop("enable_thinking", None)
    level = (level or "").strip().lower()
    if level == "off":
        eb["thinking"] = {"type": "disabled"}
    elif level in THINKING_LEVELS:
        eb["reasoning_effort"] = level
    cfg["custom_extra_body"] = eb
    return cfg




def _qq_login_window(pids):
    """在给定 QQ 进程里找“登录窗口”（窄小、带账号头像的那种）。
    返回 (hwnd, rect) 或 (None, None)。"""
    import ctypes
    from ctypes import wintypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    u = ctypes.windll.user32
    want = set(pids)
    cand = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lp):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in want:
            return True
        n = u.GetWindowTextLengthW(hwnd)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, b, n + 1)
            if (b.value or "").strip() != "QQ":
                return True
        else:
            return True
        r = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        # 登录窗口大约 320~560 x 440~760；主面板会大得多
        if 240 <= w <= 620 and 380 <= h <= 820:
            cand.append((hwnd, (r.left, r.top, r.right, r.bottom), w * h))
        return True

    u.EnumWindows(cb, 0)
    if not cand:
        return None, None
    cand.sort(key=lambda x: x[2])
    return cand[0][0], cand[0][1]


def click_qq_login():
    """机器人 QQ 若停在“点登录”界面（保存了账号、没勾自动登录），替它点一下。"""
    import ctypes
    from ctypes import wintypes

    pids = qqbot_pids()
    if not pids:
        return False, "机器人 QQ 没在运行"
    hwnd, rect = _qq_login_window(pids)
    if not hwnd:
        return False, "没找到登录窗口（可能已经在主界面或正在扫码）"
    u = ctypes.windll.user32
    try:
        u.ShowWindow(hwnd, 9)
        u.ShowWindow(hwnd, 5)
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.8)
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return False, "登录窗口尺寸异常"
    x = r.left + int(w * 0.5)
    y = r.top + int(h * 0.72)   # “登录”按钮大致在这一行
    u.SetCursorPos(x, y)
    time.sleep(0.25)
    u.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.06)
    u.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
    time.sleep(0.4)
    return True, "已点“登录”按钮"


def qr_payload(force=False):
    """SnowLuma 没有二维码接口，二维码由 capture_qr() 从 QQ 登录窗口截图得到。"""
    if not os.path.exists(QR_FILE):
        return {"ok": False, "message": "暂无二维码：QQ 已登录，或还没有进入登录流程"}
    age = time.time() - os.path.getmtime(QR_FILE)
    if age > 240 and not force:
        return {"ok": False, "message": "二维码已过期（%.0f 分钟前），点“重新截图”刷新" % (age / 60)}
    return {
        "ok": True,
        "age": int(age),
        "qrcode": "file",
        "message": "二维码截取于 %s" % time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(QR_FILE))),
    }


def qr_png(force=False):
    p = qr_payload(force=force)
    if not p.get("ok"):
        return None
    try:
        with open(QR_FILE, "rb") as f:
            return f.read()
    except Exception:
        return None


# ---------------- 二维码截取（QQ 登录窗口） ----------------
def _qq_win_handles(pids):
    """返回 QQ 主窗口 [(hwnd, title, rect)]。QQ 最小化后 IsWindowVisible 也是 0，
    所以先按标题/尺寸筛，再尝试还原。"""
    import ctypes
    from ctypes import wintypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    u = ctypes.windll.user32
    want = set(pids)
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lp):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in want:
            n = u.GetWindowTextLengthW(hwnd)
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, b, n + 1)
            title = b.value or ""
            r = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            visible = bool(u.IsWindowVisible(hwnd))
            # 主窗口：标题叫 QQ，或者面积够大；排除 IME / 工具栏小窗
            ok = visible and (title == "QQ" or (w > 400 and h > 300 and title))
            if not ok and not visible and title == "QQ" and w >= 100 and h >= 25:
                ok = True  # 最小化或托盘态
            if ok:
                out.append((hwnd, title, (r.left, r.top, r.right, r.bottom)))
        return True

    u.EnumWindows(cb, 0)
    out.sort(key=lambda x: (x[2][2] - x[2][0]) * (x[2][3] - x[2][1]), reverse=True)
    return out


def _grab_hwnd(hwnd):
    """抓某个窗口的内容，返回 (PIL.Image, rect) 或 (None, None)。
    优先 PrintWindow（被遮挡也能抓到），失败再退回屏幕截图。"""
    import ctypes
    from ctypes import wintypes

    from PIL import Image, ImageGrab

    u = ctypes.windll.user32
    g = ctypes.windll.gdi32
    try:
        u.ShowWindow(hwnd, 9)  # SW_RESTORE
        u.ShowWindow(hwnd, 5)  # SW_SHOW
    except Exception:
        pass
    try:
        u.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.7)
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if r.left < -10000 or w < 100 or h < 100:
        return None, None
    box = (r.left, r.top, r.right, r.bottom)

    img = None
    try:
        hdc = u.GetWindowDC(hwnd)
        mdc = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, w, h)
        g.SelectObject(mdc, bmp)
        if u.PrintWindow(hwnd, mdc, 2):
            class _BMI(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]
            bi = _BMI()
            bi.biSize = 40
            bi.biWidth = w
            bi.biHeight = -h
            bi.biPlanes = 1
            bi.biBitCount = 32
            bi.biCompression = 0
            buf = ctypes.create_string_buffer(w * h * 4)
            g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1).convert("RGB")
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(hwnd, hdc)
    except Exception:
        img = None

    if img is None:
        try:
            img = ImageGrab.grab().crop(box)
        except Exception:
            return None, None
    return img, box


def _crop_qr(im):
    """在窗口图里找二维码并裁剪放大存到 QR_CACHE；找到返回提示语，否则 None。"""
    try:
        import cv2
        import numpy as np

        arr = np.array(im.convert("RGB"))[:, :, ::-1].copy()
        det = cv2.QRCodeDetector()
        ok, pts = det.detect(arr)
        if not ok or pts is None:
            return None
        xs = [float(p[0]) for p in pts[0]]
        ys = [float(p[1]) for p in pts[0]]
        pad = 14
        box = (
            max(0, int(min(xs)) - pad),
            max(0, int(min(ys)) - pad),
            min(im.width, int(max(xs)) + pad),
            min(im.height, int(max(ys)) + pad),
        )
        qr = im.crop(box)
        qr = qr.resize((qr.width * 3, qr.height * 3))
        qr.save(QR_CACHE)
        return "已截到登录二维码，用手机 QQ 扫"
    except Exception:
        return None


def capture_qr():
    """把机器人 QQ 窗口截下来，能找到二维码就裁出来存到 QR_CACHE。"""
    pids = qqbot_pids()
    if not pids:
        return False, "没找到机器人 QQ 实例，先点“启动 QQ 并挂载”"
    wins = _qq_win_handles(pids)
    lw, lrect = _qq_login_window(pids)
    if lw:
        im, _b = _grab_hwnd(lw)
        if im is not None and im.width > 100:
            try:
                im.save(QR_CACHE)
                msg = _crop_qr(im)
                if msg:
                    return True, msg
            except Exception:
                pass
    if not wins:
        return False, "机器人 QQ 没有可见窗口（QQ 可能被最小化到托盘）"
    try:
        import numpy as np

        best = None
        for hwnd, title, _rect in wins[:4]:
            im, box = _grab_hwnd(hwnd)
            if im is None:
                continue
            std = float(np.array(im.convert("L")).std())
            if std < 6:   # 纯黑/纯白的空窗口
                continue
            if best is None or std > best[0]:
                best = (std, im, box)
        if best is None:
            return False, "QQ 窗口是空的（可能是隐藏的托盘窗口），先点“显示 QQ 窗口”"
        _std, im, _box = best
        im.save(QR_CACHE)
        msg = _crop_qr(im)
        if msg:
            return True, msg
        return True, "已截 QQ 窗口（没定位到二维码：可能已经登录，或还没到扫码那一步）"
    except Exception as e:
        return False, "截图失败: %r" % (e,)


def qzone_cfg_path():
    return os.path.join(ASTRBOT_DIR, "data", "config", "astrbot_plugin_qzone_config.json")


def qzone_cfg_get():
    try:
        with open(qzone_cfg_path(), "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def qzone_cfg_save(patch):
    cfg = qzone_cfg_get()
    if not cfg:
        return False, "读不到配置文件"
    trig = cfg.setdefault("trigger", {})
    for k, v in patch.items():
        trig[k] = v
    tmp = qzone_cfg_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, qzone_cfg_path())
    return True, "已保存（定时任务改动需重启 AstrBot 生效）"


MEME_PLUGIN = "meme_manager"


def meme_plugin_cfg():
    st, d = ab("GET", "/api/config/get", params={"plugin_name": MEME_PLUGIN}, timeout=30)
    if st != 200 or not isinstance(d, dict):
        return None
    return (d.get("data") or {}).get("config") or {}


def meme_trigger_get():
    cfg = meme_plugin_cfg()
    if cfg is None:
        return None
    g = cfg.get("generation") or {}
    em = g.get("emotion") or {}
    msg = g.get("message") or {}
    sem = cfg.get("semantic") or {}
    return {
        "probability": em.get("probability", 100),
        "max_per_msg": em.get("max_memes_per_message", -1),
        "scope": (g.get("trigger") or {}).get("scope", "only_chat_llm"),
        "emotion_llm": bool((em.get("llm") or {}).get("enabled", False)),
        "mixed": bool(msg.get("enable_mixed", False)),
        "mixed_probability": msg.get("mixed_probability", 50),
        "loose": bool((g.get("matching") or {}).get("enable_loose_matching", False)),
        "semantic": bool(sem.get("enabled", False)),
        "semantic_top_k": sem.get("top_k", 5),
    }


def meme_trigger_save(body):
    cfg = meme_plugin_cfg()
    if not cfg:
        return False, "读不到表情包插件配置"
    g = cfg.setdefault("generation", {})
    em = g.setdefault("emotion", {})
    msg = g.setdefault("message", {})
    trig = g.setdefault("trigger", {})
    mat = g.setdefault("matching", {})
    sem = cfg.setdefault("semantic", {})

    def num(v, dflt, lo, hi):
        try:
            v = type(dflt)(v)
        except Exception:
            return dflt
        return max(lo, min(hi, v))

    if "probability" in body:
        em["probability"] = num(body["probability"], 100, 0, 100)
    if "max_per_msg" in body:
        em["max_memes_per_message"] = num(body["max_per_msg"], -1, -1, 10)
    if "scope" in body and body["scope"] in ("only_chat_llm", "chat_and_plugin_llm"):
        trig["scope"] = body["scope"]
    if "emotion_llm" in body:
        em.setdefault("llm", {})["enabled"] = bool(body["emotion_llm"])
    if "mixed" in body:
        msg["enable_mixed"] = bool(body["mixed"])
    if "mixed_probability" in body:
        msg["mixed_probability"] = num(body["mixed_probability"], 50, 1, 100)
    if "loose" in body:
        mat["enable_loose_matching"] = bool(body["loose"])
    if "semantic" in body:
        sem["enabled"] = bool(body["semantic"])
    if "semantic_top_k" in body:
        sem["top_k"] = num(body["semantic_top_k"], 5, 1, 10)

    st, d = ab("POST", "/api/config/plugin/update?plugin_name=" + MEME_PLUGIN, body=cfg, timeout=60)
    if st != 200:
        return False, "保存失败：%s" % (d,)
    return True, "已保存，插件已热重载"


def memes_all():
    st, data = ab("GET", "/api/plug/meme_manager/emoji", timeout=120)
    cats = data if isinstance(data, dict) and "status" not in data else {}
    st2, emo = ab("GET", "/api/plug/meme_manager/emotions", timeout=60)
    desc = emo if isinstance(emo, dict) and "status" not in emo else {}
    return {
        "categories": cats,
        "descriptions": desc,
        "total": sum(len(v) for v in cats.values()),
    }


# ---------------------------------------------------------------- HTTP 层
ROUTES = {}


def route(path, methods=("GET",)):
    def deco(fn):
        for m in methods:
            ROUTES[(m, path)] = fn
        return fn

    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "QQBotPanel/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---- 工具
    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        try:
            return json.loads(self._body().decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _cookie(self, name):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() == name:
                    return v.strip()
        return ""

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        public = (
            path in ("/login.html", "/favicon.ico")
            or path.startswith("/api/auth/")
        )
        if not public and not session_ok(self._cookie("qp_session")):
            if path.startswith("/api/"):
                return self._json({"status": "error", "message": "需要登录", "need_login": True}, 401)
            return self._send(302, "", "text/plain; charset=utf-8", {"Location": "/login.html"})

        if not path.startswith("/api/"):
            return self._static(path)
        fn = ROUTES.get((method, path))
        if not fn:
            return self._json({"status": "error", "message": "no route %s %s" % (method, path)}, 404)
        try:
            fn(self, qs)
        except Exception as e:
            import traceback

            traceback.print_exc()
            self._json({"status": "error", "message": repr(e)}, 500)

    def _static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        rel = path.lstrip("/").replace("..", "")
        full = os.path.join(WEB_DIR, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            return self._send(404, "not found", "text/plain; charset=utf-8")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


# ---------------- QQ 登录接口（SnowLuma 只能扫码） ----------------
@route("/api/qq/login", ("POST",))
def api_qq_login(h, qs):
    h._json({"status": "error", "message": "SnowLuma 不支持账号密码登录，只能用手机 QQ 扫码（见“扫码登录”卡片）"})


@route("/api/qq/quick", ("POST",))
def api_qq_quick(h, qs):
    h._json({"status": "error", "message": "SnowLuma 不支持快速登录，只能扫码；扫过一次后 QQ 会记住登录"})


@route("/api/qq/accounts")
def api_qq_accounts(h, qs):
    ok, data = sl_api("/api/qq-list")
    items = (data or {}).get("list") or [] if ok else []
    return h._json({"status": "ok" if ok else "error", "list": items, "message": "" if ok else str(data)})


@route("/api/qq/capture", ("POST",))
def api_qq_capture(h, qs):
    ok, m = capture_qr()
    return h._json({"status": "ok" if ok else "error", "message": m})


@route("/api/qq/login-config")
def api_qq_login_cfg(h, qs):
    d = qq_login_cfg()
    return h._json({"status": "ok", "uin": d.get("uin") or target_uin(), "password": d.get("password") or "",
                    "remember": bool(d.get("remember")), "auto": bool(d.get("auto"))})


@route("/api/qq/login-config", ("POST",))
def api_qq_login_cfg_save(h, qs):
    return h._json(qq_login_cfg_save(h._json_body()))


# ---------------- 登录接口 ----------------
@route("/api/auth/login", ("POST",))
def api_auth_login(h, qs):
    d = h._json_body()
    u = (d.get("username") or "").strip()
    if not verify_login(u, d.get("password") or ""):
        time.sleep(0.4)
        return h._json({"status": "error", "message": "账号或密码不对"}, 401)
    remember = bool(d.get("remember"))
    ttl = 30 * 86400 if remember else 12 * 3600
    tok = secrets.token_urlsafe(24)
    _sessions[tok] = (time.time() + ttl, u)
    cookie = "qp_session=%s; Path=/; HttpOnly; SameSite=Lax" % tok
    if remember:
        cookie += "; Max-Age=%d" % ttl
    body = json.dumps({"status": "ok", "message": "登录成功", "username": u}, ensure_ascii=False)
    return h._send(200, body, extra={"Set-Cookie": cookie})


@route("/api/auth/logout", ("POST",))
def api_auth_logout(h, qs):
    tok = h._cookie("qp_session")
    _sessions.pop(tok, None)
    body = json.dumps({"status": "ok", "message": "已退出"}, ensure_ascii=False)
    return h._send(200, body, extra={"Set-Cookie": "qp_session=; Path=/; Max-Age=0"})


@route("/api/auth/whoami")
def api_auth_whoami(h, qs):
    tok = h._cookie("qp_session")
    item = _sessions.get(tok)
    return h._json({"status": "ok", "username": item[1] if item else ""})


# ---- 系统
@route("/api/status")
def api_status(h, qs):
    qq = qq_state()
    h._json(
        {
            "status": "ok",
            "astrbot_up": port_open(ASTRBOT_PORT),
            "sl_up": port_open(SL_PORT),
            "cv2_up": port_open(CV2_PORT),
            "qq": qq,
            "paused": os.path.exists(PAUSE_FLAG),
            "watcher_pid": watcher_pid(),
            "dsh_running": dsh_running(),
            "game_mode": bool(game_cfg().get("enabled")),
            "embed_up": port_open(EMB_PORT),
            "owner_qq": str(_c("owner_qq") or ""),
            # 让前端显示实际端口，配置改了页面也对得上
            "ports": {
                "astrbot": ASTRBOT_PORT,
                "astrbot_ws": ASTRBOT_WS_PORT,
                "snowluma": SL_PORT,
                "onebot": _uport(ONEBOT_WS, 3001),
                "embed": EMB_PORT,
                "cv2": CV2_PORT,
                "gsv": GSV_PORT,
                "fy": FY_PORT,
            },
            "time": time.strftime("%H:%M:%S"),
        }
    )


@route("/api/bot/online", ("POST",))
def api_bot_online(h, qs):
    try:
        if os.path.exists(PAUSE_FLAG):
            os.remove(PAUSE_FLAG)
    except Exception:
        pass
    msgs = []
    if not port_open(SL_PORT):
        ok, m = start_snowluma(wait=False)
        msgs.append(m)
    if not qqbot_pids():
        ok, m = start_qqbot(wait=False)
        msgs.append(m)
        # 等 QQ 进程起来再让它 hook，否则 SnowLuma 找不到实例
        for _ in range(45):
            if qqbot_pids():
                break
            time.sleep(1)
        time.sleep(3)
        ok2, m2 = sl_hook_bot()
        msgs.append(m2)
    elif not sl_bot_proc():
        for _ in range(20):
            if port_open(SL_PORT):
                break
            time.sleep(1)
        ok2, m2 = sl_hook_bot()
        msgs.append(m2)
    ok, m = auto_bring_online(deep=True)
    msgs.append(m)
    h._json({"status": "ok" if ok else "error",
             "message": "；".join([x for x in msgs if x]) or "已请求上线（约 10~20 秒完成）"})


@route("/api/bot/offline", ("POST",))
def api_bot_offline(h, qs):
    try:
        with open(PAUSE_FLAG, "w", encoding="utf-8") as f:
            f.write("manual offline %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass
    kill_pids(qqbot_pids())
    time.sleep(0.8)
    stop_snowluma()
    kill_pids(astrbot_pids())
    time.sleep(1.0)
    h._json(
        {
            "status": "ok",
            "message": "已下线（QQ 与 SnowLuma 都已停）",
            "astrbot_up": port_open(ASTRBOT_PORT),
            "sl_up": port_open(SL_PORT),
        }
    )


@route("/api/bot/restart", ("POST",))
def api_bot_restart(h, qs):
    ok, m = restart_astrbot()
    h._json({"status": "ok" if ok else "error", "message": m})


# ---------------- SnowLuma 控制 ----------------
@route("/api/sl/status")
def api_sl_status(h, qs):
    h._json({"status": "ok", "sl_up": port_open(SL_PORT), "qq_proc": bool(qqbot_pids()),
             "hooked": bool(sl_bot_proc()), "qq": qq_state()})


@route("/api/sl/restart", ("POST",))
def api_sl_restart(h, qs):
    stop_snowluma()
    time.sleep(1)
    ok, m = start_snowluma()
    if not ok:
        return h._json({"status": "error", "message": m})
    if qqbot_pids():
        sl_hook_bot()
        return h._json({"status": "ok", "message": "SnowLuma 已重启，并重新挂上 QQ（若已登录会自动连回）"})
    h._json({"status": "ok", "message": "SnowLuma 已重启，点“启动 QQ”拉起机器人 QQ"})


@route("/api/sl/hook", ("POST",))
def api_sl_hook(h, qs):
    if not port_open(SL_PORT):
        ok, m = start_snowluma(wait=False)
        if not ok:
            return h._json({"status": "error", "message": m})
        for _ in range(60):
            if port_open(SL_PORT):
                break
            time.sleep(1)
    if not qqbot_pids():
        ok, m = start_qqbot(wait=False)
        if not ok:
            return h._json({"status": "error", "message": m})
        for _ in range(45):
            if qqbot_pids():
                break
            time.sleep(1)
        time.sleep(3)
    ok, m = sl_hook_bot()
    h._json({"status": "ok" if ok else "error", "message": m})


@route("/api/sl/click-login", ("POST",))
def api_sl_click_login(h, qs):
    ok, m = click_qq_login()
    h._json({"status": "ok" if ok else "error", "message": m})


@route("/api/sl/show", ("POST",))
def api_sl_show(h, qs):
    """把机器人 QQ 窗口拉到前台（让用户能看到/扫码）。"""
    pids = qqbot_pids()
    if not pids:
        return h._json({"status": "error", "message": "机器人 QQ 没在运行"})
    wins = _qq_win_handles(pids)
    if not wins:
        return h._json({"status": "error", "message": "没找到可见的 QQ 窗口"})
    try:
        import ctypes

        u = ctypes.windll.user32
        for hwnd, _t, _r in wins:
            u.ShowWindow(hwnd, 9)
        u.SetForegroundWindow(wins[0][0])
        return h._json({"status": "ok", "message": "已把 QQ 窗口拉到前台"})
    except Exception as e:
        return h._json({"status": "error", "message": repr(e)})


@route("/api/sl/qr", ("GET", "POST"))
def api_sl_qr(h, qs):
    force = qs.get("force", ["0"])[0] == "1"
    h._json(qr_payload(force=force))


@route("/api/sl/qr.png", ("GET",))
def api_sl_qr_png(h, qs):
    force = qs.get("force", ["0"])[0] == "1"
    buf = qr_png(force=force)
    if not buf:
        return h._send(404, "no qr", "text/plain; charset=utf-8")
    h._send(200, buf, "image/png")


@route("/api/sl/relogin", ("POST",))
def api_sl_relogin(h, qs):
    """重开机器人 QQ 并截图二维码（会让机器人号退出登录，需重新扫码）。"""
    if qq_state().get("isLogin") and not (h._json_body() or {}).get("force"):
        return h._json({"status": "ok", "message": "当前已在线，无需重新登录"})
    try:
        if os.path.exists(QR_FILE):
            os.remove(QR_FILE)
    except Exception:
        pass
    kill_pids(qqbot_pids())
    time.sleep(2)
    ok, m = start_qqbot(wait=False)
    if not ok:
        return h._json({"status": "error", "message": m})
    for _ in range(45):
        if qqbot_pids():
            break
        time.sleep(1)
    time.sleep(4)
    if not port_open(SL_PORT):
        ok2, m2 = start_snowluma(wait=True)
        if not ok2:
            return h._json({"status": "error", "message": m2})
    sl_hook_bot()
    for _ in range(20):
        ok3, m3 = capture_qr()
        if ok3 and os.path.exists(QR_FILE):
            return h._json({"status": "ok", "message": "已截到登录二维码，用手机 QQ 扫下面的码"})
        time.sleep(2)
    h._json({"status": "error", "message": "没能截到二维码，看看机器人 QQ 窗口是不是弹出来了"})


# ---- 人格
@route("/api/persona")
def api_persona(h, qs):
    p = persona_get()
    try:
        with open(PERSONA_MIRROR, "r", encoding="utf-8") as f:
            p["mirror"] = f.read()
    except Exception:
        p["mirror"] = ""
    h._json({"status": "ok", "persona_id": p["persona_id"], "system_prompt": p["system_prompt"]})


@route("/api/persona", ("POST",))
def api_persona_save(h, qs):
    text = (h._json_body().get("system_prompt") or "").strip()
    if not text:
        return h._json({"status": "error", "message": "内容为空"})
    old = persona_get()
    if old.get("system_prompt"):
        backup_text("persona", old["system_prompt"])
    st, data = ab(
        "POST", "/api/persona/update", body={"persona_id": old["persona_id"], "system_prompt": text}
    )
    ok = (data or {}).get("status") == "ok"
    if ok:
        try:
            with open(PERSONA_MIRROR, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
    h._json(
        {
            "status": "ok" if ok else "error",
            "message": (data or {}).get("message") or ("已保存" if ok else "保存失败"),
        }
    )


@route("/api/persona/restore", ("POST",))
def api_persona_restore(h, qs):
    files = sorted(
        [x for x in os.listdir(BACKUP_DIR) if x.startswith("persona-")],
        key=lambda x: os.path.getmtime(os.path.join(BACKUP_DIR, x)),
    )
    if not files:
        return h._json({"status": "error", "message": "没有备份"})
    with open(os.path.join(BACKUP_DIR, files[-1]), "r", encoding="utf-8") as f:
        text = f.read()
    old = persona_get()
    if old.get("system_prompt"):
        backup_text("persona", old["system_prompt"])
    st, data = ab(
        "POST", "/api/persona/update", body={"persona_id": old["persona_id"], "system_prompt": text}
    )
    ok = (data or {}).get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": "已回滚到 %s" % files[-1], "system_prompt": text})


# ---- 模型
@route("/api/providers")
def api_providers(h, qs):
    h._json({"status": "ok", **providers_list()})


@route("/api/providers/active", ("POST",))
def api_providers_active(h, qs):
    pid = h._json_body().get("id")
    st, data = ab("GET", "/api/config/provider/list", params={"provider_type": "chat_completion"})
    items = (data or {}).get("data") or []
    target = None
    for p in items:
        if p.get("id") == pid:
            target = p
    if not target:
        return h._json({"status": "error", "message": "没有这个模型"})
    msgs = []
    for p in items:
        want = p.get("id") == pid
        if bool(p.get("enable")) != want:
            cfg = dict(p)
            cfg["enable"] = want
            st2, d2 = ab("POST", "/api/config/provider/update", body={"id": p.get("id"), "config": cfg})
            if (d2 or {}).get("status") != "ok":
                msgs.append("%s 更新失败: %s" % (p.get("id"), (d2 or {}).get("message")))
    h._json({"status": "ok", "message": "已切换到 %s %s" % (pid, "；".join(msgs))})


@route("/api/providers/save", ("POST",))
def api_providers_save(h, qs):
    body = h._json_body()
    pid = body.get("id")
    st, data = ab("GET", "/api/config/provider/list", params={"provider_type": "chat_completion"})
    items = (data or {}).get("data") or []
    target = next((p for p in items if p.get("id") == pid), None)
    if not target:
        return h._json({"status": "error", "message": "没有这个模型"})
    cfg = dict(target)
    if body.get("model"):
        cfg["model"] = body["model"].strip()
    if body.get("api_base") is not None:
        cfg["api_base"] = body["api_base"].strip()
    if body.get("key"):
        cfg["key"] = [body["key"].strip()]
    st2, d2 = ab("POST", "/api/config/provider/update", body={"id": pid, "config": cfg})
    ok = (d2 or {}).get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": (d2 or {}).get("message") or "已保存"})


@route("/api/providers/thinking", ("POST",))
def api_providers_thinking(h, qs):
    body = h._json_body()
    pid = (body.get("id") or "").strip()
    level = (body.get("level") if body.get("level") is not None else "").strip().lower()
    if level and level not in THINKING_LEVELS:
        return h._json({"status": "error", "message": "档位只能是 off/low/medium/high/max 或留空"})
    st, data = ab("GET", "/api/config/provider/list", params={"provider_type": "chat_completion"})
    items = (data or {}).get("data") or []
    target = next((p for p in items if p.get("id") == pid), None)
    if not target:
        return h._json({"status": "error", "message": "没有这个模型"})
    cfg = _apply_thinking(dict(target), level)
    st2, d2 = ab("POST", "/api/config/provider/update", body={"id": pid, "config": cfg})
    ok = (d2 or {}).get("status") == "ok"
    h._json({
        "status": "ok" if ok else "error",
        "message": (d2 or {}).get("message") or "已保存",
        "thinking": _thinking_level(cfg),
    })
# ---- 表情包
# ---------- 表情自动收录 ----------
MEME_AUTOSAVE_CFG = os.path.join(BASE, "data", "meme_autosave.json")
MEME_AUTOSAVE_INDEX = os.path.join(BASE, "data", "meme_autosave_index.json")
MEME_AUTOSAVE_DEFAULTS = {
    "enabled": True,
    # 只收录这些 QQ 号发的表情（留空 = 谁发都收）；面板里可改
    "owners": [str(_c("owner_qq") or "")] if _c("owner_qq") else [],
    "category_mode": "auto",
    "fixed_category": "自存",
    "max_per_message": 1,
}


def meme_autosave_get():
    cfg = dict(MEME_AUTOSAVE_DEFAULTS)
    try:
        with open(MEME_AUTOSAVE_CFG, encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    recent = []
    try:
        with open(MEME_AUTOSAVE_INDEX, encoding="utf-8") as f:
            recent = (json.load(f) or [])[:20]
    except Exception:
        pass
    return {"config": cfg, "recent": recent, "total": len(recent)}


def meme_autosave_save(body):
    cfg = meme_autosave_get()["config"]
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "owners" in body:
        raw = body["owners"]
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace("，", ",").split(",")]
        cfg["owners"] = [str(x).strip() for x in (raw or []) if str(x).strip()]
    if body.get("category_mode") in ("auto", "fixed"):
        cfg["category_mode"] = body["category_mode"]
    if "fixed_category" in body:
        cfg["fixed_category"] = str(body["fixed_category"]).strip() or "自存"
    if "max_per_message" in body:
        try:
            cfg["max_per_message"] = max(1, min(4, int(body["max_per_message"])))
        except Exception:
            pass
    tmp = MEME_AUTOSAVE_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEME_AUTOSAVE_CFG)
    return True


@route("/api/meme-autosave")
def api_meme_autosave(h, qs):
    h._json({"status": "ok", **meme_autosave_get()})


@route("/api/meme-autosave", ("POST",))
def api_meme_autosave_save(h, qs):
    meme_autosave_save(h._json_body())
    h._json({"status": "ok", "message": "已保存，立即生效"})


@route("/api/memes/trigger")
def api_memes_trigger(h, qs):
    d = meme_trigger_get()
    if d is None:
        return h._json({"status": "error", "message": "读不到插件配置"})
    h._json({"status": "ok", **d})


@route("/api/memes/trigger", ("POST",))
def api_memes_trigger_save(h, qs):
    ok, msg = meme_trigger_save(h._json_body())
    h._json({"status": "ok" if ok else "error", "message": msg})


@route("/api/memes")
def api_memes(h, qs):
    h._json({"status": "ok", **memes_all()})


@route("/api/memes/img")
def api_memes_img(h, qs):
    cat = qs.get("category", [""])[0]
    fn = qs.get("filename", [""])[0]
    st, buf = ab(
        "GET",
        "/api/plug/meme_manager/meme_image",
        params={"category": cat, "filename": fn},
        raw=True,
        timeout=30,
    )
    if st != 200 or not isinstance(buf, bytes):
        return h._send(404, "img", "text/plain; charset=utf-8")
    ctype = "image/gif" if fn.lower().endswith(".gif") else ("image/png" if fn.lower().endswith(".png") else "image/jpeg")
    h._send(200, buf, ctype, {"Cache-Control": "max-age=600"})


@route("/api/memes/upload", ("POST",))
def api_memes_upload(h, qs):
    cat = qs.get("category", [""])[0]
    if not cat:
        return h._json({"status": "error", "message": "缺少分类"})
    raw = h._body()
    ctype = h.headers.get("Content-Type") or ""
    tok = ab_login()
    if not tok:
        return h._json({"status": "error", "message": "AstrBot 未启动"})
    req = urllib.request.Request(
        ASTRBOT_URL + "/api/plug/meme_manager/emoji/add/" + urllib.parse.quote(cat),
        data=raw,
        headers={"Authorization": "Bearer " + tok, "Content-Type": ctype},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            out = r.read()
            try:
                h._json(json.loads(out.decode("utf-8", "replace")))
            except Exception:
                h._send(r.status, out, "application/json; charset=utf-8")
    except urllib.error.HTTPError as e:
        h._send(e.code, e.read(), "application/json; charset=utf-8")
    except Exception as e:
        h._json({"status": "error", "message": repr(e)})


@route("/api/memes/delete", ("POST",))
def api_memes_delete(h, qs):
    b = h._json_body()
    fn = b.get("image_file") or b.get("filename")
    st, d = ab(
        "POST",
        "/api/plug/meme_manager/emoji/delete",
        body={"category": b.get("category"), "image_file": fn},
    )
    ok = st == 200 and "不能为空" not in json.dumps(d, ensure_ascii=False)
    msg = ""
    if isinstance(d, dict):
        msg = d.get("message") or ""
    h._json({"status": "ok" if ok else "error", "message": msg or ("已删除" if ok else "删除失败")})


@route("/api/memes/category", ("POST",))
def api_memes_category(h, qs):
    b = h._json_body()
    cat = (b.get("category") or "").strip()
    desc = (b.get("description") or "").strip()
    if not cat:
        return h._json({"status": "error", "message": "缺少分类名"})
    st, d = ab("POST", "/api/plug/meme_manager/category/restore", body={"category": cat, "description": desc})
    ok = (d or {}).get("status") == "ok" or st == 200
    if ok and desc:
        st2, d2 = ab(
            "POST",
            "/api/plug/meme_manager/category/update_description",
            body={"category": cat, "description": desc},
        )
        ok = (d2 or {}).get("status") == "ok" or st2 == 200
    h._json({"status": "ok" if ok else "error", "message": "已保存分类 %s" % cat if ok else str(d)[:200]})


@route("/api/memes/category/delete", ("POST",))
def api_memes_category_delete(h, qs):
    b = h._json_body()
    st, d = ab("POST", "/api/plug/meme_manager/category/delete", body={"category": b.get("category")})
    h._json(d if isinstance(d, dict) else {"status": "ok"})


# ---- QQ 空间
@route("/api/qzone/status")
def api_qzone_status(h, qs):
    st, d = ab("GET", "/api/plug/astrbot_plugin_qzone/page/status")
    h._json(d if isinstance(d, dict) else {"status": "error", "message": str(d)[:200]})


@route("/api/qzone/feed")
def api_qzone_feed(h, qs):
    page = qs.get("page", ["1"])[0]
    st, d = ab("GET", "/api/plug/astrbot_plugin_qzone/page/feed", params={"page": page})
    h._json(d if isinstance(d, dict) else {"status": "error", "message": str(d)[:200]})


@route("/api/qzone/publish", ("POST",))
def api_qzone_publish(h, qs):
    text = (h._json_body().get("text") or "").strip()
    if not text:
        return h._json({"status": "error", "message": "内容为空"})
    st, d = ab("POST", "/api/plug/astrbot_plugin_qzone/page/publish", body={"text": text})
    h._json(d if isinstance(d, dict) else {"status": "ok"})


@route("/api/qzone/config")
def api_qzone_config(h, qs):
    cfg = qzone_cfg_get()
    h._json({"status": "ok", "trigger": cfg.get("trigger", {})})


@route("/api/qzone/config", ("POST",))
def api_qzone_config_save(h, qs):
    ok, msg = qzone_cfg_save(h._json_body())
    h._json({"status": "ok" if ok else "error", "message": msg})



# ---- 用量明细（直接读 AstrBot 的 provider_stats 表）
STATS_DB = os.path.join(ASTRBOT_DIR, "data", "data_v4.db")


def stats_db_query(sql, params=()):
    """只读查询 provider_stats；库不存在或无表时返回 []。"""
    import sqlite3

    if not os.path.exists(STATS_DB):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STATS_DB.replace("\\", "/"), uri=True, timeout=10)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()
    except Exception:
        return []


@route("/api/tokens/detail")
def api_tokens_detail(h, qs):
    """命中/未命中/输出三类 token 的实际拆分。"""
    try:
        days = int(qs.get("days", ["1"])[0])
    except Exception:
        days = 1
    if days not in (1, 3, 7, 30):
        days = 1
    # 与 AstrBot 面板口径一致：本地时间 now-days 后取整到整点，再转 UTC 比较
    now_local = datetime.datetime.now().astimezone()
    cutoff_local = (now_local - datetime.timedelta(days=days)).replace(
        minute=0, second=0, microsecond=0
    )
    cutoff_utc = cutoff_local.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    where = "created_at >= '%s' and agent_type = 'internal'" % cutoff_utc
    agg = stats_db_query(
        "select count(*), coalesce(sum(token_input_cached),0), "
        "coalesce(sum(token_input_other),0), coalesce(sum(token_output),0) "
        "from provider_stats where " + where
    )
    n, cached, other, output = (agg[0] if agg else (0, 0, 0, 0))
    input_total = int(cached) + int(other)
    # 按天
    byday = stats_db_query(
        "select substr(datetime(created_at,'localtime'),1,10), coalesce(sum(token_input_cached),0), "
        "coalesce(sum(token_input_other),0), coalesce(sum(token_output),0), count(*) "
        "from provider_stats where " + where + " group by 1 order by 1"
    )
    # 按会话
    byumo = stats_db_query(
        "select umo, coalesce(sum(token_input_cached),0), coalesce(sum(token_input_other),0), "
        "coalesce(sum(token_output),0), count(*) from provider_stats where " + where +
        " group by 1 order by (coalesce(sum(token_input_cached),0)+coalesce(sum(token_input_other),0)) desc limit 12"
    )
    h._json({
        "status": "ok",
        "days": days,
        "calls": n,
        "hit": int(cached),
        "miss": int(other),
        "output": int(output),
        "input_total": input_total,
        "hit_rate": (float(cached) / input_total) if input_total else None,
        "by_day": [
            {"date": r[0], "hit": int(r[1]), "miss": int(r[2]), "output": int(r[3]), "calls": r[4]}
            for r in byday
        ],
        "by_umo": [
            {"umo": r[0], "hit": int(r[1]), "miss": int(r[2]), "output": int(r[3]), "calls": r[4]}
            for r in byumo
        ],
    })


# ---- 用量统计
@route("/api/tokens")
def api_tokens(h, qs):
    try:
        days = int(qs.get("days", ["1"])[0])
    except Exception:
        days = 1
    if days not in (1, 3, 7):
        days = 1
    st, d = ab("GET", "/api/stat/provider-tokens", params={"days": days}, timeout=90)
    data = (d or {}).get("data") or {}
    st2, d2 = ab("GET", "/api/stat/get", timeout=30)
    stat = ((d2 or {}).get("data") or {}) if isinstance(d2, dict) else {}
    series = (data.get("trend") or {}).get("total_series") or []
    provs = data.get("range_by_provider") or []
    h._json(
        {
            "status": "ok" if st == 200 else "error",
            "days": days,
            "total_tokens": data.get("range_total_tokens") or 0,
            "calls": data.get("range_total_calls") or 0,
            "avg_ms": round(data.get("range_avg_duration_ms") or 0),
            "avg_tpm": round(data.get("range_avg_tpm") or 0),
            "success_rate": data.get("range_success_rate"),
            "ttft_ms": round(data.get("range_avg_ttft_ms") or 0),
            "providers": provs,
            "series": series,
            "message_count": stat.get("message_count"),
            "platform_count": stat.get("platform_count"),
            "plugin_count": stat.get("plugin_count"),
        }
    )


# ---- 参数调整
PARAM_SPECS = [
    {"path": "provider_settings.enable", "label": "模型总开关", "type": "bool", "hint": "关掉后机器人不再调用大模型"},
    {"path": "wake_prefix", "label": "全局唤醒词（多个用逗号分隔）", "type": "list", "hint": "留空表示群里需要 @ 才回；填了就必须以它开头"},
    {"path": "platform_settings.segmented_reply.enable", "label": "分条回复", "type": "bool", "hint": "把长回复拆成几条挨个发"},
    {"path": "platform_settings.segmented_reply.interval", "label": "分条间隔（秒）", "type": "str", "hint": "如 0.3,0.8 表示在区间内随机"},
    {"path": "platform_settings.segmented_reply.words_count_threshold", "label": "超过多少字才分条", "type": "int"},
    {"path": "platform_settings.forward_threshold", "label": "长消息合并转发阈值", "type": "int", "hint": "超过这个字数合并转发，0=不转"},
    {"path": "platform_settings.reply_with_mention", "label": "回复时 @ 对方", "type": "bool"},
    {"path": "platform_settings.reply_with_quote", "label": "回复时引用原消息", "type": "bool"},
    {"path": "platform_settings.unique_session", "label": "群内按人独立会话", "type": "bool"},
    {"path": "platform_settings.rate_limit.count", "label": "限流：窗口内最多条数", "type": "int"},
    {"path": "platform_settings.rate_limit.time", "label": "限流：窗口秒数", "type": "int"},
    {"path": "provider_settings.web_search", "label": "允许联网搜索", "type": "bool"},
    {"path": "log_level", "label": "日志级别", "type": "choice", "choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
]

PLUGIN_LIST = [
    "meme_manager",
    "astrbot_plugin_qzone",
    "astrbot_plugin_group_chat_plus",
    "astrbot_plugin_no_blank_line",
    "astrbot_plugin_qqprofile",
    "astrbot_plugin_parser",
    "astrbot_plugin_pokepro",
    "astrbot_plugin_msg_merge",
]

CONF_ID = "default"


def cfg_get(plugin=None):
    params = {"plugin_name": plugin} if plugin else None
    st, d = ab("GET", "/api/config/get", params=params, timeout=90)
    return ((d or {}).get("data") or {}).get("config") or {}


def dpick(d, path):
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def dset(d, path, val):
    ks = path.split(".")
    cur = d
    for k in ks[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[ks[-1]] = val


@route("/api/params")
def api_params(h, qs):
    cfg = cfg_get()
    items = []
    for sp in PARAM_SPECS:
        it = dict(sp)
        it["value"] = dpick(cfg, sp["path"])
        items.append(it)
    h._json({"status": "ok", "items": items, "plugins": PLUGIN_LIST})


@route("/api/params", ("POST",))
def api_params_save(h, qs):
    body = h._json_body()
    values = body.get("values") or {}
    if not values:
        return h._json({"status": "error", "message": "没有要改的参数"})
    cfg = cfg_get()
    for path, val in values.items():
        dset(cfg, path, val)
    st, d = ab(
        "POST", "/api/config/astrbot/update", body={"conf_id": CONF_ID, "config": cfg}, timeout=150
    )
    ok = st == 200 and (d or {}).get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": (d or {}).get("message") or ("已保存" if ok else "保存失败")})


@route("/api/plugin-config")
def api_plugin_config(h, qs):
    name = qs.get("name", [""])[0]
    if name not in PLUGIN_LIST:
        return h._json({"status": "error", "message": "未知插件"})
    cfg = cfg_get(name)
    h._json({"status": "ok", "name": name, "config": cfg})


@route("/api/plugin-config", ("POST",))
def api_plugin_config_save(h, qs):
    name = qs.get("name", [""])[0]
    if name not in PLUGIN_LIST:
        return h._json({"status": "error", "message": "未知插件"})
    body = h._json_body()
    config = body.get("config")
    if not isinstance(config, dict):
        return h._json({"status": "error", "message": "配置格式不对"})
    st, d = ab(
        "POST",
        "/api/config/plugin/update",
        params={"plugin_name": name},
        body=config,
        timeout=150,
    )
    ok = st == 200 and (d or {}).get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": (d or {}).get("message") or ("已保存" if ok else "保存失败")})


# ---- QQ 资料 / 解析器 / 戳一戳 快捷控制
@route("/api/qqprofile")
def api_qqprofile(h, qs):
    info = ob("get_login_info")
    d = info.get("data") or {}
    uin = str(d.get("user_id") or "") or target_uin()
    prof = ob("get_stranger_info", {"user_id": uin, "no_cache": True}) if uin else {}
    pd = prof.get("data") or {}
    h._json({
        "status": "ok",
        "uin": uin,
        "nickname": d.get("nickname") or "",
        "signature": pd.get("long_nick") or pd.get("longNick") or pd.get("sign") or "",
        "level": pd.get("qqLevel"),
    })


@route("/api/qqprofile/set", ("POST",))
def api_qqprofile_set(h, qs):
    body = h._json_body()
    acts = []
    nickname = (body.get("nickname") or "").strip()
    signature = (body.get("signature") or "").strip()
    status = body.get("status")
    if nickname:
        acts.append(("set_qq_profile", {"nickname": nickname}))
    if signature:
        acts.append(("set_qq_profile", {"personal_note": signature}))
    if status not in (None, ""):
        acts.append(("set_online_status", {"status": int(status), "ext_status": 0, "battery_status": 0}))
    if not acts:
        return h._json({"status": "error", "message": "没有要改的内容"})
    msgs = []
    ok = True
    for act, params in acts:
        r = ob(act, params, timeout=25)
        if r.get("status") != "ok":
            ok = False
            msgs.append("%s 失败：%s" % (act, r.get("message") or r.get("retcode")))
    if not status and len(acts) > 1:
        pass
    h._json({
        "status": "ok" if ok else "error",
        "message": "已修改" if ok else "；".join(msgs),
        "note": "签名/昵称由 QQ 服务端同步，客户端可能有几秒到几十秒延迟；在线状态若被手机端顶回属正常",
    })


@route("/api/pokepro/poke", ("POST",))
def api_pokepro_poke(h, qs):
    body = h._json_body()
    target = str(body.get("user_id") or "").strip()
    times = int(body.get("times") or 1)
    group = str(body.get("group_id") or "").strip()
    if not target.isdigit():
        return h._json({"status": "error", "message": "user_id 必须是 QQ 号"})
    times = max(1, min(times, 10))
    params = {"user_id": int(target)}
    if group.isdigit():
        params["group_id"] = int(group)
    r = ob("send_poke", params, timeout=25)
    ok = r.get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": "已戳 %s" % target if ok else (r.get("message") or "失败")})


# ---- 群管理（走 SnowLuma 的 OneBot v11 websocket）


def ob(action, params=None, timeout=20):
    try:
        from websockets.sync.client import connect
    except Exception:
        return {"status": "error", "message": "缺少 websockets 依赖"}
    try:
        with connect(ONEBOT_WS, open_timeout=8, close_timeout=2) as ws:
            echo = "p%d" % int(time.time() * 1000)
            ws.send(
                json.dumps({"action": action, "params": params or {}, "echo": echo}, ensure_ascii=False)
            )
            end = time.time() + timeout
            while time.time() < end:
                left = max(0.5, end - time.time())
                msg = json.loads(ws.recv(timeout=left))
                if str(msg.get("echo")) == echo:
                    return msg
            return {"status": "error", "message": "SnowLuma 响应超时"}
    except Exception as e:
        return {"status": "error", "message": "连不上 SnowLuma(%d)：%r" % (_uport(ONEBOT_WS, 3001), e)}


@route("/api/groups")
def api_groups(h, qs):
    r = ob("get_group_list")
    groups = r.get("data") or []
    me = ob("get_login_info", timeout=8).get("data") or {}
    bot = str(me.get("user_id") or "") or target_uin()
    out = []
    for g in groups:
        gid = g.get("group_id")
        role = ""
        if bot:
            info = ob(
                "get_group_member_info",
                {"group_id": gid, "user_id": int(bot), "no_cache": False},
                timeout=8,
            )
            if isinstance(info.get("data"), dict):
                role = info["data"].get("role") or ""
        out.append(
            {
                "group_id": gid,
                "group_name": g.get("group_name"),
                "member_count": g.get("member_count"),
                "max_member_count": g.get("max_member_count"),
                "all_shut": bool(g.get("group_all_shut")),
                "bot_role": role,
            }
        )
    out.sort(key=lambda x: -(x.get("member_count") or 0))
    h._json({"status": r.get("status", "ok"), "groups": out, "message": r.get("message") or ""})


@route("/api/groups/members")
def api_group_members(h, qs):
    gid = qs.get("gid", [""])[0]
    if not gid:
        return h._json({"status": "error", "message": "缺少群号"})
    r = ob("get_group_member_list", {"group_id": int(gid), "no_cache": False}, timeout=30)
    members = r.get("data") or []
    members.sort(key=lambda m: ({"owner": 0, "admin": 1}.get(m.get("role"), 2), -(m.get("last_sent_time") or 0)))
    h._json({"status": r.get("status", "ok"), "members": members})


GROUP_SETTINGS_PATH = os.path.join(BASE, "qbot_group_settings.json")


def _gs_read():
    try:
        with open(GROUP_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _gs_write(d):
    tmp = GROUP_SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, GROUP_SETTINGS_PATH)


@route("/api/groups/settings")
def api_group_settings(h, qs):
    cfg = _gs_read()
    defaults = {"reply_prob": 0.3, "context_messages": 20, "per_minute": 0}
    try:
        _st, rc = ab("GET", "/api/config/get", {"plugin_name": "astrbot_plugin_group_chat_plus"})
        pc = (rc.get("data") or {}).get("config") or {}
        if "initial_probability" in pc:
            defaults["reply_prob"] = pc.get("initial_probability")
        if "max_context_messages" in pc:
            defaults["context_messages"] = pc.get("max_context_messages")
    except Exception:
        pass
    h._json({"status": "ok", "settings": cfg, "defaults": defaults})


@route("/api/groups/settings", ("POST",))
def api_group_settings_save(h, qs):
    b = h._json_body()
    gid = str(b.get("group_id") or "").strip()
    if not gid:
        return h._json({"status": "error", "message": "缺少群号"})
    cfg = _gs_read()
    item = cfg.get(gid) or {}
    for key in ("reply_prob", "context_messages", "per_minute"):
        if key in b:
            raw = b.get(key)
            if raw in (None, ""):
                item.pop(key, None)
                continue
            try:
                val = float(raw) if key == "reply_prob" else int(raw)
            except (TypeError, ValueError):
                return h._json({"status": "error", "message": "参数格式不对"})
            if key == "reply_prob" and not (0.0 <= val <= 1.0):
                return h._json({"status": "error", "message": "接话概率要在 0~1 之间"})
            if key == "context_messages" and val < 0:
                return h._json({"status": "error", "message": "上下文条数不能为负"})
            if key == "per_minute" and val < 0:
                return h._json({"status": "error", "message": "每分钟限量不能为负"})
            item[key] = val
    if item:
        cfg[gid] = item
    else:
        cfg.pop(gid, None)
    _gs_write(cfg)
    h._json({"status": "ok", "message": "已保存，下一条消息起生效", "settings": cfg})


@route("/api/groups/send", ("POST",))
def api_group_send(h, qs):
    b = h._json_body()
    gid = int(b.get("gid") or 0)
    text = (b.get("text") or "").strip()
    at = b.get("at")
    if not gid or not text:
        return h._json({"status": "error", "message": "群号或内容为空"})
    segs = []
    if at:
        segs.append({"type": "at", "data": {"qq": str(at)}})
        segs.append({"type": "text", "data": {"text": " "}})
    segs.append({"type": "text", "data": {"text": text}})
    r = ob("send_group_msg", {"group_id": gid, "message": segs}, timeout=30)
    ok = r.get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": "已发送" if ok else str(r.get("message") or r)[:200]})


@route("/api/groups/notice", ("POST",))
def api_group_notice(h, qs):
    b = h._json_body()
    gid = int(b.get("gid") or 0)
    text = (b.get("text") or "").strip()
    if not gid or not text:
        return h._json({"status": "error", "message": "群号或内容为空"})
    r = ob("_send_group_notice", {"group_id": gid, "content": text}, timeout=30)
    if r.get("status") != "ok":
        r = ob("send_group_notice", {"group_id": gid, "content": text}, timeout=30)
    ok = r.get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": "公告已发布" if ok else str(r.get("message") or r)[:200]})


@route("/api/groups/ban", ("POST",))
def api_group_ban(h, qs):
    b = h._json_body()
    gid = int(b.get("gid") or 0)
    uid = b.get("user")
    dur = int(b.get("duration") or 0)
    if not gid:
        return h._json({"status": "error", "message": "缺少群号"})
    if uid:
        r = ob("set_group_ban", {"group_id": gid, "user_id": int(uid), "duration": dur}, timeout=20)
        msg = "已禁言" if dur else "已解除禁言"
    else:
        r = ob("set_group_whole_ban", {"group_id": gid, "enable": bool(dur)}, timeout=20)
        msg = "已开启全员禁言" if dur else "已解除全员禁言"
    ok = r.get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": msg if ok else str(r.get("message") or r)[:200]})


@route("/api/groups/kick", ("POST",))
def api_group_kick(h, qs):
    b = h._json_body()
    gid = int(b.get("gid") or 0)
    uid = b.get("user")
    if not gid or not uid:
        return h._json({"status": "error", "message": "缺少群号或 QQ"})
    r = ob(
        "set_group_kick",
        {"group_id": gid, "user_id": int(uid), "reject_add_request": bool(b.get("reject"))},
        timeout=20,
    )
    ok = r.get("status") == "ok"
    h._json({"status": "ok" if ok else "error", "message": "已踢出" if ok else str(r.get("message") or r)[:200]})


# ---- 日志
# ---------------- 主动聊天 ----------------
PROACTIVE_CFG = os.path.join(BASE, "data", "proactive.json")
PROACTIVE_STATE = os.path.join(BASE, "data", "proactive_state.json")

PROACTIVE_DEFAULTS = {
    "enabled": False,
    # 主动聊天的目标会话；格式 <协议>:<消息类型>:<QQ号>，面板里可增删
    "targets": (["napcat:FriendMessage:" + str(_c("owner_qq"))] if _c("owner_qq") else []),
    "random_min_minutes": 180,
    "random_max_minutes": 480,
    "probability": 0.7,
    "quiet_start": 0,
    "quiet_end": 8,
    "daily_limit": 5,
    "idle_minutes": 45,
    "context_messages": 30,
    "style_hint": "",
    "meme_enable": True,
    "meme_probability": 0.5,
    "meme_categories": ["自拍", "happy", "shy", "meow"],
    "memory_write": True,
    "source_mode": "conversation_history",
    "platform_history_count": 30,
    "max_unanswered": 3,
    "resume_schedule": True,
    "line_mode": "split",
    "split_delay": 1.2,
    "join_punct": "，",
}

LIVINGMEM_PAGE = ASTRBOT_URL + "/#/plugin-view/astrbot_plugin_livingmemory/dashboard"


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return json.loads(json.dumps(default))


def _write_json(path, data):
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def proactive_cfg():
    cfg = dict(PROACTIVE_DEFAULTS)
    cfg.update(_read_json(PROACTIVE_CFG, {}))
    return cfg


@route("/api/proactive")
def api_proactive_get(h, qs):
    cfg = proactive_cfg()
    state = _read_json(PROACTIVE_STATE, {})
    extra = {}
    st, d = ab("GET", "/api/plug/astrbot_plugin_proactive/page/state", timeout=15)
    if st == 200 and isinstance(d, dict):
        extra = d
    h._json({"status": "ok", "config": cfg, "state": state, "live": extra})


@route("/api/proactive", ("POST",))
def api_proactive_save(h, qs):
    body = h._json_body()
    if not isinstance(body, dict) or not body:
        return h._json({"status": "error", "message": "请求体不是合法 JSON（前端应把对象 JSON.stringify 后发送）"})
    cfg = proactive_cfg()
    for key in PROACTIVE_DEFAULTS:
        if key not in body:
            continue
        val = body[key]
        if key in ("targets", "meme_categories"):
            if isinstance(val, str):
                val = [x.strip() for x in val.replace("，", ",").split(",") if x.strip()]
            if not isinstance(val, list):
                return h._json({"status": "error", "message": "%s 格式不对" % key})
            if key == "targets" and not val:
                return h._json({"status": "error", "message": "至少要有一个目标会话"})
            cfg[key] = val
        elif key in ("enabled", "meme_enable", "memory_write", "resume_schedule"):
            cfg[key] = bool(val)
        elif key == "source_mode":
            v = str(val or "").strip()
            if v not in (
                "conversation_history",
                "platform_message_history",
                "hybrid",
            ):
                return h._json({"status": "error", "message": "上下文来源取值不对"})
            cfg[key] = v
        elif key == "style_hint":
            cfg[key] = str(val or "")[:500]
        elif key == "line_mode":
            v = str(val or "").strip().lower()
            if v not in ("split", "punct", "space"):
                return h._json({"status": "error", "message": "分行处理取值不对"})
            cfg[key] = v
        elif key == "join_punct":
            cfg[key] = str(val or "，")[:4] or "，"
        else:
            try:
                iv = float(val)
            except Exception:
                return h._json({"status": "error", "message": "%s 不是数字" % key})
            if key in ("quiet_start", "quiet_end"):
                iv = int(iv)
                if iv < 0 or iv > 23:
                    return h._json({"status": "error", "message": "安静时段要在 0~23 点"})
            elif key in ("probability", "meme_probability"):
                iv = max(0.0, min(1.0, iv))
            elif key == "daily_limit":
                iv = int(iv)
                if iv < 0:
                    return h._json({"status": "error", "message": "每日上限不能为负"})
            elif key == "context_messages":
                iv = int(iv)
                if iv < 4:
                    return h._json({"status": "error", "message": "上下文至少 4 条"})
            elif key == "max_unanswered":
                iv = int(iv)
                if iv < 0:
                    return h._json({"status": "error", "message": "未回复上限不能为负"})
            elif key == "split_delay":
                iv = round(max(0.2, min(10.0, iv)), 1)
            elif key == "platform_history_count":
                iv = int(iv)
                if iv < 4 or iv > 200:
                    return h._json({"status": "error", "message": "平台历史条数要在 4~200"})
            else:
                iv = int(round(iv))
                if iv < 1:
                    return h._json({"status": "error", "message": "%s 至少 1" % key})
            cfg[key] = iv
    if cfg["random_max_minutes"] < cfg["random_min_minutes"]:
        cfg["random_max_minutes"] = cfg["random_min_minutes"]
    mode = str(cfg.get("source_mode") or "")
    if mode not in ("conversation_history", "platform_message_history", "hybrid"):
        cfg["source_mode"] = "conversation_history"
    _write_json(PROACTIVE_CFG, cfg)
    h._json({"status": "ok", "message": "已保存，立即生效", "config": cfg})


@route("/api/proactive/test", ("POST",))
def api_proactive_test(h, qs):
    st, d = ab("POST", "/api/plug/astrbot_plugin_proactive/page/test", body={}, timeout=120)
    if isinstance(d, dict):
        h._json(d)
    else:
        h._json({"status": "error", "message": str(d)[:200]})


@route("/api/proactive/reset-count", ("POST",))
def api_proactive_reset(h, qs):
    _write_json(PROACTIVE_STATE, {"date": time.strftime("%Y-%m-%d"), "count": 0})
    h._json({"status": "ok", "message": "今日计数已清零"})


@route("/api/proactive/sessions")
def api_proactive_sessions(h, qs):
    st, d = ab("GET", "/api/plug/astrbot_plugin_proactive/page/sessions", timeout=20)
    if st == 200 and isinstance(d, dict):
        h._json(d)
    else:
        h._json({"status": "error", "message": "插件没响应：%s" % str(d)[:120]})


@route("/api/proactive/reset-unanswered", ("POST",))
def api_proactive_reset_unanswered(h, qs):
    st, d = ab(
        "POST", "/api/plug/astrbot_plugin_proactive/page/reset-unanswered", body={}, timeout=20
    )
    if st == 200 and isinstance(d, dict):
        h._json(d)
    else:
        h._json({"status": "error", "message": "插件没响应：%s" % str(d)[:120]})


# ---------------- LivingMemory 面板 ----------------
@route("/api/livingmemory")
def api_livingmemory(h, qs):
    st, d = ab("GET", "/api/plug/astrbot_plugin_livingmemory/page/stats", timeout=25)
    if st == 200 and isinstance(d, dict):
        data = d.get("data") if isinstance(d.get("data"), dict) else d
        h._json({"status": "ok", "url": LIVINGMEM_PAGE, "stats": data})
    else:
        h._json(
            {
                "status": "ok",
                "url": LIVINGMEM_PAGE,
                "stats": {},
                "warning": "读不到统计：%s" % str(d)[:120],
            }
        )


@route("/api/livingmemory/graph")
def api_livingmemory_graph(h, qs):
    st, d = ab(
        "GET", "/api/plug/astrbot_plugin_livingmemory/page/graph/overview", timeout=40
    )
    if st == 200 and isinstance(d, dict):
        data = d.get("data") if isinstance(d.get("data"), dict) else d
        h._json({"status": "ok", "graph": data})
    else:
        h._json(
            {
                "status": "ok",
                "graph": {},
                "warning": "读不到图谱统计：%s" % str(d)[:120],
            }
        )


@route("/api/livingmemory/graph/rebuild", methods=["POST"])
def api_livingmemory_graph_rebuild(h, qs):
    st, d = ab(
        "POST",
        "/api/plug/astrbot_plugin_livingmemory/page/graph/rebuild",
        body={},
        timeout=600,
    )
    if st == 200 and isinstance(d, dict):
        h._json(d)
    else:
        h._json({"status": "error", "message": "重建失败：%s" % str(d)[:200]})


@route("/api/livingmemory/memories")
def api_livingmemory_memories(h, qs):
    try:
        page = int((qs.get("page") or ["1"])[0])
    except Exception:
        page = 1
    try:
        size = int((qs.get("page_size") or ["20"])[0])
    except Exception:
        size = 20
    size = max(1, min(100, size))
    st, d = ab(
        "GET",
        "/api/plug/astrbot_plugin_livingmemory/page/memories",
        params={"page": page, "page_size": size},
        timeout=30,
    )
    if st == 200 and isinstance(d, dict):
        h._json(d)
    else:
        h._json({"status": "error", "message": "读取失败：%s" % str(d)[:150]})



# ---------------- 语音回复 ----------------
VOICE_CFG = os.path.join(BASE, "data", "voice.json")
VOICE_DEFAULTS = {
    "enabled": True,
    "mode": "on_demand",
    "probability": 0.3,
    "trigger_words": ["发语音", "念给我听", "语音回复", "用语音说", "说句话", "语音"],
    "targets": [],
    "max_chars": 120,
    "keep_text": True,
    "async_voice": True,
    "tmp_keep_days": 2,
    "voice": "zh-CN-XiaoxiaoNeural",
    "rate": "+0%",
    "pitch": "+0Hz",
    "volume": "+0%",
    "engine": "fy",
    "fy_url": "http://127.0.0.1:%d" % FY_PORT,
    "fy_style": "cute",
    "fy_speed": 1.0,
    "fy_lazy": True,
    "fy_idle_minutes": 10,
    "fy_preload": False,
    "cv2_url": "http://127.0.0.1:%d" % CV2_PORT,
    "cv2_ref": "default",
    "cv2_speed": 1.0,
    "gsv_url": "http://127.0.0.1:%d" % GSV_PORT,
    "gsv_ref": "",
    "gsv_prompt": "你好呀，很高兴认识你，今天也要开心哦。",
    "gsv_speed": 1.0,
    "sing_enable": True,
    "sing_in_private": True,
    "sing_in_group": True,
    "sing_cooldown": 0,
}
CV2_SERVICE = "http://127.0.0.1:%d" % CV2_PORT
EDGE_VOICES = [
    ("zh-CN-XiaoxiaoNeural", "晓晓（女·温柔，默认）"),
    ("zh-CN-XiaoyiNeural", "晓伊（女·活泼）"),
    ("zh-CN-XiaoxuanNeural", "晓萱（女·成熟）"),
    ("zh-CN-YunxiNeural", "云希（男·年轻）"),
    ("zh-CN-YunyangNeural", "云扬（男·播音）"),
    ("zh-CN-YunjianNeural", "云健（男·浑厚）"),
    ("zh-CN-YunxiaNeural", "云夏（男·可爱）"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓北（东北话）"),
    ("zh-CN-shaanxi-XiaoniNeural", "晓妮（陕西话）"),
    ("zh-TW-HsiaoChenNeural", "曉臻（台湾）"),
    ("zh-HK-HiuMaanNeural", "曉曼（粤语）"),
]


def voice_cfg():
    cfg = dict(VOICE_DEFAULTS)
    cfg.update(_read_json(VOICE_CFG, {}) or {})
    return cfg


@route("/api/voice")
def api_voice_get(h, qs):
    cfg = voice_cfg()
    live = {}
    try:
        st, d = ab("GET", "/api/plug/astrbot_plugin_voice/page/state", timeout=20)
        live = (d or {}).get("data") or d or {}
    except Exception as e:
        live = {"error": str(e)[:120]}
    clone = {"up": False, "loaded": False, "refs": []}
    try:
        # 注意：http_json 内部会吞掉连接异常并返回 (0, {...})，必须显式判 200，
        # 否则服务没起来也会被当成 up=True。
        cst, cd = http_json(CV2_SERVICE + "/health", timeout=5)
        if cst == 200 and isinstance(cd, dict):
            clone = {"up": True, "loaded": bool(cd.get("loaded")), "refs": []}
            vst, vd = http_json(CV2_SERVICE + "/voices", timeout=5)
            if vst == 200 and isinstance(vd, dict):
                clone["refs"] = vd.get("refs") or []
        else:
            clone["error"] = str((cd or {}).get("message") or "连不上 %d" % CV2_PORT)[:120]
    except Exception as e:
        clone["error"] = str(e)[:120]
    fy = {"up": False, "loaded": False, "styles": [], "port": FY_PORT}
    try:
        fst, fd = http_json("http://127.0.0.1:%d/health" % FY_PORT, timeout=5)
        if fst == 200 and isinstance(fd, dict):
            fy = {
                "up": True,
                "loaded": bool(fd.get("loaded")),
                "styles": list(fd.get("styles") or []),
                "error": fd.get("error") or "",
                "port": FY_PORT,
                "idle_seconds": fd.get("idle_seconds"),
                "last_used": fd.get("last_used"),
            }
    except Exception as e:
        fy["error"] = str(e)[:120]
    h._json({"status": "ok", "config": cfg, "live": live, "clone": clone, "fy": fy, "gsv": {"up": port_open(GSV_PORT), "port": GSV_PORT}, "voices": [{"id": v, "name": n} for v, n in EDGE_VOICES]})


@route("/api/voice", ("POST",))
def api_voice_save(h, qs):
    body = h._json_body()
    cfg = voice_cfg()
    for key in ("enabled", "keep_text", "master", "async_voice"):
        if key in body:
            cfg[key] = bool(body[key])
    if "mode" in body and body["mode"] in ("on_demand", "probability", "always"):
        cfg["mode"] = body["mode"]
    if "probability" in body:
        cfg["probability"] = max(0.0, min(1.0, float(body["probability"])))
    if "max_chars" in body:
        cfg["max_chars"] = max(2, int(body["max_chars"]))
    for key in ("trigger_words", "targets"):
        if key in body:
            v = body[key]
            if isinstance(v, str):
                v = [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]
            cfg[key] = [str(x).strip() for x in (v or []) if str(x).strip()]
    for key in ("voice", "rate", "pitch", "volume", "cv2_url", "cv2_ref", "gsv_url", "gsv_ref", "gsv_prompt", "fy_url", "fy_style"):
        if key in body:
            cfg[key] = str(body[key]).strip()
    if "engine" in body and body["engine"] in ("edge", "fy", "cv2", "gsv"):
        cfg["engine"] = body["engine"]
    if "fy_speed" in body:
        cfg["fy_speed"] = max(0.5, min(2.0, float(body["fy_speed"])))
    if "cv2_speed" in body:
        cfg["cv2_speed"] = max(0.5, min(2.0, float(body["cv2_speed"])))
    if "gsv_speed" in body:
        cfg["gsv_speed"] = max(0.5, min(2.0, float(body["gsv_speed"])))
    for key in ("fy_lazy", "fy_preload"):
        if key in body:
            cfg[key] = bool(body[key])
    if "tmp_keep_days" in body:
        cfg["tmp_keep_days"] = max(0.0, min(90.0, float(body["tmp_keep_days"])))
    if "fy_idle_minutes" in body:
        cfg["fy_idle_minutes"] = max(0, min(240, int(body["fy_idle_minutes"])))
    for key in ("sing_enable", "sing_in_private", "sing_in_group", "sing_master"):
        if key in body:
            cfg[key] = bool(body[key])
    if "sing_cooldown" in body:
        cfg["sing_cooldown"] = max(0, min(1440, int(body["sing_cooldown"])))
    _write_json(VOICE_CFG, cfg)
    # 关掉语音总开关时，顺手停掉吃显存的本地 TTS 服务（模型不驻留）
    freed = ""
    if "master" in body and not cfg.get("master", True):
        was = []
        try:
            if port_open(FY_PORT):
                stop_fy()
                was.append("肥鱼")
        except Exception:
            pass
        try:
            if port_open(CV2_PORT):
                stop_cv2()
                was.append("CosyVoice2")
        except Exception:
            pass
        try:
            if port_open(GSV_PORT):
                kill_pids(gsv_pids())
                was.append("GPT-SoVITS")
        except Exception:
            pass
        if was:
            freed = "；已停止 " + "、".join(was) + "，显存已释放"
    h._json({"status": "ok", "message": "已保存，下一条回复立即生效" + freed, "config": cfg})


@route("/api/voice/test", ("POST",))
def api_voice_test(h, qs):
    body = h._json_body()
    text = str(body.get("text") or "").strip() or "你好，这是一条语音测试。"
    st, d = ab(
        "POST",
        "/api/plug/astrbot_plugin_voice/page/test",
        body={"text": text},
        timeout=180,
    )
    if isinstance(d, dict):
        h._json(d)
    else:
        h._json({"status": "error", "message": str(d)[:200]})


@route("/api/voice/cv2/start", ("POST",))
def api_cv2_start(h, qs):
    ok, m = start_cv2(wait=False)
    h._json({"status": "ok" if ok else "error", "message": ("CosyVoice2 正在启动（约 40~60 秒加载模型）" if ok else m)})


@route("/api/voice/cv2/stop", ("POST",))
def api_cv2_stop(h, qs):
    stop_cv2()
    h._json({"status": "ok", "message": "CosyVoice2 已停止", "up": port_open(CV2_PORT)})


@route("/api/voice/cv2/restart", ("POST",))
def api_cv2_restart(h, qs):
    stop_cv2()
    ok, m = start_cv2(wait=False)
    h._json({"status": "ok" if ok else "error", "message": ("CosyVoice2 正在重启（约 40~60 秒）" if ok else m)})


@route("/api/voice/gsv/start", ("POST",))
def api_gsv_start(h, qs):
    if port_open(CV2_PORT):
        h._json(
            {
                "status": "error",
                "message": "CosyVoice2 正在占用显卡，请先停止它再启动 GPT-SoVITS（两者同时开会爆显存）",
            }
        )
        return
    ok, m = start_gsv(wait=False)
    h._json(
        {
            "status": "ok" if ok else "error",
            "message": ("GPT-SoVITS 正在启动（首次约 30~90 秒加载模型）" if ok else m),
        }
    )


@route("/api/voice/gsv/stop", ("POST",))
def api_gsv_stop(h, qs):
    stop_gsv()
    h._json({"status": "ok", "message": "GPT-SoVITS 已停止", "up": port_open(GSV_PORT)})


@route("/api/voice/gsv/restart", ("POST",))
def api_gsv_restart(h, qs):
    stop_gsv()
    ok, m = start_gsv(wait=False)
    h._json(
        {
            "status": "ok" if ok else "error",
            "message": ("GPT-SoVITS 正在重启（约 30~90 秒）" if ok else m),
        }
    )


@route("/api/voice/fy/start", ("POST",))
def api_fy_start(h, qs):
    ok, m = start_fy(wait=False)
    h._json(
        {
            "status": "ok" if ok else "error",
            "message": ("肥鱼声音服务正在启动（约 15~40 秒加载模型）" if ok else m),
        }
    )


@route("/api/voice/fy/stop", ("POST",))
def api_fy_stop(h, qs):
    stop_fy()
    h._json({"status": "ok", "message": "肥鱼声音服务已停止", "up": port_open(FY_PORT)})


@route("/api/voice/fy/restart", ("POST",))
def api_fy_restart(h, qs):
    stop_fy()
    ok, m = start_fy(wait=False)
    h._json(
        {
            "status": "ok" if ok else "error",
            "message": ("肥鱼声音服务正在重启（约 15~40 秒）" if ok else m),
        }
    )


# ---------------------------------------------------------------- 翻唱（RVC）
SING_DIR = str(_c("sing_dir") or "")
SING_PY = str(_c("sing_python") or VENV_PY)
SING_SCRIPT = _j(SING_DIR, "sing.py")
SING_OUT = _j(SING_DIR, "out")
SING_STATE = os.path.join(BASE, "data", "sing_state.json")
_sing_lock = threading.RLock()   # _sing_save 会在持锁区内被调用，必须可重入
_sing_state = {
    "running": False,
    "stage": "",
    "query": "",
    "song": "",
    "started": 0,
    "finished": 0,
    "out": "",
    "error": "",
    "log": "",
    "vocal": "",
    "inst": "",
    "conv": "",
    "vocal_db": 0.0,
    "inst_db": 0.0,
    "preview": "",
}
SING_MIX_CFG = _j(SING_DIR, "mix.json")


def _sing_mix_cfg():
    """读混音偏移；面板与 sing.py 共用同一个文件。"""
    v, i = 0.0, 0.0
    try:
        with open(SING_MIX_CFG, "r", encoding="utf-8") as f:
            d = json.load(f)
        v = float(d.get("vocal_db", 0))
        i = float(d.get("inst_db", 0))
    except Exception:
        pass
    return {"vocal_db": max(-24.0, min(24.0, v)), "inst_db": max(-24.0, min(24.0, i))}


def _sing_save():
    try:
        with _sing_lock:
            snap = dict(_sing_state)
        with open(SING_STATE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
    except Exception:
        pass


def _sing_load():
    try:
        with open(SING_STATE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d["running"] = False  # 重启后面板线程已不存在
            _sing_state.update({k: d[k] for k in _sing_state if k in d})
    except Exception:
        pass


_sing_load()


@route("/api/sing/status")
def api_sing_status(h, qs):
    with _sing_lock:
        st = dict(_sing_state)
    if st.get("out") and os.path.isfile(st["out"]):
        st["size"] = os.path.getsize(st["out"])
        st["mtime"] = int(os.path.getmtime(st["out"]))
    h._json({"status": "ok", "state": st})


@route("/api/sing/search", ("GET",))
def api_sing_search(h, qs):
    q = (qs.get("q") or [""])[0].strip()
    if not q:
        return h._json({"status": "ok", "list": []})
    try:
        code = (
            "import json,sys,urllib.request,urllib.parse\n"
            "q=sys.argv[1]\n"
            "u='https://api.vkeys.cn/v2/music/netease?word='+urllib.parse.quote(q)\n"
            "d=json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=25).read().decode('utf-8'))\n"
            "print(json.dumps([{'id':it.get('id'),'song':it.get('song'),'singer':it.get('singer')} for it in (d.get('data') or [])[:10]],ensure_ascii=False))\n"
        )
        r = run_hidden([SING_PY, "-c", code, q], timeout=40)
        data = json.loads((r.stdout or "").strip().splitlines()[-1]) if r and r.stdout else []
        h._json({"status": "ok", "list": data})
    except Exception as e:
        h._json({"status": "error", "message": str(e)[:200], "list": []})


def _sing_worker(query, song_id, out_path):
    global _sing_state
    with _sing_lock:
        _sing_state.update(
            {
                "running": True,
                "stage": "开始",
                "query": query or "",
                "song": "",
                "started": int(time.time()),
                "finished": 0,
                "out": "",
                "error": "",
                "log": "",
                "vocal": "",
                "inst": "",
                "conv": "",
                "preview": "",
            }
        )
    try:
        args = [SING_PY, SING_SCRIPT]
        if song_id:
            args += ["--song-id", str(song_id)]
        else:
            args += ["--query", query or "大鱼"]
        args += ["--out", out_path]
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.Popen(
            args,
            cwd=SING_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
            startupinfo=_startupinfo(),
            env=env,
        )
        buf = []
        _acc_vocal = _acc_inst = _acc_conv = ""
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            buf.append(line)
            stage = ""
            if line.startswith("[0/4]"):
                stage = "搜索歌曲"
            elif line.startswith("[1/4]"):
                stage = "下载伴奏"
            elif line.startswith("[2/4]"):
                stage = "分离人声（UVR5）"
            elif line.startswith("[3/4]"):
                stage = "换声（RVC）"
            elif line.startswith("[4/4]"):
                stage = "混音输出"
            # 记下人声/伴奏/换声产物路径，后面「只调音量重新混」要用
            m2 = re.match(r"\s*伴奏[:：]\s*(.+)$", line)
            if line.startswith("[2/4]"):
                _acc_vocal = line.split("人声:")[-1].strip() if "人声:" in line else ""
            if m2:
                _acc_inst = m2.group(1).strip()
            if line.startswith("[3/4]"):
                _acc_conv = line.split("换声:")[-1].strip() if "换声:" in line else ""
            with _sing_lock:
                _sing_state["log"] = "\n".join(buf[-40:])
                if stage:
                    _sing_state["stage"] = stage
                if _acc_vocal:
                    _sing_state["vocal"] = _acc_vocal
                if _acc_inst:
                    _sing_state["inst"] = _acc_inst
                if _acc_conv:
                    _sing_state["conv"] = _acc_conv
                if line.startswith("RESULT "):
                    _sing_state["out"] = line[7:].strip()
                    _sing_save()
        proc.wait()
        with _sing_lock:
            if proc.returncode == 0 and _sing_state.get("out") and os.path.isfile(_sing_state["out"]):
                _sing_state["stage"] = "完成"
                _sing_state["finished"] = int(time.time())
            _sing_save()
        with _sing_lock:
            if not (proc.returncode == 0 and _sing_state.get("out") and os.path.isfile(_sing_state["out"])):
                _sing_state["stage"] = "失败"
                _sing_state["error"] = "\n".join(buf[-8:])[:800]
                _sing_state["finished"] = int(time.time())
        _sing_save()
    except Exception as e:
        with _sing_lock:
            _sing_state["stage"] = "失败"
            _sing_state["error"] = "%r" % (e,)
            _sing_state["finished"] = int(time.time())
    finally:
        with _sing_lock:
            _sing_state["running"] = False


@route("/api/sing/start", ("POST",))
def api_sing_start(h, qs):
    body = h._json_body() or {}
    if _sing_state.get("running"):
        return h._json({"status": "error", "message": "上一首还在做，等它完成"})
    query = str(body.get("query") or "").strip()
    song_id = str(body.get("song_id") or "").strip()
    if not query and not song_id:
        return h._json({"status": "error", "message": "请填歌名或网易云歌曲 ID"})
    if not os.path.isfile(SING_SCRIPT):
        return h._json({"status": "error", "message": "找不到 sing.py：%s" % SING_SCRIPT})
    os.makedirs(SING_OUT, exist_ok=True)
    name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+", "_", (query or song_id))[:30] or "song"
    out_path = os.path.join(SING_OUT, "feiyu_%s_%d.mp3" % (name, int(time.time())))
    threading.Thread(target=_sing_worker, args=(query, song_id, out_path), daemon=True).start()
    h._json({"status": "ok", "message": "开始制作，通常需要 1~3 分钟"})


@route("/api/sing/audio", ("GET",))
def api_sing_audio(h, qs):
    path = _sing_state.get("out") or ""
    if not path or not os.path.isfile(path):
        return h._send(404, "no audio", "text/plain; charset=utf-8")
    with open(path, "rb") as f:
        data = f.read()
    h._send(200, data, "audio/mpeg")


@route("/api/sing/stop", ("POST",))
def api_sing_stop(h, qs):
    """停掉正在跑的翻唱进程（按 sing.py 所在目录匹配命令行）。"""
    pat = _psq(os.path.basename(SING_DIR.rstrip("\\/")) or "sing.py") if SING_DIR else "sing.py"
    run_hidden(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*%s*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % pat]
    )
    h._json({"status": "ok", "message": "已请求停止"})


@route("/api/sing/mix", ("GET", "POST"))
def api_sing_mix(h, qs):
    if (h.command or "").upper() == "POST":
        body = h._json_body() or {}
        cfg = _sing_mix_cfg()
        for k in ("vocal_db", "inst_db"):
            if k in body:
                try:
                    cfg[k] = max(-24.0, min(24.0, round(float(body[k]), 1)))
                except Exception:
                    pass
        os.makedirs(os.path.dirname(SING_MIX_CFG), exist_ok=True)
        with open(SING_MIX_CFG, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        with _sing_lock:
            _sing_state["vocal_db"] = cfg["vocal_db"]
            _sing_state["inst_db"] = cfg["inst_db"]
            _sing_save()
        return h._json({"status": "ok", "message": "已保存", "config": cfg})
    cfg = _sing_mix_cfg()
    with _sing_lock:
        has = bool(_sing_state.get("conv") and os.path.isfile(_sing_state.get("conv") or "")
                   and _sing_state.get("inst") and os.path.isfile(_sing_state.get("inst") or ""))
        pv = _sing_state.get("preview") or ""
    h._json({"status": "ok", "config": cfg, "can_remix": has,
             "preview": pv if pv and os.path.isfile(pv) else ""})


def _sing_find_material():
    """在 SING_OUT 里找最近一次带 rvc_native.wav + instrument_*.wav 的目录。"""
    try:
        best, newest = None, 0
        for name in os.listdir(SING_OUT):
            d = os.path.join(SING_OUT, name)
            if not os.path.isdir(d):
                continue
            rvc = os.path.join(d, "rvc", "rvc_native.wav")
            sep = os.path.join(d, "sep")
            if not (os.path.isfile(rvc) and os.path.isdir(sep)):
                continue
            insts = [f for f in os.listdir(sep) if f.startswith("instrument_") and f.endswith(".wav")]
            if not insts:
                continue
            mt = os.path.getmtime(rvc)
            if mt > newest:
                newest = mt
                best = (rvc, os.path.join(sep, insts[0]))
        return best
    except Exception:
        return None


@route("/api/sing/remix", ("POST",))
def api_sing_remix(h, qs):
    """只按当前音量比例重新混一遍，不重新下载/分离/换声（几秒）。"""
    with _sing_lock:
        conv, inst = _sing_state.get("conv") or "", _sing_state.get("inst") or ""
        q = _sing_state.get("query") or _sing_state.get("song") or "song"
    if not (conv and os.path.isfile(conv) and inst and os.path.isfile(inst)):
        # 状态里没记（老记录/QQ 点歌），就从 out 目录找最近一次留下素材的活干
        found = _sing_find_material()
        if found:
            conv, inst = found
            with _sing_lock:
                _sing_state["conv"] = conv
                _sing_state["inst"] = inst
    if not (conv and os.path.isfile(conv) and inst and os.path.isfile(inst)):
        return h._json({"status": "error",
                        "message": "没有可重混的素材，请先完整做一首（人声/伴奏要落盘）"})
    cfg = _sing_mix_cfg()
    name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5_-]+", "_", str(q))[:24] or "song"
    out = os.path.join(SING_OUT, "mix_%s_%d.mp3" % (name, int(time.time())))
    args = [SING_PY, SING_SCRIPT, "--remix-vocal", conv, "--remix-inst", inst,
            "--out", out, "--vocal-db", str(cfg["vocal_db"]),
            "--inst-db", str(cfg["inst_db"])]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(args, cwd=SING_DIR, capture_output=True, timeout=300,
                           creationflags=NO_WINDOW, startupinfo=_startupinfo(), env=env)
    except Exception as e:
        return h._json({"status": "error", "message": "%r" % (e,)})
    log = (r.stdout or b"").decode("utf-8", "replace")
    if r.returncode != 0 or not os.path.isfile(out):
        tail = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "replace")[-600:]
        return h._json({"status": "error", "message": "重混失败：" + tail})
    with _sing_lock:
        _sing_state["preview"] = out
        _sing_state["vocal_db"] = cfg["vocal_db"]
        _sing_state["inst_db"] = cfg["inst_db"]
        _sing_save()
    line = [l for l in log.splitlines() if l.startswith("[mix]")]
    return h._json({"status": "ok", "message": line[0] if line else "已重新混音",
                    "preview": out, "size": os.path.getsize(out)})


@route("/api/sing/mixaudio", ("GET",))
def api_sing_mixaudio(h, qs):
    """试听：优先放刚刚重混的版本，没有就放成品。"""
    with _sing_lock:
        pv = _sing_state.get("preview") or ""
        path = pv if pv and os.path.isfile(pv) else (_sing_state.get("out") or "")
    if not path or not os.path.isfile(path):
        return h._send(404, "no audio", "text/plain; charset=utf-8")
    with open(path, "rb") as f:
        data = f.read()
    h._send(200, data, "audio/mpeg")


@route("/api/logs")
def api_logs(h, qs):
    which = qs.get("file", ["astrbot"])[0]
    n = int(qs.get("lines", ["200"])[0])
    files = {
        "astrbot": os.path.join(BASE, "astrbot_run.log"),
        "snowluma": _sl_log_ready(),
        "napcat": os.path.join(BASE, "napcat-boot.log"),
        "watcher": os.path.join(BASE, "dsh_watch.log"),
        "feiyu": os.path.join(BASE, "feiyu.log"),
        "cosyvoice2": os.path.join(BASE, "cosyvoice2.log"),
        "gptsovits": os.path.join(BASE, "gptsovits.log"),
    }
    path = files.get(which, files["astrbot"])
    h._json({"status": "ok", "file": which, "path": path, "text": tail(path, n)})


def open_panel(port):
    try:
        import webbrowser

        webbrowser.open("http://127.0.0.1:%d/" % port)
    except Exception as e:
        log("打开浏览器失败: %r" % (e,))


# ---------------------------------------------------------------- 自动登录
AUTOLOGIN_CFG = os.path.join(BASE, "data", "autologin.json")
AUTOLOGIN_DEFAULTS = {"enabled": True, "retry_seconds": 90, "qr_refresh": True}
_auto_lock = threading.Lock()
_auto_wake = threading.Event()
_auto_state = {
    "last_try": 0,
    "last_result": "",
    "tries": 0,
    "clicked": 0,
}


def autologin_cfg():
    cfg = dict(AUTOLOGIN_DEFAULTS)
    try:
        with open(AUTOLOGIN_CFG, encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    try:
        cfg["retry_seconds"] = max(20, min(3600, int(cfg.get("retry_seconds") or 90)))
    except Exception:
        cfg["retry_seconds"] = 90
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["qr_refresh"] = bool(cfg.get("qr_refresh"))
    return cfg


def autologin_save(body):
    cfg = autologin_cfg()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "qr_refresh" in body:
        cfg["qr_refresh"] = bool(body["qr_refresh"])
    if "retry_seconds" in body:
        try:
            cfg["retry_seconds"] = max(20, min(3600, int(body["retry_seconds"])))
        except Exception:
            pass
    tmp = AUTOLOGIN_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, AUTOLOGIN_CFG)
    _auto_wake.set()
    return cfg


def auto_bring_online(deep=True):
    """把机器人拉到在线：起 SnowLuma → 起 QQ → 注入 → 替它点登录 → 起 AstrBot。
    deep=False 时只做“轻量”修复（注入 + 点登录），不重启任何进程。"""
    steps = []
    if qq_state().get("isLogin"):
        # QQ 在线也可能只是 AstrBot 掉了，这种情况只补 AstrBot
        if deep and not port_open(ASTRBOT_PORT):
            ok, m = start_astrbot(wait=False)
            steps.append(m)
        return True, "已在线" + ("；" + "；".join(steps) if steps else "")

    # 1) SnowLuma
    if not port_open(SL_PORT):
        if not deep:
            return False, "SnowLuma 没在运行"
        ok, m = start_snowluma(wait=True)
        steps.append(m)
        if not ok:
            return False, "；".join(steps)

    # 2) 机器人 QQ
    if not qqbot_pids():
        if not deep:
            return False, "机器人 QQ 没在运行"
        ok, m = start_qqbot(wait=True)
        steps.append(m)
        if not ok:
            return False, "；".join(steps)
        time.sleep(4)

    # 3) 注入
    proc = sl_bot_proc()
    if not (proc and proc.get("injected")):
        ok, m = sl_hook_bot()
        steps.append(m)
        time.sleep(3)

    # 4) 点登录（最多两轮，第二轮处理“点完才弹账号选择”的情况）
    for _ in range(2):
        proc = sl_bot_proc()
        if proc and proc.get("loggedIn"):
            break
        okc, mc = click_qq_login()
        steps.append(mc)
        if not okc:
            break
        with _auto_lock:
            _auto_state["clicked"] += 1
        for _ in range(15):
            time.sleep(1.2)
            if qq_state().get("isLogin"):
                break

    # 5) AstrBot
    if not port_open(ASTRBOT_PORT):
        if deep:
            ok, m = start_astrbot(wait=False)
            steps.append(m)

    if qq_state().get("isLogin"):
        return True, "；".join([x for x in steps if x]) or "已在线"

    # 6) 登录不上就刷新二维码，方便主人扫
    try:
        autologin_cfg()["qr_refresh"] and capture_qr()
    except Exception:
        pass
    return False, "；".join([x for x in steps if x]) or "还没登录上"


def auto_login_worker():
    """常驻：发现掉线就自动拉回来（起进程 → 注入 → 点登录）。"""
    time.sleep(8)
    while True:
        cfg = autologin_cfg()
        try:
            # 手动点了“下线”就不自动拉起来，尊重主人意图
            if os.path.exists(PAUSE_FLAG):
                pass
            elif cfg.get("enabled") and not qq_state().get("isLogin"):
                with _auto_lock:
                    _auto_state["last_try"] = time.time()
                    _auto_state["tries"] += 1
                ok, msg = auto_bring_online(deep=True)
                with _auto_lock:
                    _auto_state["last_result"] = msg
                log("[自动登录] %s -> %s" % ("成功" if ok else "未成功", msg))
        except Exception as e:
            log("[自动登录] 异常: %r" % (e,))
        _auto_wake.wait(cfg.get("retry_seconds") or 90)
        _auto_wake.clear()


def napcat_guard_worker():
    """常驻看门狗：NapCat 与 SnowLuma 抢同一个 QQ 号会让 QQ 反复被踢下线。
    只要发现 NapCat 进程就立刻清掉。"""
    time.sleep(5)
    while True:
        try:
            pids = napcat_pids()
            if pids:
                log("[守卫] 发现残留 NapCat 进程 %s，清理（避免与 SnowLuma 抢 QQ）" % (pids,))
                kill_pids(pids)
        except Exception as e:
            log("[守卫] 异常: %r" % (e,))
        try:
            # 两个 SnowLuma 实例会让端口分叉，面板就问不到真实在线状态
            if snowluma_broken():
                pids = snowluma_pids()
                log("[守卫] SnowLuma 端口分叉（实例 %s 抢了不同端口），清理后等自动登录重建" % (pids,))
                kill_pids(pids)
                _auto_wake.set()
        except Exception as e:
            log("[守卫] SnowLuma 检查异常: %r" % (e,))
        time.sleep(10)



# ---------------------------------------------------------------- 游戏模式
# 打游戏时把吃显存/内存的东西全停掉：三个本地 TTS 服务、RVC 翻唱残留、
# 语音插件的开关（插件每次都实时读 voice.json，所以改文件立即生效），
# 以及可选的本地 Embedding 服务（726MB 内存，停了 LivingMemory 的向量检索会失效）。
GAME_CFG = os.path.join(BASE, "data", "game_mode.json")
GAME_DEFAULTS = {"enabled": False, "stop_embedding": True, "saved": {}}
_game_lock = threading.RLock()
_game_state = {"last_action": "", "last_time": 0, "stopped": []}


def game_cfg():
    cfg = dict(GAME_DEFAULTS)
    cfg.update(_read_json(GAME_CFG, {}) or {})
    if not isinstance(cfg.get("saved"), dict):
        cfg["saved"] = {}
    return cfg


def _game_save(cfg):
    _write_json(GAME_CFG, cfg)


def embed_pids():
    owner = port_owner(EMB_PORT)
    if owner:
        return [owner]
    return ps_pids("($_.Name -like 'python*') and ($_.CommandLine -like '*embed*service.py*')")


def stop_embed():
    kill_pids(embed_pids())
    for _ in range(20):
        if not port_open(EMB_PORT):
            break
        time.sleep(0.3)


def rvc_pids():
    # 只认 python 进程，否则会把执行这段查询的 powershell 自己也匹配进来
    return ps_pids(
        "($_.Name -like 'python*') and "
        "($_.CommandLine -like '*run_rvc.py*' -or $_.CommandLine -like '*AudioPre*')"
    )


def game_status():
    cfg = game_cfg()
    with _game_lock:
        st = dict(_game_state)
    voice = voice_cfg()
    return {
        "enabled": bool(cfg.get("enabled")),
        "stop_embedding": bool(cfg.get("stop_embedding", True)),
        "saved": cfg.get("saved") or {},
        "voice_master": bool(voice.get("master", True)),
        "sing_master": bool(voice.get("sing_master", True)),
        "fy_up": port_open(FY_PORT),
        "cv2_up": port_open(CV2_PORT),
        "gsv_up": port_open(GSV_PORT),
        "embed_up": port_open(EMB_PORT),
        "rvc_running": bool(rvc_pids()),
        "last_action": st.get("last_action", ""),
        "last_time": st.get("last_time", 0),
        "stopped": st.get("stopped", []),
    }


def game_apply(on):
    """开/关游戏模式。返回 (ok, message)。"""
    cfg = game_cfg()
    stopped = []
    with _game_lock:
        voice = voice_cfg()
        if on:
            # 1) 记住原始值，只记第一次（避免重复开关把 False 当成原始值）
            if not cfg.get("saved"):
                cfg["saved"] = {
                    "voice_master": bool(voice.get("master", True)),
                    "sing_master": bool(voice.get("sing_master", True)),
                }
            # 2) 语音插件的开关（插件实时读这个文件，改完下一条消息就生效）
            voice["master"] = False
            voice["sing_master"] = False
            _write_json(VOICE_CFG, voice)
            # 3) 停掉所有吃显存的服务
            for name, port, stopper in (
                ("肥鱼语音", FY_PORT, stop_fy),
                ("CosyVoice2", CV2_PORT, stop_cv2),
                ("GPT-SoVITS", GSV_PORT, lambda: kill_pids(gsv_pids())),
            ):
                try:
                    if port_open(port):
                        stopper()
                        stopped.append(name)
                except Exception as e:
                    log("[游戏模式] 停止 %s 失败: %r" % (name, e))
            # 4) 杀掉 RVC 翻唱残留（独占显卡）
            try:
                pids = rvc_pids()
                if pids:
                    kill_pids(pids)
                    stopped.append("RVC 翻唱残留")
            except Exception as e:
                log("[游戏模式] 清理 RVC 失败: %r" % (e,))
            # 5) 可选：停 Embedding（726MB 内存）
            if cfg.get("stop_embedding", True):
                try:
                    if port_open(EMB_PORT):
                        stop_embed()
                        stopped.append("本地 Embedding")
                except Exception as e:
                    log("[游戏模式] 停止 Embedding 失败: %r" % (e,))
            cfg["enabled"] = True
            msg = "游戏模式已开启"
            if stopped:
                msg += "，已停止 " + "、".join(stopped)
            else:
                msg += "，没有需要停的服务"
            msg += "；语音与唱歌已禁用"
        else:
            saved = cfg.get("saved") or {}
            if "voice_master" in saved:
                voice["master"] = bool(saved.get("voice_master"))
            if "sing_master" in saved:
                voice["sing_master"] = bool(saved.get("sing_master"))
            _write_json(VOICE_CFG, voice)
            if cfg.get("stop_embedding", True) and not port_open(EMB_PORT):
                try:
                    start_embed(wait=False)
                    stopped.append("Embedding 已重启")
                except Exception as e:
                    log("[游戏模式] 重启 Embedding 失败: %r" % (e,))
            cfg["enabled"] = False
            cfg["saved"] = {}
            msg = "游戏模式已关闭，语音已恢复；本地 TTS 服务仍是懒加载（用到才起）"
            if stopped:
                msg += "；" + "、".join(stopped)
        _game_save(cfg)
        _game_state["last_action"] = msg
        _game_state["last_time"] = int(time.time())
        _game_state["stopped"] = stopped
    log("[游戏模式] " + msg)
    return True, msg


def game_guard_worker():
    """游戏模式开着的时候，每 30 秒扫一遍，谁偷偷起来了就再按下去。"""
    while True:
        time.sleep(30)
        try:
            cfg = game_cfg()
            if not cfg.get("enabled"):
                continue
            if port_open(FY_PORT):
                stop_fy()
                log("[游戏模式] 拦住偷偷启动的肥鱼语音")
            if port_open(CV2_PORT):
                stop_cv2()
                log("[游戏模式] 拦住偷偷启动的 CosyVoice2")
            if port_open(GSV_PORT):
                kill_pids(gsv_pids())
                log("[游戏模式] 拦住偷偷启动的 GPT-SoVITS")
            if cfg.get("stop_embedding", True) and port_open(EMB_PORT):
                stop_embed()
                log("[游戏模式] 拦住偷偷启动的 Embedding")
            pids = rvc_pids()
            if pids:
                kill_pids(pids)
                log("[游戏模式] 拦住偷偷启动的 RVC")
        except Exception as e:
            log("[游戏模式] 守卫异常: %r" % (e,))


@route("/api/game")
def api_game_get(h, qs):
    h._json({"status": "ok", **game_status()})


@route("/api/game/toggle", ("POST",))
def api_game_toggle(h, qs):
    body = h._json_body() or {}
    cur = game_cfg()
    want = body.get("enabled")
    want = (not bool(cur.get("enabled"))) if want is None else bool(want)
    if "stop_embedding" in body:
        c2 = game_cfg()
        c2["stop_embedding"] = bool(body["stop_embedding"])
        _game_save(c2)
    ok, msg = game_apply(want)
    h._json({"status": "ok" if ok else "error", "message": msg, **game_status()})


@route("/api/game/embedding", ("POST",))
def api_game_embedding(h, qs):
    body = h._json_body() or {}
    cfg = game_cfg()
    cfg["stop_embedding"] = bool(body.get("stop_embedding", True))
    _game_save(cfg)
    h._json({"status": "ok", "message": "已保存", "stop_embedding": cfg["stop_embedding"], **game_status()})


# ---------------------------------------------------------------- 自动清理
CLEAN_CFG = os.path.join(BASE, "data", "clean.json")
CLEAN_DEFAULTS = {"enabled": True, "days": 7, "interval_hours": 6}
_clean_state = {"last_run": 0, "last_result": "", "removed": 0, "freed_mb": 0.0}
_clean_lock = threading.RLock()   # clean_run 会在持锁区内再取一次，必须可重入
_clean_wake = threading.Event()


def clean_cfg():
    cfg = dict(CLEAN_DEFAULTS)
    try:
        with open(CLEAN_CFG, encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    try:
        cfg["days"] = max(1, min(365, int(cfg.get("days") or 7)))
    except Exception:
        cfg["days"] = 7
    try:
        cfg["interval_hours"] = max(1, min(168, int(cfg.get("interval_hours") or 6)))
    except Exception:
        cfg["interval_hours"] = 6
    cfg["enabled"] = bool(cfg.get("enabled", True))
    return cfg


def clean_save(body):
    cfg = clean_cfg()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    for k, lo, hi in (("days", 1, 365), ("interval_hours", 1, 168)):
        if k in body:
            try:
                cfg[k] = max(lo, min(hi, int(body[k])))
            except Exception:
                pass
    tmp = CLEAN_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CLEAN_CFG)
    _clean_wake.set()
    return cfg


def clean_targets():
    """要清理的目录：翻唱成品/素材、语音临时文件。"""
    return [
        ("翻唱产出", SING_OUT),
        # 语音插件的临时目录（插件自己算出来的 _BASE/data/voice_tmp），
        # 早期这里误写成 BASE\AstrBot\data\voice_tmp，导致语音文件从来没被清理过。
        ("语音临时", os.path.join(BASE, "data", "voice_tmp")),
        ("语音临时（旧）", os.path.join(BASE, "AstrBot", "data", "voice_tmp")),
    ]


def clean_scan(days=None):
    """只统计不删除，给面板显示用。"""
    cfg = clean_cfg()
    d = days if days is not None else cfg["days"]
    cutoff = time.time() - d * 86400
    out = []
    for label, path in clean_targets():
        item = {"label": label, "path": path, "files": 0, "mb": 0.0, "expired": 0, "keep": 0}
        try:
            if not os.path.isdir(path):
                out.append(item)
                continue
            for root, dirs, files in os.walk(path):
                # 正在做的目录先跳过：sing_ 目录里如果有近 10 分钟动过的文件就整目录保留
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        st = os.stat(fp)
                    except Exception:
                        continue
                    item["files"] += 1
                    item["mb"] += st.st_size / 1048576.0
                    if st.st_mtime < cutoff:
                        item["expired"] += 1
                    else:
                        item["keep"] += 1
        except Exception as e:
            item["error"] = repr(e)
        item["mb"] = round(item["mb"], 1)
        out.append(item)
    return out


def clean_run(force=False):
    """删除超过保留天数的产出。返回 (删除数, 释放MB)。"""
    with _clean_lock:
        cfg = clean_cfg()
        if not cfg["enabled"] and not force:
            return 0, 0.0
        cutoff = time.time() - cfg["days"] * 86400
        busy = os.path.join(SING_OUT, "..")
        removed, freed = 0, 0.0
        for label, path in clean_targets():
            if not os.path.isdir(path):
                continue
            for root, dirs, files in os.walk(path, topdown=False):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        st = os.stat(fp)
                    except Exception:
                        continue
                    if st.st_mtime >= cutoff:
                        continue
                    try:
                        os.remove(fp)
                        removed += 1
                        freed += st.st_size / 1048576.0
                    except Exception:
                        pass
                # 顺手删掉空目录（但不删 SING_OUT 本身）
                if os.path.abspath(root) != os.path.abspath(path):
                    try:
                        os.rmdir(root)
                    except Exception:
                        pass
        with _clean_lock:
            _clean_state.update({
                "last_run": int(time.time()),
                "removed": removed,
                "freed_mb": round(freed, 1),
                "last_result": ("清理 %d 个文件，释放 %.1f MB" % (removed, freed)) if removed else "没有需要清理的文件",
            })
        if removed:
            log("[清理] " + _clean_state["last_result"])
        return removed, freed


def clean_worker():
    time.sleep(20)
    while True:
        cfg = clean_cfg()
        if cfg.get("enabled"):
            try:
                clean_run()
            except Exception as e:
                log("[清理] 异常: %r" % (e,))
        _clean_wake.wait(max(3600, cfg.get("interval_hours", 6) * 3600))
        _clean_wake.clear()


@route("/api/clean")
def api_clean(h, qs):
    cfg = clean_cfg()
    h._json({"status": "ok", "config": cfg, "state": _clean_state,
             "targets": clean_scan()})


@route("/api/clean", ("POST",))
def api_clean_save(h, qs):
    cfg = clean_save(h._json_body())
    h._json({"status": "ok", "message": "已保存，保留 %d 天" % cfg["days"], "config": cfg})


@route("/api/clean/run", ("POST",))
def api_clean_run(h, qs):
    removed, freed = clean_run(force=True)
    h._json({"status": "ok",
             "message": ("清理 %d 个文件，释放 %.1f MB" % (removed, freed)) if removed else "没有需要清理的文件",
             "removed": removed, "freed_mb": round(freed, 1), "targets": clean_scan()})


# ---------------------------------------------------------------- 掉线监控
MONITOR_CFG = os.path.join(BASE, "data", "monitor.json")
MONITOR_DEFAULTS = {"enabled": True, "interval_seconds": 120, "notify": True}
NAPCAT_QR = QR_CACHE
_mon_state = {
    "qq_ok": None,
    "astrbot_ok": None,
    "last_check": 0,
    "down_since": 0,
    "last_notify": 0,
}
_mon_lock = threading.Lock()
_mon_wake = threading.Event()


def monitor_cfg():
    cfg = dict(MONITOR_DEFAULTS)
    try:
        with open(MONITOR_CFG, encoding="utf-8") as f:
            cfg.update(json.load(f) or {})
    except Exception:
        pass
    try:
        cfg["interval_seconds"] = max(10, min(3600, int(cfg.get("interval_seconds") or 120)))
    except Exception:
        cfg["interval_seconds"] = 120
    return cfg


def monitor_save(body):
    cfg = monitor_cfg()
    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
    if "notify" in body:
        cfg["notify"] = bool(body["notify"])
    if "interval_seconds" in body:
        try:
            cfg["interval_seconds"] = max(10, min(3600, int(body["interval_seconds"])))
        except Exception:
            pass
    tmp = MONITOR_CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MONITOR_CFG)
    _mon_wake.set()
    return cfg


def _balloon(title, text):
    """Windows 托盘气泡提示（不阻塞）。"""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Warning;"
        "$n.Visible=$true;"
        "$n.ShowBalloonTip(10000,'%s','%s','Warning');"
        "Start-Sleep -Seconds 7;$n.Dispose()" % (title.replace("'", "''"), text.replace("'", "''"))
    )
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,
        )
    except Exception as e:
        log("[监控] 通知失败: %r" % (e,))


def monitor_check_once(force_notify=False):
    qq_ok = port_open(ONEBOT_WWW_PORT) and bool(sl_bot_proc())
    astrbot_ok = port_open(ASTRBOT_PORT) and port_open(ASTRBOT_WS_PORT)
    now = time.time()
    with _mon_lock:
        prev = _mon_state["qq_ok"]
        _mon_state["qq_ok"] = qq_ok
        _mon_state["astrbot_ok"] = astrbot_ok
        _mon_state["last_check"] = now
        if not qq_ok:
            if not _mon_state["down_since"]:
                _mon_state["down_since"] = now
        else:
            _mon_state["down_since"] = 0
        cfg = monitor_cfg()
        went_down = (prev != qq_ok) and (qq_ok is False)
        if (went_down or force_notify) and cfg.get("notify"):
            if now - _mon_state["last_notify"] > 120:
                _mon_state["last_notify"] = now
                _balloon("QQ 掉线了", "SnowLuma 的 OneBot 服务(%d)没起来，打开面板点“重新登录”扫码" % _uport(ONEBOT_WS, 3001))
    return qq_ok, astrbot_ok


def monitor_worker():
    while True:
        cfg = monitor_cfg()
        if not cfg.get("enabled"):
            _mon_wake.wait(30)
            _mon_wake.clear()
            continue
        try:
            monitor_check_once()
        except Exception as e:
            log("[监控] 检查异常: %r" % (e,))
        _mon_wake.wait(cfg.get("interval_seconds") or 120)
        _mon_wake.clear()


def monitor_state():
    cfg = monitor_cfg()
    with _mon_lock:
        st = dict(_mon_state)
    st["enabled"] = cfg["enabled"]
    st["interval_seconds"] = cfg["interval_seconds"]
    st["notify"] = cfg["notify"]
    st["qr_available"] = os.path.isfile(NAPCAT_QR)
    st["qr_mtime"] = int(os.path.getmtime(NAPCAT_QR)) if st["qr_available"] else 0
    st["qr_age"] = int(time.time() - st["qr_mtime"]) if st["qr_mtime"] else -1
    st["sl_up"] = port_open(SL_PORT)
    st["now"] = int(time.time())
    return st


@route("/api/autologin")
def api_autologin_get(h, qs):
    with _auto_lock:
        st = dict(_auto_state)
    h._json({"status": "ok", "config": autologin_cfg(), **st})


@route("/api/autologin", ("POST",))
def api_autologin_save(h, qs):
    cfg = autologin_save(h._json_body())
    h._json({"status": "ok", "message": "已保存", "config": cfg})


@route("/api/autologin/run", ("POST",))
def api_autologin_run(h, qs):
    with _auto_lock:
        _auto_state["tries"] += 1
        _auto_state["last_try"] = time.time()
    ok, msg = auto_bring_online(deep=True)
    with _auto_lock:
        _auto_state["last_result"] = msg
    h._json({"status": "ok" if ok else "error", "message": msg})


@route("/api/monitor")
def api_monitor(h, qs):
    # 前端每 20 秒轮询一次；间隔可能被设成 10 分钟，若只读后台缓存的旧结果，
    # 掉线横幅会在恢复后仍挂着 —— 轮询时顺手做一次实时检查。
    with _mon_lock:
        stale = (time.time() - (_mon_state.get("last_check") or 0)) > 15
    if stale:
        try:
            monitor_check_once()
        except Exception as e:
            log("[监控] 实时检查异常: %r" % (e,))
    h._json({"status": "ok", **monitor_state()})


@route("/api/monitor", ("POST",))
def api_monitor_save(h, qs):
    cfg = monitor_save(h._json_body())
    h._json({"status": "ok", "message": "已保存，%d 秒后生效" % cfg["interval_seconds"], "config": cfg})


@route("/api/monitor/check", ("POST",))
def api_monitor_check(h, qs):
    monitor_check_once()
    h._json({"status": "ok", **monitor_state()})


@route("/api/monitor/qr")
def api_monitor_qr(h, qs):
    try:
        with open(NAPCAT_QR, "rb") as f:
            buf = f.read()
        h.send_response(200)
        h.send_header("Content-Type", "image/png")
        h.send_header("Cache-Control", "no-store")
        h.send_header("Content-Length", str(len(buf)))
        h.end_headers()
        h.wfile.write(buf)
    except Exception:
        h._json({"status": "error", "message": "还没有二维码，点“重新登录”截一张"}, code=404)


def main():
    port = int(os.environ.get("PANEL_PORT") or CFG.get("port") or 8899)
    if port_open(port):
        log("面板已在运行，直接打开浏览器: http://127.0.0.1:%d" % port)
        open_panel(port)
        return
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    log("QQ Bot 管理面板: http://127.0.0.1:%d" % port)
    load_auth()  # 首次启动就把登录密码打印出来，不用等第一次登录失败
    if "--no-browser" not in sys.argv:
        threading.Timer(1.2, open_panel, args=(port,)).start()
    threading.Thread(target=auto_login_worker, daemon=True).start()
    threading.Thread(target=napcat_guard_worker, daemon=True).start()
    threading.Thread(target=monitor_worker, daemon=True).start()
    threading.Thread(target=clean_worker, daemon=True).start()
    threading.Thread(target=game_guard_worker, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
