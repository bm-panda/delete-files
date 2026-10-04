#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按条件批量删除文件 —— 基于开源 deletor CLI 的文件夹批量清理脚本。

用法（盒子注入）：python main.py <参数JSON文件路径>
JSON 三段：environment（盒子只读）/ data.target_dirs（文件夹数组）/ params（运行参数）。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "bm-scripts-box-rc.toml"
TITLE = "按条件批量删除文件"
LOG_ROOT = SCRIPT_DIR / "logs"
LOG_KEEP = 30                    # 日志目录只保留最近 N 次，避免无限增长
DELETOR_TIMEOUT = 1800           # 单个文件夹的 deletor 执行上限（秒）
ENVELOPE_LIST_CAP = 500          # 信封里单个 list 键最多回传多少条路径
GUIDE_URL = "在文件管理器中选中要清理的文件夹 → 右键 → 按条件批量删除文件"

SAFETY_MODES = ("preview", "trash", "permanent")
SIZE_MODES = ("off", "on")
TIME_MODES = ("off", "older", "newer", "both")
BOOL_FIELDS = ("recursive", "prune_empty", "allow_system")
ENUM_FIELDS = {"safety": SAFETY_MODES, "size_mode": SIZE_MODES, "time_mode": TIME_MODES}
TEXT_FIELDS = ("extensions", "min_size", "max_size", "older", "newer", "exclude")
DEFAULT_EXCLUDE = ("Windows,Program Files,Program Files (x86),ProgramData,AppData,"
                   "boot,Recovery,$Recycle.Bin,System Volume Information,.git,node_modules")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DRIVE_ROOT_RE = re.compile(r"^[A-Za-z]:[\\/]*$")
SIZE_TEXT_RE = re.compile(r"^\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|PB)$", re.I)
SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "PB": 1024 ** 5, "TB": 1024 ** 4}
STATUS_MARKS = ("✓", "⚠", "ℹ", "✗")


# ==================== 通用基建 ====================

def fix_encoding():
    """强制 stdout/stderr 走 UTF-8，避免 GBK 控制台打印中文与符号时崩溃。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def run_cmd(cmd, **kw):
    """统一 subprocess 执行：不闪黑窗、UTF-8 容错解码、禁用 ANSI 颜色。"""
    kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    kw.setdefault("encoding", "utf-8")
    kw.setdefault("errors", "replace")
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    kw.setdefault("env", env)
    return subprocess.run(cmd, text=True, **kw)


def script_version():
    """版本号单一来源：从 bm-scripts-box-rc.toml 读，不硬编码。"""
    try:
        m = re.search(r'^\s*version\s*=\s*"([^"]+)"', CONFIG_FILE.read_text("utf-8"), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _dw(text):
    """显示宽度：CJK / 全角 / emoji 计 2，其余计 1。"""
    return sum(2 if ord(c) > 0x1100 else 1 for c in text)


def banner(text):
    body = f"  {text}  "
    print("┌" + "─" * _dw(body) + "┐")
    print("│" + body + "│")
    print("└" + "─" * _dw(body) + "┘", flush=True)


def section(title):
    print(f"\n── {title} " + "─" * max(4, 46 - _dw(title)))


def countdown_exit(seconds=5):
    """批量任务结束后用倒计时退出：msvcrt 监听按键可提前走，必须 flush 否则块缓冲下不可见。"""
    try:
        import msvcrt
        has_key = True
    except ImportError:
        has_key = False
    width = 10
    for i in range(seconds, 0, -1):
        if has_key:
            try:
                if msvcrt.kbhit():
                    break
            except OSError:            # stdout 被重定向、无真实控制台时不可用
                has_key = False
        filled = round(width * (seconds - i + 1) / seconds)
        bar = "█" * filled + "░" * (width - filled)
        print(f"\r  ⏳ {i}s {bar}  按任意键立即退出", end="", flush=True)
        time.sleep(1)
    print("\r" + " " * 60 + "\r  👋 已退出", flush=True)
    sys.exit(0 if _EXIT_OK else 1)


def pause_exit(message="按任意键退出"):
    """无参引导用暂停退出：没有真实任务时倒计时显得仓促。"""
    try:
        import msvcrt
        print(f"\n  {message}...", end="", flush=True)
        msvcrt.getch()
    except (ImportError, OSError):     # 非 Windows 或无控制台：退化为 input()
        try:
            input(f"\n  {message}...")
        except (EOFError, OSError):
            pass
    print("  👋 已退出", flush=True)
    sys.exit(0)


def hide_console():
    """自动模式（定时/节点）隐藏控制台窗口，保持静默。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()   # 无控制台返回 0
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)       # SW_HIDE
    except Exception:
        pass


def human_size(num):
    if num < 0:
        num = 0
    unit = "B"
    val = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if val < 1024 or unit == "PB":
            break
        val /= 1024
    return f"{val:.2f} {unit}"


def parse_size_text(text):
    """把 deletor 表格里的 '1.00 KB' 还原成字节数（preview 档估算释放量用）。"""
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|PB)$", text.strip(), re.I)
    if not m:
        return 0
    return int(float(m.group(1)) * SIZE_UNITS[m.group(2).upper()])


_EXIT_OK = True


# ==================== 配置 ====================

@dataclass
class Config:
    safety: str = "trash"
    recursive: bool = True
    prune_empty: bool = False
    extensions: str = ""
    size_mode: str = "off"
    min_size: str = ""
    max_size: str = ""
    time_mode: str = "off"
    older: str = ""
    newer: str = ""
    exclude: str = DEFAULT_EXCLUDE
    allow_system: bool = False

    @property
    def preview(self):
        return self.safety == "preview"

    @property
    def trash(self):
        return self.safety == "trash"

    @classmethod
    def from_dict(cls, data):
        """盒子只注入声明过的键，被 when 隐藏的键不会进来 —— 全部回落默认值。

        exclude 例外：键存在但为空串表示用户主动清空了排除项，尊重其意图
        （目录根防护仍由 guard_dir 兜底）。
        """
        cfg = cls()
        if not isinstance(data, dict):
            return cfg
        for name in BOOL_FIELDS:
            if isinstance(data.get(name), bool):
                setattr(cfg, name, data[name])
        for name, allowed in ENUM_FIELDS.items():
            value = data.get(name)
            if isinstance(value, str) and value in allowed:
                setattr(cfg, name, value)
        for name in TEXT_FIELDS:
            value = data.get(name)
            if isinstance(value, str):
                setattr(cfg, name, value.strip())
        return cfg


def protected_roots():
    """受保护的目录根集合（小写规范化）：只有目标恰好等于这些根时才拦截，不误伤其子目录。"""
    roots = set()

    def add(path):
        if path:
            roots.add(norm_path(path))

    for key in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData"):
        add(os.environ.get(key))
    home = os.path.expanduser("~")
    add(home)
    add(os.path.join(home, os.pardir))          # 一般是 C:\Users
    add(os.environ.get("SYSTEMDRIVE", "C:\\"))
    return roots


def norm_path(path):
    return os.path.normpath(os.path.abspath(str(path))).rstrip("\\/").lower()


def guard_dir(path, cfg):
    """调 deletor 前校验目标本身。返回 (是否放行, 原因)。"""
    raw = str(path).strip().strip('"')
    # "D:" 这种写法经 abspath 会展开成「D 盘当前目录」，必须在规范化之前按字面识别盘根
    if re.match(r"^[A-Za-z]:[\\/]*$", raw):
        return False, "禁止以盘根目录作为清理目标（不可解除）"
    n = norm_path(raw)
    if not n:
        return False, "路径为空"
    if DRIVE_ROOT_RE.match(n):
        return False, "禁止以盘根目录作为清理目标（不可解除）"
    if not os.path.isdir(n):
        return False, "文件夹不存在"
    if not cfg.allow_system and n in protected_roots():
        return False, "命中受保护的系统/用户目录根（如需强制请开启「允许系统目录」）"
    return True, ""


# ==================== 业务引擎 ====================

class Deletor:
    """只产数据、不掺展示：调用 deletor 并解析其结果。"""

    def __init__(self, binary):
        self.binary = binary

    def build_args(self, cfg, folder, log_dir):
        """组装命令行。参数顺序是契约：--log-json 与其目录必须是最后两个 token。"""
        args = [self.binary, "-cli", "-d", os.path.normpath(str(folder))]
        if cfg.extensions:
            args += ["-e", cfg.extensions]
        if cfg.size_mode == "on":
            if cfg.min_size:
                args += ["--min-size", cfg.min_size]
            if cfg.max_size:
                args += ["--max-size", cfg.max_size]
        if cfg.time_mode in ("older", "both") and cfg.older:
            args += ["--older", cfg.older]
        if cfg.time_mode in ("newer", "both") and cfg.newer:
            args += ["--newer", cfg.newer]
        if cfg.exclude:
            args += ["--exclude", cfg.exclude]
        if cfg.recursive:
            args.append("-subdirs")
        if cfg.preview:
            # preview 档：不传 -skip-confirm（靠它就无法拿到表格）、不传 -prune-empty
            # （deletor 的两次确认无法用管道 stdin 满足，必然 EOF → log.Fatal 退出码 1）
            return args
        if cfg.prune_empty:
            args.append("-prune-empty")
        args.append("-skip-confirm")           # 无人值守必给，否则读 stdin 阻塞/EOF
        if cfg.trash:
            args.append("-trash")              # 不给 -trash 就是 os.Remove 物理删除
        args += ["--log-json", str(log_dir)]   # 必须双横线 + 传目录 + 位于最末尾
        return args

    def clean_one(self, cfg, folder, log_dir):
        """清理单个文件夹，返回 dict(paths, freed_bytes, stdout, rc, error)。"""
        args = self.build_args(cfg, folder, log_dir)
        kw = {"capture_output": True, "cwd": str(log_dir), "timeout": DELETOR_TIMEOUT}
        if cfg.preview:
            kw["input"] = "n\n"          # 只有一个确认（preview 不传 -prune-empty），喂 n 拒绝
        else:
            kw["stdin"] = subprocess.DEVNULL   # 已给 -skip-confirm，仍显式关闭 stdin 防阻塞
        try:
            proc = run_cmd(args, **kw)
        except subprocess.TimeoutExpired:
            return {"paths": [], "freed_bytes": 0, "stdout": "", "rc": -1,
                    "error": f"执行超时（>{DELETOR_TIMEOUT}s）"}
        except OSError as exc:
            return {"paths": [], "freed_bytes": 0, "stdout": "", "rc": -1,
                    "error": f"无法执行 deletor：{exc}"}

        out = ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))
        if proc.returncode != 0:
            return {"paths": [], "freed_bytes": 0, "stdout": out, "rc": proc.returncode,
                    "error": tail_line(out) or f"deletor 退出码 {proc.returncode}"}

        if cfg.preview:
            rows = parse_table_lines(out)
            paths = [p for _, p in rows]
            size = sum(parse_size_text(s) for s, _ in rows)
        else:
            paths, size = read_json_log(log_dir)
        return {"paths": paths, "freed_bytes": size, "stdout": out, "rc": 0, "error": ""}

    @staticmethod
    def verify(paths):
        """复核删除是否真生效：deletor 的 MoveFileToTrash 丢弃错误也会打印成功。"""
        pending = [p for p in paths if os.path.exists(p)]
        if pending:
            time.sleep(0.35)                   # 回收站移动可能刚提交给 shell
            pending = [p for p in pending if os.path.exists(p)]
        return pending


def parse_table_lines(stdout_text):
    """deletor 的 PrintFilesTable 行形如「补齐空格的大小␣␣路径」。"""
    rows = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if "  " not in line or line.startswith(STATUS_MARKS):
            continue
        size_part, path_part = line.split("  ", 1)
        size_part, path_part = size_part.strip(), path_part.strip()
        if not SIZE_TEXT_RE.match(size_part):      # 排除 'DIR  路径'（prune-empty）等行
            continue
        if not os.path.isabs(path_part):
            continue
        rows.append((size_part, path_part))
    return rows


def read_json_log(log_dir):
    """读本次独立目录下的 deletor.json —— trash/permanent 档的权威已删清单。"""
    target = Path(log_dir) / "deletor.json"
    if not target.exists():
        return [], 0
    try:
        data = json.loads(target.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], 0
    if not isinstance(data, list):
        return [], 0
    paths, total, seen = [], 0, set()
    for entry in data:
        if not (isinstance(entry, dict) and isinstance(entry.get("path"), str)):
            continue
        path = entry["path"]
        if path in seen:               # 同一文件被重复记录时不双计
            continue
        seen.add(path)
        paths.append(path)
        nbytes = entry.get("bytes")
        if isinstance(nbytes, int) and not isinstance(nbytes, bool):
            total += nbytes
    return paths, total


def tail_line(stdout_text):
    lines = [l.strip() for l in stdout_text.splitlines() if l.strip()]
    return lines[-1] if lines else ""


def resolve_deletor():
    """解析 deletor 可执行文件绝对路径（盒子 binaries 的 shim 可能是 .cmd，需用绝对路径）。"""
    for candidate in ("deletor", "deletor.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    local = SCRIPT_DIR / "deletor.exe"
    if local.exists():
        return str(local)
    raise FileNotFoundError("未找到 deletor 命令行工具。请通过不忙脚本盒子安装/更新本脚本，"
                            "盒子会自动下载该工具；确认安装成功后可重试。")


def rotate_logs(keep=LOG_KEEP):
    """只保留最近 keep 个运行日志目录。"""
    try:
        if not LOG_ROOT.is_dir():
            return
        dirs = sorted((d for d in LOG_ROOT.iterdir() if d.is_dir()),
                      key=lambda d: d.name, reverse=True)
        for stale in dirs[keep:]:
            shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        pass


# ==================== 流程编排 ====================

class Cli:
    def __init__(self, cfg, mode, env):
        self.cfg = cfg
        self.mode = mode
        self.env = env
        self.lines = []
        self.run_dir = None            # 真正开跑时才原子创建，见 _make_run_dir
        self.visible = (mode == "manual")

    def say(self, text=""):
        self.lines.append(str(text))
        if self.visible:
            try:
                print(text, flush=True)
            except Exception:
                pass

    def _make_run_dir(self):
        """原子创建本次运行的日志目录。同一秒内的第二次运行必须换目录：
        deletor 会读取已存在的 deletor.json 追加合并，共用目录会把上一次的清单算进来。"""
        LOG_ROOT.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        for attempt in range(1, 100):
            name = stamp if attempt == 1 else f"{stamp}-{attempt}"
            candidate = LOG_ROOT / name
            try:
                candidate.mkdir()          # 不带 exist_ok：创建失败即说明已被占用
                return candidate
            except FileExistsError:
                continue
            except OSError:
                return candidate
        return LOG_ROOT / stamp

    def _restore_target(self, cfg, folder, summary):
        """deletor 的 -prune-empty 会连「被清空的扫描根」一起 os.Remove（实测目标夹自身消失）。
        用户的意图是清空这个文件夹、不是删掉它，所以删空后原地重建。"""
        if cfg.preview or not cfg.prune_empty:
            return
        target = os.path.normpath(str(folder))
        if os.path.isdir(target):
            return
        try:
            os.makedirs(target, exist_ok=True)
            self.say(f"    ↻ 目标文件夹被清空后一并删除，已原地重建：{target}")
        except OSError as exc:
            self.say(f"    ⚠️  目标文件夹被清空删除且重建失败：{exc}")
            summary["errors"].append((str(folder), f"目标文件夹重建失败：{exc}"))

    def run(self, dirs):
        cfg = self.cfg
        summary = {"deleted": [], "failed": [], "skipped": [], "errors": [],
                   "count": 0, "bytes": 0, "log_path": ""}
        self.say()
        banner(f"{TITLE} v{script_version()}")
        label = {"preview": "仅预览（不删除）", "trash": "送回收站",
                 "permanent": "永久删除（不可恢复）"}[cfg.safety]
        self.say(f"  方式：{label}")
        self.say(f"  范围：{'递归子目录' if cfg.recursive else '仅当前层'}"
                 f"{' · 清理空目录' if cfg.prune_empty and not cfg.preview else ''}")
        if cfg.safety == "permanent":
            self.say("  ⚠️  永久删除档：文件不会进入回收站，无法恢复")

        if not dirs:
            self.say("\n  ❌ 未获取到有效的目标文件夹")
            summary["errors"].append(("（无输入）", "未获取到有效的目标文件夹"))
            return summary

        section("筛选条件")
        self.say(f"  扩展名：{cfg.extensions or '不限'}")
        self.say(f"  大小：{'不限' if cfg.size_mode != 'on' else format_range(cfg)}")
        self.say(f"  时间：{'不限' if cfg.time_mode == 'off' else format_time(cfg)}")
        self.say(f"  排除：{cfg.exclude or '无'}")

        try:
            binary = resolve_deletor()
        except FileNotFoundError as exc:
            self.say(f"\n  ❌ {exc}")
            summary["errors"].append(("（环境）", str(exc)))
            return summary

        self.run_dir = self._make_run_dir()
        summary["log_path"] = "" if cfg.preview else str(self.run_dir)
        worker = Deletor(binary)

        section(f"处理 {len(dirs)} 个文件夹")
        for idx, folder in enumerate(dirs, 1):
            ok, reason = guard_dir(folder, cfg)
            if not ok:
                self.say(f"  ⏭️  跳过 {folder} —— {reason}")
                summary["skipped"].append(str(folder))
                continue
            sub = self.run_dir / f"{idx:02d}"
            sub.mkdir(parents=True, exist_ok=True)
            self.say(f"  ▶ [{idx}/{len(dirs)}] {folder}")
            result = worker.clean_one(cfg, folder, sub)
            self._restore_target(cfg, folder, summary)
            if result["error"]:
                self.say(f"    ❌ 失败：{result['error']}")
                summary["errors"].append((str(folder), result["error"]))
                continue
            paths = result["paths"]
            if not paths:
                self.say("    · 没有匹配到文件")
                continue
            if cfg.preview:
                for p in paths[:60]:
                    self.say(f"    📄 待删除 {p}")
                if len(paths) > 60:
                    self.say(f"    …… 另有 {len(paths) - 60} 个")
            else:
                still = worker.verify(paths)
                gone = [p for p in paths if p not in still]
                summary["failed"].extend(still)
                paths = gone
                if still:
                    self.say(f"    ⚠️  {len(still)} 个文件删除未生效（仍存在于原路径）")
            summary["deleted"].extend(paths)
            summary["count"] += len(paths)
            summary["bytes"] += result["freed_bytes"]
            self.say(f"    ✅ {LABEL_VERB[cfg.safety]} {len(paths)} 个 · "
                     f"{human_size(result['freed_bytes'])}")

        self._summarize(summary)
        self._write_run_log()
        return summary

    def _summarize(self, summary):
        section("汇总")
        verb = LABEL_VERB[self.cfg.safety]
        self.say(f"  ✅ {verb} {summary['count']} 个 · {human_size(summary['bytes'])}")
        if summary["failed"]:
            self.say(f"  ⚠️  未生效 {len(summary['failed'])} 个")
        if summary["skipped"]:
            self.say(f"  ⏭️  已跳过 {len(summary['skipped'])} 个文件夹")
        if summary["errors"]:
            self.say(f"  ❌ 失败 {len(summary['errors'])} 个文件夹")
        if not self.cfg.preview and summary["log_path"]:
            self.say(f"  📝 日志：{summary['log_path']}")

    def _write_run_log(self):
        if self.visible:
            return
        try:
            LOG_ROOT.mkdir(parents=True, exist_ok=True)
            stamp = self.run_dir.name if self.run_dir else time.strftime("%Y%m%d_%H%M%S")
            (LOG_ROOT / f"run_{stamp}.log").write_text(
                "\n".join(self.lines) + "\n", encoding="utf-8")
        except OSError:
            pass


LABEL_VERB = {"preview": "待删除", "trash": "已送回收站", "permanent": "已永久删除"}


def format_range(cfg):
    parts = []
    if cfg.min_size:
        parts.append(f"≥{cfg.min_size}")
    if cfg.max_size:
        parts.append(f"≤{cfg.max_size}")
    return " 且 ".join(parts) or "未填（视为不限，已忽略）"


def format_time(cfg):
    if cfg.time_mode == "older":
        return f"早于 {cfg.older}" if cfg.older else "未填（已忽略）"
    if cfg.time_mode == "newer":
        return f"晚于 {cfg.newer}" if cfg.newer else "未填（已忽略）"
    both = []
    if cfg.older:
        both.append(f"早于 {cfg.older}")
    if cfg.newer:
        both.append(f"晚于 {cfg.newer}")
    return " 且 ".join(both) or "未填（已忽略）"


# ==================== 结果交付 ====================

def build_msg(summary, safety):
    verb = {"preview": "待删除", "trash": "已送回收站", "permanent": "已永久删除"}[safety]
    msg = f"{verb} {summary['count']} 个文件 / {human_size(summary['bytes'])}"
    if summary["failed"]:
        msg += f"，{len(summary['failed'])} 个未生效"
    if summary["skipped"]:
        msg += f"，{len(summary['skipped'])} 个文件夹被跳过"
    if summary["errors"]:
        msg += f"，{len(summary['errors'])} 个文件夹失败：{summary['errors'][0][1]}"
    return msg


def envelope_body(summary):
    """业务键在信封顶层平铺；数量/字节按 outputs 声明转字符串。"""
    return {
        "deleted_files": summary["deleted"][:ENVELOPE_LIST_CAP],
        "failed_paths": summary["failed"][:ENVELOPE_LIST_CAP],
        "skipped_dirs": summary["skipped"][:ENVELOPE_LIST_CAP],
        "total_count": str(summary["count"]),
        "freed_bytes": str(summary["bytes"]),
        "freed_size": human_size(summary["bytes"]),
        "safety_mode": summary["safety"],
        "log_path": summary["log_path"],
    }


def write_envelope(env, code, msg, summary):
    path = env.get("output_json")
    if not path or not summary:
        return
    body = {"code": int(code), "msg": str(msg)}
    body.update(envelope_body(summary))
    try:
        Path(path).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_payload(arg_path):
    with open(arg_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def guide():
    banner(f"{TITLE} v{script_version()}")
    print()
    print("  📌 使用说明")
    print("  ── 本脚本按「文件夹 + 筛选条件」批量清理 ──")
    print()
    print("   ① " + GUIDE_URL)
    print("   ② 或在不忙脚本盒子中点击运行，在弹出的窗口里选择文件夹与条件")
    print()
    print("  🔔 方式三档：仅预览 / 送回收站（默认） / 永久删除")
    print()
    pause_exit()


def main():
    global _EXIT_OK
    fix_encoding()
    if len(sys.argv) < 2:
        guide()
        return

    mode = "manual"
    env = {}
    try:
        payload = read_payload(sys.argv[1])
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"❌ 无法读取参数文件：{exc}")
        if len(sys.argv) > 1 and Path(sys.argv[1]).exists():
            pause_exit()
        sys.exit(1)

    env = payload.get("environment") or {}
    mode = env.get("invoke_mode") or "manual"
    cfg = Config.from_dict(payload.get("params") or {})
    data = payload.get("data") or {}
    raw_dirs = data.get("target_dirs") or []
    if isinstance(raw_dirs, str):
        raw_dirs = [raw_dirs]
    dirs = [d for d in dict.fromkeys(str(x) for x in raw_dirs) if d.strip()]

    if mode != "manual":
        hide_console()                # 定时/节点：静默，输出改走日志文件
    rotate_logs()

    app = Cli(cfg, mode, env)
    try:
        summary = app.run(dirs)
    except Exception as exc:           # 兜底：绝不让异常挂住无人值守进程
        app.say(f"\n❌ 执行异常：{exc}")
        app._write_run_log()
        summary = None
        code, msg = 1, f"执行异常：{exc}"
        if mode == "node":
            write_envelope(env, code, msg, None)
            sys.exit(0)                # 节点模式进程退出码恒 0，成败由信封表达
        sys.exit(code)

    summary["safety"] = cfg.safety
    code = 0 if not summary["errors"] and not summary["failed"] else 1
    msg = build_msg(summary, cfg.safety)
    _EXIT_OK = (code == 0)

    if mode == "node":
        write_envelope(env, code, msg, summary)
        sys.exit(0)                    # 业务失败只由信封 code 表达
    if mode == "scheduled":
        write_envelope(env, code, msg, summary)
        sys.exit(code)
    print(f"\n  📣 {msg}", flush=True)
    countdown_exit()


if __name__ == "__main__":
    main()
