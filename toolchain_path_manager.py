"""工具链 PATH 管理器（单文件版）。

把「工具链的可执行入口目录」统一报备到与程序同级的 registered-paths.yaml，
GUI 只是它的可视化编辑器；右栏把报备路径放入**用户级 PATH**。

设计依据：toolchain-path-manager-design.md（v0.8，仅用户级 PATH）。

运行（与系统环境剥离，使用工程内 venv）：
    .\\.venv\\Scripts\\python.exe toolchain_path_manager.py

核心规则（与设计文档一致）：
    R0 配置文件即报备库（唯一事实源）
    R1 白名单与接管：只有报备过的路径才可移出 / 拖动 / 多选
    R2 锚点不变：未报备路径的相对顺序永不改变，落盘前断言
    R3 库内自由排序：左栏拖拽直接写回配置文件
    R4 组序不变：批量操作保持选中项相对顺序
    R5 右栏拖动：仅报备项可拖，落任意插入位或反向拖回左栏=移出
    R6 唯一性：同目录禁止重复报备，移入判重
    R7 待应用机制：右栏改动只进内存，应用 / 确认时全量写回注册表

注册表写入遵循 Microsoft 关于改写 PATH 的标准做法：
    * 键位固定为 HKCU\\Environment\\Path（只写用户级，绝不触碰 HKLM / 其他键）；
    * 读取时记录原始类型，写回保持 REG_EXPAND_SZ / REG_SZ 不变，不做类型转换；
    * 不对 %VAR% 做展开改写，按字面字符串原样写回；
    * 不使用 setx（它会把 REG_EXPAND_SZ 降级为 REG_SZ 并在 1024 字符处截断）；
    * 写入成功后广播 WM_SETTINGCHANGE（lParam = "Environment"），使新进程生效；
    * 落盘为「整键全量写回」，变化时只写一次，不留下任何临时文件。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
import sys
import threading
import time
import uuid

if sys.platform == "win32":                       # 仅 Windows 需要，非 Windows 走内存假实现
    import ctypes
    import winreg

# ============================================================================
# 应用信息与常量
# ============================================================================
__version__ = "2.0.0"
APP_NAME = "ToolchainPathManager"
APP_DISPLAY_NAME = "工具链 PATH 管理器"

REG_SZ = 1                                        # 注册表字符串类型
REG_EXPAND_SZ = 2                                 # 可展开字符串类型（PATH 缺省）
TYPE_NAMES = {REG_SZ: "REG_SZ", REG_EXPAND_SZ: "REG_EXPAND_SZ"}
DEFAULT_REG_TYPE = REG_EXPAND_SZ                  # 用户级 PATH 缺省类型（Microsoft 约定）
PATH_LENGTH_LIMIT = 32000                         # 写回前安全长度阈值（约）

ANCHOR = "anchor"                                 # 未报备路径（只读锚点）
MANAGED = "managed"                               # 报备项（可操作）
USER_LABEL = "用户级"

DEFAULT_CONFIG_NAME = "registered-paths.yaml"
DEFAULT_HEADER = [
    "# Toolchain Path Manager - 已报备的工具链入口目录",
    "# 每一行 = 一个入口目录；顺序 = 报备库内顺序；可手工编辑后在本软件中重新载入",
    "",
]
LOCK = threading.RLock()


def type_name(t: int) -> str:
    return TYPE_NAMES.get(t, f"type-{t}")


# ============================================================================
# 路径规范化与通用工具
# ============================================================================
def norm_path(p: str) -> str:
    """规范化用于判重 / 比较：小写 + 去尾部空格与反斜杠（R6）。"""
    return p.strip().rstrip("\\").lower()


def clean_entry_dir(p: str) -> str:
    """报备时清理用户输入：去引号、去尾部空格与反斜杠。"""
    p = (p or "").strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1].strip()
    return p.rstrip().rstrip("\\")


def gen_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ts() -> float:
    return time.time()


# 插入位描述：where ∈ 'top' | 'bottom' | ('before', <row_uid / toolchain_id>)
def where_top():
    return "top"


def where_bottom():
    return "bottom"


def where_before(uid: str):
    return ("before", uid)


def where_is_top(w) -> bool:
    return w == "top"


def where_is_bottom(w) -> bool:
    return w == "bottom"


# ============================================================================
# 领域模型
# ============================================================================
@dataclasses.dataclass
class Toolchain:
    """报备库条目（配置文件视图）。sort_order 只影响库内展示与批量移入次序（R3）。"""
    id: str
    name: str
    entry_dir: str                # 绝对路径（去尾部反斜杠；保留原串用于写入）
    sort_order: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def norm(self) -> str:
        return norm_path(self.entry_dir)

    @property
    def dir_missing(self) -> bool:
        """软校验：目录不存在 / 不可访问则标黄。含 %VAR% 的路径不做展开，返回 False。"""
        d = self.entry_dir.strip()
        if "%" in d:
            return False
        return not os.path.isdir(d)


@dataclasses.dataclass
class Row:
    """用户级 PATH 中的一行。uid 在本会话内稳定，供选中集 / 拖拽引用。"""
    uid: str
    kind: str                        # ANCHOR / MANAGED
    value_raw: str                   # 注册表中的原始串（用于全量写回）
    norm: str                        # norm_path(value_raw)
    anchor_seq: int | None = None    # 锚点在锚点序列中的序号；managed 为 None
    toolchain_id: str | None = None  # managed 时指向报备项
    display_name: str = ""           # 供展示（managed = 报备名；anchor 为空）

    @property
    def selectable(self) -> bool:
        return self.kind == MANAGED


@dataclasses.dataclass
class BaseItem:
    """最近一次从注册表读取的原始条目（快照），用于锚点不变断言（R2）。"""
    value_raw: str
    norm: str
    was_managed: bool = False


@dataclasses.dataclass
class PathState:
    """用户级 PATH 的工作序列（内存态，可能含待应用变更）+ 快照。"""
    rows: list[Row] = dataclasses.field(default_factory=list)
    reg_type: int = DEFAULT_REG_TYPE
    base_raw: str = ""                 # 快照原始整串（用于判定 PATH 是否变化）
    base_items: list[BaseItem] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class ModelState:
    """整盘内存态快照（撤销用）。library_order 记录报备库顺序；tools_removed 记录该组删除的报备记录。"""
    rows: list[Row] = dataclasses.field(default_factory=list)
    library_order: list[str] | None = None           # toolchain ids（None = 该组未改库）
    tools_removed: list[Toolchain] = dataclasses.field(default_factory=list)

    @classmethod
    def capture(cls, rows: list[Row],
                library_order: list[str] | None = None,
                tools_removed: list[Toolchain] | None = None) -> "ModelState":
        return cls(
            rows=[copy.deepcopy(r) for r in rows],
            library_order=list(library_order) if library_order is not None else None,
            tools_removed=copy.deepcopy(tools_removed or []),
        )


@dataclasses.dataclass
class PendingGroup:
    """一组待应用变更（共享 groupId）。撤销 = 恢复 undo_snapshot。"""
    group_id: str
    description: str
    count: int = 0
    undo_snapshot: ModelState | None = None


@dataclasses.dataclass
class OpResult:
    """引擎操作的统一返回：UI 据此弹提示。"""
    ok: bool = True
    message: str = ""
    warnings: list[str] = dataclasses.field(default_factory=list)

    @classmethod
    def ok_result(cls, message: str = "", warnings: list[str] | None = None) -> "OpResult":
        return cls(ok=True, message=message, warnings=warnings or [])

    @classmethod
    def fail(cls, message: str) -> "OpResult":
        return cls(ok=False, message=message)


# ============================================================================
# 注册表读写（用户级 PATH，Microsoft 标准做法）
# ============================================================================
USER_REG_PATH = r"Environment"                    # HKCU\Environment
VALUE_NAME = "Path"
HWND_BROADCAST = 0xFFFF
WM_SETTINGCHANGE = 0x001A
SMTO_ABORTIFHUNG = 0x0002


class RegistryError(RuntimeError):
    pass


class RegistryApi:
    """真实 / 假的统一接口。返回 (value_or_None, reg_type)。"""

    def read_user(self):
        raise NotImplementedError

    def write_user(self, value: str, reg_type: int) -> None:
        raise NotImplementedError


if sys.platform == "win32":
    _send_message_timeout = ctypes.windll.user32.SendMessageTimeoutW
    _send_message_timeout.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
    ]
    _send_message_timeout.restype = ctypes.c_void_p

    def _broadcast_environment_change() -> None:
        """广播 WM_SETTINGCHANGE（lParam = "Environment"），使环境变更对新进程生效。"""
        try:
            _send_message_timeout(HWND_BROADCAST, WM_SETTINGCHANGE, 0,
                                  "Environment", SMTO_ABORTIFHUNG, 3000, None)
        except Exception:
            pass  # 广播失败不影响写回结果

    class WindowsRegistry(RegistryApi):
        """只读写 HKCU\\Environment\\Path，不触碰 HKLM 与任何其他键值。"""

        def read_user(self):
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_REG_PATH,
                                     0, winreg.KEY_READ)
            except FileNotFoundError:
                return None, None
            try:
                value, rtype = winreg.QueryValueEx(key, VALUE_NAME)
                return value, int(rtype)
            except FileNotFoundError:
                return None, None
            finally:
                winreg.CloseKey(key)

        def write_user(self, value: str, reg_type: int) -> None:
            # 保持原类型：REG_EXPAND_SZ 与 REG_SZ 不互相转换（Microsoft 约定）。
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_REG_PATH,
                                     0, winreg.KEY_SET_VALUE)
            except FileNotFoundError:
                key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, USER_REG_PATH)
            try:
                winreg.SetValueEx(key, VALUE_NAME, 0, reg_type, value)
            finally:
                winreg.CloseKey(key)

else:  # 非 Windows（测试 / 开发预览回退）
    class WindowsRegistry(RegistryApi):
        def read_user(self):
            return None, None

        def write_user(self, value, reg_type):
            pass

    def _broadcast_environment_change() -> None:
        pass


def broadcast_environment_change():
    if sys.platform == "win32":
        _broadcast_environment_change()


def get_registry() -> RegistryApi:
    return WindowsRegistry()


# ============================================================================
# 报备库 = 外部配置文件 registered-paths.yaml（唯一事实源）
# ============================================================================
def program_base_dir() -> str:
    """与「程序本体同级」的目录：打包后为可执行文件所在目录；源码运行 = 脚本所在目录。"""
    if getattr(sys, "frozen", False):               # PyInstaller 等打包产物
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def default_config_path() -> str:
    return os.path.join(program_base_dir(), DEFAULT_CONFIG_NAME)


def derive_display_name(entry_dir: str) -> str:
    """显示名由路径末级派生（`C:\\Program Files\\nodejs` → `nodejs`），不落盘。

    末级为 bin 时（很多工具链入口目录都叫 bin，不便区分）带上上一级目录名，
    如 `C:\\msys64\\ucrt64\\bin` → `ucrt64/bin`；其余情况只显示末级。
    """
    d = (entry_dir or "").strip().rstrip("\\").rstrip()
    if not d:
        return entry_dir or ""
    base = os.path.basename(d)
    if base.lower() == "bin":
        parent = os.path.basename(os.path.dirname(d))
        if parent:
            return f"{parent}/{base}"
    return base or entry_dir


class RegisteredFileStore:
    """registered-paths.yaml 的读写器 + 外部改动检测。

    只重写路径列表区并尽量保留头部注释；写入为直接整文件覆写，
    不生成 .bak / .tmp 等任何临时文件。
    """

    def __init__(self, config_path: str):
        self.path = config_path
        self._tools: dict[str, Toolchain] = {}
        self._header: list[str] = list(DEFAULT_HEADER)
        self._sig: tuple | None = None          # (mtime_ns, size)
        self._loaded = False

    # ---------------------------------------------------------- 基础路径
    @property
    def config_name(self) -> str:
        return os.path.basename(self.path)

    def display_path(self) -> str:
        return self.path

    # ---------------------------------------------------------- 载入
    def load(self) -> list[str]:
        """读取配置文件。返回问题清单（重复行、非法行等），正常为 []。

        若文件不存在：首次启动自动创建空清单（含头部注释）。
        """
        with LOCK:
            issues: list[str] = []
            self._tools = {}
            self._header = []                 # 先收集文件自带头部注释；无则写默认
            if not os.path.exists(self.path):
                self._header = list(DEFAULT_HEADER)
                self.save()                   # 自动创建空清单
                self._sig = self._snapshot()
                self._loaded = True
                return issues
            try:
                with open(self.path, "r", encoding="utf-8-sig") as f:
                    lines = f.readlines()
            except OSError as e:
                self._loaded = False
                return [f"无法读取配置文件：{e}"]
            seen: set[str] = set()
            order = 0
            header_done = False
            for lineno, raw in enumerate(lines, start=1):
                line = raw.strip()
                if not line:
                    if not header_done:
                        self._header.append("")
                    continue
                if line.startswith("#"):
                    if not header_done:
                        self._header.append(raw.rstrip("\r\n"))
                    continue
                if line.startswith("---"):
                    continue
                entry = line[2:].strip() if line.startswith("- ") else line
                if not entry:
                    continue
                header_done = True
                d = clean_entry_dir(entry)
                if not d:
                    continue
                n = norm_path(d)
                if n in seen:
                    issues.append(f"配置文件第 {lineno} 行与已报备目录重复，读取时只显示一条：{d}")
                    continue
                seen.add(n)
                t = Toolchain(id=n, name=derive_display_name(d), entry_dir=d,
                              sort_order=order, created_at=now_ts(), updated_at=now_ts())
                self._tools[t.id] = t
                order += 1
            if not self._header:              # 文件本身无注释 → 写回时补默认头部
                self._header = list(DEFAULT_HEADER)
            self._sig = self._snapshot()
            self._loaded = True
            return issues

    # ---------------------------------------------------------- 保存（直接覆写，无临时文件）
    def save(self) -> None:
        with LOCK:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            body_lines = list(self._header)
            body_lines.extend(f"- {t.entry_dir}" for t in self.sorted())
            text = "\n".join(body_lines).rstrip("\n") + "\n"
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(text)
            self._sig = self._snapshot()

    def _snapshot(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def mark_clean(self) -> None:
        """在外部重载 / 已提示后记录当前磁盘状态，避免重复提示。"""
        self._sig = self._snapshot()

    def external_changed(self) -> bool:
        """外部改动检测：文件 mtime / 大小 与最近一次签名不同。"""
        if not self._loaded or not os.path.exists(self.path):
            return False
        sig = self._snapshot()
        return sig is not None and sig != self._sig

    # ---------------------------------------------------------- 查询
    def sorted(self) -> list[Toolchain]:
        return sorted(self._tools.values(), key=lambda t: t.sort_order)

    def by_id(self, tid: str) -> Toolchain | None:
        return self._tools.get(tid)

    def by_norm(self, n: str) -> Toolchain | None:
        return self._tools.get(n)

    def all_norms(self) -> set[str]:
        return set(self._tools)

    # ---------------------------------------------------------- 写操作（改完由调用方 save()）
    def add(self, entry_dir: str) -> Toolchain:
        d = clean_entry_dir(entry_dir)
        n = norm_path(d)
        order = max((t.sort_order for t in self._tools.values()), default=-1) + 1
        t = Toolchain(id=n, name=derive_display_name(d), entry_dir=d,
                      sort_order=order, created_at=now_ts(), updated_at=now_ts())
        self._tools[t.id] = t
        return t

    def update(self, tid: str, new_dir: str) -> Toolchain | None:
        old = self._tools.get(tid)
        if old is None:
            return None
        d = clean_entry_dir(new_dir)
        n = norm_path(d)
        if n != tid:                                   # 路径改变 → 以新规范化路径为新 id
            if n in self._tools:
                return None                            # 目标目录已存在其他条目
            self._tools.pop(tid, None)
        t = Toolchain(id=n, name=derive_display_name(d), entry_dir=d,
                      sort_order=old.sort_order, created_at=old.created_at, updated_at=now_ts())
        self._tools[t.id] = t
        return t

    def remove(self, tid: str) -> Toolchain | None:
        return self._tools.pop(tid, None)

    def reorder(self, ids_in_order: list[str]) -> None:
        pos = {tid: i for i, tid in enumerate(ids_in_order) if tid in self._tools}
        for t in self._tools.values():
            if t.id in pos:
                t.sort_order = pos[t.id]

    def append_to_tail(self, ids: list[str]) -> None:
        order = max((t.sort_order for t in self._tools.values()), default=-1) + 1
        for tid in ids:
            t = self._tools.get(tid)
            if t is not None:
                t.sort_order = order
                order += 1

    def add_back(self, t: Toolchain) -> None:
        if t.id not in self._tools:
            self._tools[t.id] = t

    def loaded_ok(self) -> bool:
        return self._loaded


# ============================================================================
# 核心规则引擎（纯 Python，不依赖 GUI）
# ============================================================================
@dataclasses.dataclass
class ApplyOutcome:
    ok: bool = True
    written: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)   # 超长被放弃 / 无变化
    failed: list[str] = dataclasses.field(default_factory=list)
    messages: list[str] = dataclasses.field(default_factory=list)


class Engine:
    def __init__(self, registry: RegistryApi | None = None,
                 config_path: str | None = None):
        self.reg: RegistryApi = registry or get_registry()
        # 报备库唯一事实源 = 外部配置文件（与程序同级 registered-paths.yaml）
        self.library = RegisteredFileStore(config_path or default_config_path())
        self.state = PathState()
        self.pending: list[PendingGroup] = []
        self.warnings: list[str] = []
        self.config_issues: list[str] = []
        self.config_ok = True

    # ================================================================ 启动 / 重建
    def start(self) -> OpResult:
        self.warnings = []
        self.pending = []
        self.config_issues = self.library.load()
        self.config_ok = self.library.loaded_ok()
        if self.config_issues:
            self.warnings.extend(f"配置文件：{msg}" for msg in self.config_issues)
        if not self.config_ok:
            self.warnings.append(
                f"配置文件（{self.library.config_name}）未能加载：已进入保护态，"
                "禁止写 PATH 相关操作，请修复后点「重新载入配置」。")
        try:
            self._read_path()
        except Exception as e:  # noqa: BLE001
            self.warnings.append(f"读取注册表失败：{e}")
            self.state = PathState()
        return OpResult.ok_result(warnings=list(self.warnings))

    def _read_path(self) -> None:
        """从注册表重建用户级 PATH：快照 + 工作序列。"""
        raw, rtype = self.reg.read_user()
        rtype = rtype or REG_EXPAND_SZ
        base_items: list[BaseItem] = []
        rows: list[Row] = []
        anchor_no = 0
        for token in (raw or "").split(";"):
            if not token.strip():
                continue
            n = norm_path(token)
            if not n:
                continue
            managed = n in self._lib_norms()
            base_items.append(BaseItem(value_raw=token, norm=n, was_managed=managed))
            if managed:
                tid = self._lib_id_for(n)
                rows.append(Row(uid=gen_id(), kind=MANAGED,
                                value_raw=token, norm=n, toolchain_id=tid,
                                display_name=self._lib_name_for(n)))
            else:
                rows.append(Row(uid=gen_id(), kind=ANCHOR,
                                value_raw=token, norm=n, anchor_seq=anchor_no))
                anchor_no += 1
        self.state.rows = rows
        self.state.reg_type = rtype
        self.state.base_raw = raw or ""
        self.state.base_items = base_items

    def _lib_id_for(self, n: str) -> str | None:
        t = self.library.by_norm(n)
        return t.id if t else None

    def _lib_norms(self) -> set[str]:
        return self.library.all_norms()

    def _lib_name_for(self, n: str) -> str:
        t = self.library.by_norm(n)
        return t.name if t else ""

    # ================================================================ 查询（供 UI / 测试）
    def rows(self) -> list[Row]:
        return self.state.rows

    def pending_count(self) -> int:
        return len(self.pending)

    def pending_entries(self) -> int:
        return sum(g.count for g in self.pending)

    def left_items(self) -> list[Toolchain]:
        """左栏仅展示「未加入 PATH」的报备项（设计 6.1）。"""
        in_path = {r.norm for r in self.rows()}
        return [t for t in self.library.sorted() if t.norm not in in_path]

    def toolchain_in_path(self, tid: str) -> bool:
        t = self.library.by_id(tid)
        if not t:
            return False
        return any(r.toolchain_id == tid for r in self.rows())

    # ---------------------------------------------------------------- 待应用组快照
    def _lib_order(self) -> list[str]:
        return [t.id for t in self.library.sorted()]

    def _begin(self, description: str,
               tools_removed: list[Toolchain] | None = None) -> PendingGroup:
        return PendingGroup(group_id=gen_id(), description=description,
                            undo_snapshot=ModelState.capture(
                                self.rows(), self._lib_order(), tools_removed))

    def _commit(self, group: PendingGroup, count: int) -> None:
        if count <= 0:
            self._restore(group.undo_snapshot)
            return
        group.count = count
        self.pending.append(group)

    def _restore(self, snap: ModelState) -> None:
        self.state.rows = [dataclasses.replace(r) for r in snap.rows]
        if snap.library_order is not None or snap.tools_removed:
            for t in snap.tools_removed:                       # 撤销“删除报备”→ 放回记录
                self.library.add_back(t)
            # 顺序 = 快照顺序 + 快照之后新增的报备（追加到尾部，避免误删后续报备）
            known = set(snap.library_order or [])
            current = self._lib_order()
            order = list(snap.library_order or []) + [tid for tid in current if tid not in known]
            self.library.reorder(order)
            self.library.save()

    # ================================================================ 报备（R6 唯一；报备即接管 R1）
    def register_toolchain(self, dirs) -> OpResult:
        """报备一个 / 一批目录（写入配置文件末尾）。显示名由路径末级派生。"""
        if not self.config_ok:
            return OpResult.fail("配置文件未加载，禁止写入（请先点「重新载入配置」）")
        if isinstance(dirs, str):
            dirs = [dirs]
        added: list[Toolchain] = []
        taken = 0
        duplicates: list[str] = []
        for raw in dirs or []:
            d = clean_entry_dir(raw)
            if not d:
                continue
            n = norm_path(d)
            if self.library.by_norm(n):
                duplicates.append(d)
                continue                          # R6：重复提示跳过，不产生第二行
            t = self.library.add(d)
            added.append(t)
            taken += self._takeover_rows(n, t.id)
        if not added:
            hint = "\n".join(duplicates)
            return OpResult.fail(
                "该目录已报备（同目录禁止重复报备）" if len(duplicates) <= 1
                else f"以下目录均已报备：\n{hint}")
        self.library.save()
        names = "、".join(t.name for t in added)
        msg = f"已写入配置文件，报备 {len(added)} 个目录：{names}"
        if taken:
            msg += "。其中部分目录原本已在 PATH 中，报备后已接管为可管理项"
        return OpResult.ok_result(msg)

    def _takeover_rows(self, norm: str, tid: str) -> int:
        """报备即接管（R1）：把匹配的锚点行转为可操作的报备行。值 / 位置不变，无需写回。"""
        count = 0
        t = self.library.by_id(tid)
        for r in self.rows():
            if r.kind == ANCHOR and r.norm == norm:
                r.kind = MANAGED
                r.toolchain_id = tid
                r.display_name = t.name if t else self._lib_name_for(norm)
                count += 1
        return count

    def update_toolchain(self, tid: str, new_dir: str) -> OpResult:
        """重设入口目录（仅“不在 PATH 中”允许，避免与 PATH 语义冲突；在 PATH 中请先移出）。"""
        if not self.config_ok:
            return OpResult.fail("配置文件未加载，禁止写入")
        if self.toolchain_in_path(tid):
            return OpResult.fail("该报备当前在 PATH 中，请先「移出」后再修改入口目录")
        t = self.library.by_id(tid)
        if not t:
            return OpResult.fail("报备项不存在")
        d = clean_entry_dir(new_dir)
        if not d:
            return OpResult.fail("入口目录不能为空")
        updated = self.library.update(tid, d)
        if updated is None:
            return OpResult.fail("新目录与另一条报备重复")
        self.library.save()
        return OpResult.ok_result(f"已更新报备：{updated.name}")

    def delete_toolchain(self, tid: str) -> OpResult:
        """删除报备。若该目录目前在 PATH 中：移出 + 删除报备（生成待应用组）。"""
        t = self.library.by_id(tid)
        if not t:
            return OpResult.fail("报备项不存在")
        in_rows = [r for r in self.rows() if r.toolchain_id == tid]
        if not in_rows:
            self.library.remove(tid)
            self.library.save()
            return OpResult.ok_result(f"已删除报备：{t.name}")
        group = self._begin(f"删除报备并移出：{t.name}",
                            tools_removed=[dataclasses.replace(t)])
        self.library.remove(tid)
        self.library.save()
        self.state.rows = [r for r in self.rows() if r.toolchain_id != tid]
        self._commit(group, len(in_rows))
        return OpResult.ok_result(
            f"已删除报备 {t.name}，并生成移出待应用变更（应用后从 PATH 移除）")

    # ================================================================ 库内排序（R3 直接持久化）
    def reorder_library(self, visible_ids_in_order: list[str]) -> None:
        """左栏拖拽排序（R3，直接持久化）。

        左栏只展示“未加入 PATH”的子集：把子集按新顺序重排，并保持子集之外
        （在 PATH 中 / 隐藏）条目的绝对位次不变。
        """
        full = [t.id for t in self.library.sorted()]
        sub_positions = [i for i, tid in enumerate(full) if tid in visible_ids_in_order]
        if len(visible_ids_in_order) != len(sub_positions):
            return                                       # 传入子集与现状不符，忽略
        new_full = list(full)
        for pos, tid in zip(sub_positions, visible_ids_in_order):
            new_full[pos] = tid
        self.library.reorder(new_full)
        self.library.save()

    # ================================================================ 移入（按钮通道 → 用户级底部）
    def move_in_selected(self, tids: list[str]) -> OpResult:
        """中央按钮通道：按库内顺序把选中组追加到用户级底部（R4）。"""
        ordered = self._sort_by_library(tids)
        return self._move_in_at(ordered, where_bottom())

    def move_in_drop(self, tids: list[str], where) -> OpResult:
        """跨栏拖入（拖到哪落到哪）快捷通道。"""
        ordered = self._sort_by_library(tids)
        return self._move_in_at(ordered, where)

    def _sort_by_library(self, tids: list[str]) -> list[str]:
        order = {t.id: i for i, t in enumerate(self.library.sorted())}
        return sorted({x for x in tids if x in order}, key=lambda x: order[x])

    def _move_in_at(self, tids: list[str], where) -> OpResult:
        if not tids:
            return OpResult.fail("请先在左栏选中要移入的条目")
        in_path = {r.norm for r in self.rows()}
        skipped: list[str] = []
        usable: list[Toolchain] = []
        for tid in tids:
            t = self.library.by_id(tid)
            if not t:
                continue
            if t.norm in in_path:
                skipped.append(t.name)          # R6 移入判重：已存在则跳过，不拆散组序
                continue
            usable.append(t)
        if not usable:
            return OpResult.ok_result("所选条目均已加入 PATH，无需重复移入",
                                      warnings=skipped or None)

        rows = self.rows()
        if isinstance(where, tuple) and where[0] == "before":
            uid = where[1]
            pos = next((i for i, r in enumerate(rows) if r.uid == uid), None)
            if pos is None:
                return OpResult.fail("目标插入位已失效，请重新操作")
        elif where_is_top(where):
            pos = 0
        else:                                    # bottom 与默认落位一致
            pos = len(rows)

        group = self._begin(f"移入 {len(usable)} 项 → {USER_LABEL} PATH")
        inserts = [Row(uid=gen_id(), kind=MANAGED,
                       value_raw=t.entry_dir, norm=t.norm,
                       toolchain_id=t.id, display_name=t.name) for t in usable]
        rows[pos:pos] = inserts
        self._commit(group, len(inserts))
        warns = [f"{s}：已在 PATH 中，跳过" for s in skipped]
        return OpResult.ok_result(f"已生成移入待应用变更（{len(inserts)} 项）",
                                  warnings=warns or None)

    # ================================================================ 移出（R4：按右栏显示顺序回库尾）
    def move_out_rows(self, uids: list[str]) -> OpResult:
        if not uids:
            return OpResult.fail("请先在右栏选中要移出的报备项")
        ordered = self._display_order(uids)
        targets: list[Row] = []
        for uid in ordered:
            row = self._find_row(uid)
            if row is None:
                continue
            if row.kind == ANCHOR:
                continue                     # 锚点不可操作（R1 / R2）
            targets.append(row)
        if not targets:
            return OpResult.fail("选中的行均不可移出（锚点路径不可操作）")

        group = self._begin(f"移出 {len(targets)} 项（回库尾）")
        tids_ordered: list[str] = []
        seen: set[str] = set()
        for row in targets:
            rows = self.rows()
            idx = next(i for i, r in enumerate(rows) if r.uid == row.uid)
            rows.pop(idx)
            if row.toolchain_id and row.toolchain_id not in seen:
                seen.add(row.toolchain_id)
                tids_ordered.append(row.toolchain_id)
        if tids_ordered:
            self.library.append_to_tail(tids_ordered)     # 回库尾（仍在报备库）
            self.library.save()
        self._commit(group, len(targets))
        return OpResult.ok_result(f"已生成移出待应用变更（{len(targets)} 项回库尾）")

    def move_out_drop(self, uids: list[str], config_where) -> OpResult:
        """反向拖回（右→左）：右栏报备行拖回左栏「报备库」即移出（拖回即移出）。

        从 PATH 移除（生成待应用变更），并把对应报备写回配置文件：
        落到左栏插入位则插入该库位（config_where = 'top' / 'bottom' /
        ('before', toolchain_id)），落到空白区域则追加到文件末尾；
        组内按右栏当前显示顺序（R4 / R5）。锚点不可拖。
        """
        if not uids:
            return OpResult.fail("未选择可移动的行")
        ordered = self._display_order(uids)
        targets: list[Row] = []
        for uid in ordered:
            row = self._find_row(uid)
            if row is None:
                continue
            if row.kind == ANCHOR:
                continue                     # 锚点不可操作（R1 / R2）
            targets.append(row)
        if not targets:
            return OpResult.fail("选中的行均不可移出（锚点路径不可操作）")

        tids_ordered: list[str] = []
        seen: set[str] = set()
        for row in targets:
            if row.toolchain_id and row.toolchain_id not in seen:
                seen.add(row.toolchain_id)
                tids_ordered.append(row.toolchain_id)

        group = self._begin(f"拖回移出 {len(targets)} 项")
        moved_uids = {r.uid for r in targets}
        self.state.rows = [r for r in self.rows() if r.uid not in moved_uids]
        if tids_ordered:
            self._library_reorder_to_slot(tids_ordered, config_where)
            self.library.save()
        self._commit(group, len(targets))
        return OpResult.ok_result(f"已生成拖回移出待应用变更（{len(targets)} 项）")

    def _library_reorder_to_slot(self, tids: list[str], where) -> None:
        """把 tids 这组报备移动到配置文件目标库位（其余保持相对顺序）。"""
        current = [t.id for t in self.library.sorted()]
        moved = [tid for tid in tids if tid in current]
        rest = [tid for tid in current if tid not in set(moved)]
        if isinstance(where, tuple) and where[0] == "before":
            marker = where[1]
            if marker in set(moved):               # 目标在被拖组内 → 追加末尾
                order = rest + moved
            else:
                pos = next((i for i, tid in enumerate(rest) if tid == marker), len(rest))
                order = rest[:pos] + moved + rest[pos:]
        elif where_is_top(where):
            order = moved + rest
        else:                                      # bottom 与默认落位一致
            order = rest + moved
        self.library.reorder(order)

    def _find_row(self, uid: str) -> Row | None:
        return next((r for r in self.rows() if r.uid == uid), None)

    def _display_order(self, uids: list[str]) -> list[str]:
        """右栏全局显示顺序（同层保持原序）。"""
        rank = {r.uid: i for i, r in enumerate(self.rows())}
        return sorted({u for u in uids if u in rank}, key=lambda u: rank[u])

    # ================================================================ 右栏整组拖动（R4 / R5）
    def move_group_rows(self, uids: list[str], where) -> OpResult:
        if not uids:
            return OpResult.fail("未选择可移动的行")
        ordered = self._display_order(uids)
        group_rows: list[Row] = []
        for uid in ordered:
            row = self._find_row(uid)
            if row is None:
                return OpResult.fail("部分行已不存在，请重试")
            if row.kind == ANCHOR:
                return OpResult.fail("锚点路径不可拖动（顺序固定）")
            group_rows.append(row)

        # 目标插入位（删除前坐标）
        if isinstance(where, tuple) and where[0] == "before":
            marker = where[1]
            if marker in set(uids):
                return OpResult.ok_result("位置未变化")
            pos_pre = next((i for i, r in enumerate(self.rows()) if r.uid == marker), None)
            if pos_pre is None:
                return OpResult.fail("目标插入位已失效，请重新操作")
        elif where_is_top(where):
            pos_pre = 0
        else:
            pos_pre = len(self.rows())

        group = self._begin(f"移动 {len(ordered)} 项")
        target_pre = list(self.rows())                   # 删除前的目标行快照
        moved_uids = set(uids)
        self.state.rows = [r for r in self.rows() if r.uid not in moved_uids]
        # 删除后插入位置修正：pos_pre 是按删除前坐标计的，减去被删除且位于其前的行数
        before_count = sum(1 for i, r in enumerate(target_pre)
                           if r.uid in moved_uids and i < pos_pre)
        insert_pos = max(0, min(pos_pre - before_count, len(self.rows())))
        self.state.rows[insert_pos:insert_pos] = group_rows

        if [r.uid for r in self.rows()] == [r.uid for r in target_pre]:
            self._restore(group.undo_snapshot)           # 位置未变 → 取消该组
            return OpResult.ok_result("位置未变化")
        self._commit(group, len(group_rows))
        return OpResult.ok_result(f"已生成移动待应用变更（{len(group_rows)} 项）")

    # ================================================================ 单行 ▲ / ▼
    def step_move(self, uid: str, up: bool) -> OpResult:
        """报备行上移 / 下移一格（跨锚点合法，锚点相对顺序不受影响）。"""
        rows = self.rows()
        idx = next((i for i, r in enumerate(rows) if r.uid == uid), None)
        if idx is None:
            return OpResult.fail("行不存在")
        row = rows[idx]
        if row.kind == ANCHOR:
            return OpResult.fail("锚点路径不可移动")
        target = idx - 1 if up else idx + 1
        if target < 0 or target >= len(rows):
            return OpResult.ok_result("已在边界，无法继续移动")
        group = self._begin("上移一格" if up else "下移一格")
        rows.pop(idx)
        rows.insert(target, row)
        self._commit(group, 1)
        return OpResult.ok_result("已上移一格" if up else "已下移一格")

    # ================================================================ 撤销 / 待应用
    def undo_last(self) -> OpResult:
        if not self.pending:
            return OpResult.fail("没有可撤销的待应用变更")
        g = self.pending.pop()
        self._restore(g.undo_snapshot)
        return OpResult.ok_result(f"已撤销：{g.description}")

    def undo_group(self, gid: str) -> OpResult:
        for i, g in enumerate(self.pending):
            if g.group_id == gid:
                del self.pending[i]
                self._restore(g.undo_snapshot)
                return OpResult.ok_result(f"已撤销整组：{g.description}")
        return OpResult.fail("待应用组不存在")

    def discard_pending(self) -> None:
        """放弃全部待应用变更：回到第一条待应用之前的状态。"""
        if not self.pending:
            return
        snap = self.pending[0].undo_snapshot
        self.pending = []
        self._restore(snap)

    # ================================================================ 重读注册表
    def reread(self) -> OpResult:
        was = bool(self.pending)
        self.pending = []
        try:
            self._read_path()
        except Exception as e:  # noqa: BLE001
            return OpResult.fail(f"重读注册表失败：{e}")
        return OpResult.ok_result("已重读注册表并清空待应用变更" if was else "已重读注册表")

    # ================================================================ 重新载入配置文件（R0）
    def reload_config(self) -> OpResult:
        """重读 registered-paths.yaml 刷新报备库；保留待应用变更，并按文件重新分类右栏行身份。"""
        issues = self.library.load()
        self.config_ok = self.library.loaded_ok()
        self.config_issues = issues
        if not self.config_ok:
            return OpResult.fail(
                f"配置文件（{self.library.config_name}）载入失败，已保留旧清单。\n" + "\n".join(issues))
        self._reclassify_rows_from_config()
        pending_n = self.pending_count()
        msg = f"已重新载入配置文件（{self.library.config_name}）"
        if pending_n:
            msg += f"；保留待应用变更 {pending_n} 组"
        if issues:
            msg += "\n" + "\n".join(issues)
        return OpResult.ok_result(msg)

    def _reclassify_rows_from_config(self) -> None:
        """配置文件即白名单：按当前文件内容重算右栏行身份（锚点 / 报备）。"""
        for r in self.rows():
            t = self.library.by_norm(r.norm)
            if t is not None:
                r.kind = MANAGED
                r.toolchain_id = t.id
                r.display_name = t.name
            else:
                r.kind = ANCHOR
                r.toolchain_id = None
                r.display_name = ""

    # ================================================================ 锚点不变断言（R2）
    def anchor_invariant_check(self) -> list[str]:
        problems: list[str] = []
        lib_norms = self._lib_norms()
        present = {r.norm for r in self.state.rows}
        # 期望锚点 = 快照锚点（未报备）中“仍存在、且至今仍未报备”者，保持原序
        expected = [b.norm for b in self.state.base_items
                    if not b.was_managed and b.norm in present and b.norm not in lib_norms]
        current = [r.norm for r in self.state.rows if r.kind == ANCHOR]
        if current != expected:
            problems.append(f"{USER_LABEL} PATH：未报备路径的相对顺序已变化")
        return problems

    # ================================================================ 应用 / 确认（R7）
    def apply_plan(self) -> tuple[bool, bool]:
        """返回 (是否需写回, 是否超长需确认)。"""
        value = self._target_value()
        if value == self._baseline_value():
            return False, False
        return True, len(value) > PATH_LENGTH_LIMIT

    def _target_value(self) -> str:
        return ";".join(r.value_raw for r in self.rows())

    def _baseline_value(self) -> str:
        if self.state.base_items:
            return ";".join(b.value_raw for b in self.state.base_items)
        return self.state.base_raw or ""

    def apply(self, confirm_long: bool = False) -> ApplyOutcome:
        """全量写回用户级 PATH 整键。confirm_long: 用户已确认“仍然写入”超长 PATH。"""
        to_write, too_long = self.apply_plan()
        if not to_write:
            return ApplyOutcome(ok=True, messages=["没有需要落盘的变更"])

        problems = self.anchor_invariant_check()
        if problems:
            return ApplyOutcome(ok=False, failed=[USER_LABEL],
                                messages=["R2 锚点断言失败：PATH 可能已被外部程序改动，"
                                          "请先「重读注册表」再操作。" + "；".join(problems)])

        if too_long and not confirm_long:
            return ApplyOutcome(ok=True, skipped=[USER_LABEL],
                                messages=[f"{USER_LABEL} PATH：超长，已按你的选择放弃该项"])

        outcome = ApplyOutcome()
        try:
            self.reg.write_user(self._target_value(), self.state.reg_type)
        except PermissionError as e:
            outcome.failed.append(USER_LABEL)
            outcome.messages.append(f"{USER_LABEL} PATH 写入失败（需要权限）：{e}")
        except Exception as e:  # noqa: BLE001
            outcome.failed.append(USER_LABEL)
            outcome.messages.append(f"{USER_LABEL} PATH 写入失败：{e}")

        if outcome.failed:
            outcome.ok = False
            outcome.messages.append("变更仍保持待应用状态，可撤销或再次「应用」重试")
            return outcome

        broadcast_environment_change()
        # 刷新快照并清空待应用
        st = self.state
        st.base_raw = self._target_value()
        st.base_items = [BaseItem(value_raw=r.value_raw, norm=r.norm,
                                  was_managed=r.kind == MANAGED) for r in st.rows]
        self.pending = []
        outcome.written.append(USER_LABEL)
        outcome.ok = True
        outcome.messages.append(f"{USER_LABEL} PATH 已写入")
        return outcome


# ============================================================================
# GUI：自绘控件与拖放列表
# ============================================================================
from PySide6.QtCore import Qt, QMimeData, QPoint, QThread, Signal, QTimer  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor, QDrag, QKeySequence, QPainter, QPen, QShortcut,
)
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

MIME_TYPE = "application/x-tpm"

ROLE_KIND = Qt.ItemDataRole.UserRole          # 'anchor' | 'managed' | 'tool'
ROLE_ID = Qt.ItemDataRole.UserRole + 1        # row uid / toolchain id
ROLE_SEARCH = Qt.ItemDataRole.UserRole + 2    # 过滤用文本

DROP_LINE_COLOR = QColor("#0b57d0")           # 拖放提示线颜色


def make_mime(kind: str, ids: list[str]) -> QMimeData:
    m = QMimeData()
    m.setData(MIME_TYPE, json.dumps({"kind": kind, "ids": ids}).encode("utf-8"))
    return m


def read_mime(mime: QMimeData):
    if not mime or not mime.hasFormat(MIME_TYPE):
        return None
    try:
        data = json.loads(bytes(mime.data(MIME_TYPE)).decode("utf-8"))
        return data.get("kind"), list(data.get("ids", []))
    except (ValueError, UnicodeDecodeError):
        return None


def _chip(text: str, color: str, fg: str = "white") -> QLabel:
    lab = QLabel(text)
    lab.setStyleSheet(
        f"background:{color};color:{fg};border-radius:7px;padding:1px 6px;"
        "font-size:10px;font-weight:600;")
    return lab


class DragList(QListWidget):
    """带自定义载荷拖放的列表。

    kind: 'library'（左栏报备库）| 'path'（右栏 PATH 列表）
    library 接收 tools（内部排序）与 rows（反向拖回=移出）；path 接收 tools + rows。
    """

    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self.owner = None            # 由 MainWindow 注入，用于执行 drop / 刷新
        self._suppress = False
        self._drop_line_y: float | None = None   # 拖放提示线 y（viewport 坐标）
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(False)
        self.setSelectionRectVisible(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setUniformItemSizes(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)

    # ---------------- 拖放提示线（与 MainWindow 落位逻辑一致）
    def _drop_indicator_y(self, pos: QPoint, payload_kind: str) -> float | None:
        """按与真实落位相同的槽位算法，返回提示线的 y 坐标。"""
        if self.owner is None:
            return None
        try:
            if self.kind == "library" and payload_kind == "tools":
                # 库内拖拽排序：插入到第 count_above 个可视条目位置
                return self._y_at_visible_index(self.owner._count_above(self, pos))
            slot = (self.owner._path_slot(self, pos) if self.kind == "path"
                    else self.owner._library_slot(self, pos))
        except Exception:
            return None
        if slot is None:
            return None
        if isinstance(slot, tuple) and slot[0] == "before":
            ident = slot[1]
            for i in range(self.count()):
                it = self.item(i)
                if it.isHidden():
                    continue
                if it.data(ROLE_ID) == ident:
                    r = self.visualItemRect(it)
                    return float(r.top()) if r.isValid() else 0.0
            return 0.0
        if slot == "top":
            for i in range(self.count()):
                it = self.item(i)
                if it.isHidden():
                    continue
                r = self.visualItemRect(it)
                return float(r.top()) if r.isValid() else 0.0
            return 0.0
        # bottom / 默认：最后一行之下
        for i in range(self.count() - 1, -1, -1):
            it = self.item(i)
            if it.isHidden():
                continue
            r = self.visualItemRect(it)
            return float(r.bottom()) if r.isValid() else 0.0
        return 0.0

    def _y_at_visible_index(self, k: int) -> float:
        """第 k 个可视条目顶部；k 超出则取最后一个可视条目底部。"""
        last_bottom = 0.0
        idx = 0
        for i in range(self.count()):
            it = self.item(i)
            if it.isHidden():
                continue
            r = self.visualItemRect(it)
            if not r.isValid():
                continue
            last_bottom = float(r.bottom())
            if idx == k:
                return float(r.top())
            idx += 1
        return last_bottom

    def _set_drop_line(self, y: float | None) -> None:
        if y != self._drop_line_y:
            self._drop_line_y = y
            self.viewport().update()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drop_line_y is None:
            return
        p = QPainter(self.viewport())
        p.setPen(QPen(DROP_LINE_COLOR, 2))
        y = int(max(0.0, min(self._drop_line_y, self.viewport().height() - 1)))
        p.drawLine(0, y, self.viewport().width(), y)
        p.end()

    # ---------------- 供 MainWindow 渲染时调用
    def add_row(self, kind: str, ident: str, widget: QWidget,
                search_text: str = "", selectable: bool = True) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(ROLE_KIND, kind)
        item.setData(ROLE_ID, ident)
        item.setData(ROLE_SEARCH, search_text.lower())
        item.setSizeHint(widget.sizeHint())
        self.addItem(item)
        self.setItemWidget(item, widget)
        if not selectable or kind == "anchor":
            flags = item.flags()
            item.setFlags(flags & ~Qt.ItemFlag.ItemIsSelectable)
        return item

    def has_id(self, ident: str) -> bool:
        for i in range(self.count()):
            if self.item(i).data(ROLE_ID) == ident:
                return True
        return False

    def selected_ids(self) -> list[str]:
        return [it.data(ROLE_ID) for it in self.selectedItems() if it.data(ROLE_ID)]

    def all_ids(self) -> list[str]:
        return [self.item(i).data(ROLE_ID) for i in range(self.count())
                if self.item(i).data(ROLE_ID)]

    def id_at(self, pos: QPoint):
        it = self.itemAt(pos)
        return it.data(ROLE_ID) if it else None

    def select_ids(self, ids: set[str]) -> None:
        self._suppress = True
        try:
            for i in range(self.count()):
                it = self.item(i)
                it.setSelected(it.data(ROLE_ID) in ids and
                               (it.flags() & Qt.ItemFlag.ItemIsSelectable))
        finally:
            self._suppress = False

    # ---------------- 选中与行内勾选框同步
    def _on_selection_changed(self) -> None:
        if self._suppress:
            return
        for i in range(self.count()):
            it = self.item(i)
            w = self.itemWidget(it)
            if w is not None and hasattr(w, "set_checked"):
                w.set_checked(it.isSelected(), emit=False)
        if self.owner:
            self.owner.on_panel_selection_changed(self.kind)

    def _toggle_check_of(self, item: QListWidgetItem, on: bool) -> None:
        """勾选框点击 → 维护扩展选择。"""
        selectable = item.flags() & Qt.ItemFlag.ItemIsSelectable
        if not selectable:
            return
        anchor = item.data(ROLE_ID)
        self._suppress = True
        try:
            ids = self.selected_ids()
            if on:
                if anchor not in ids:
                    ids.append(anchor)
            else:
                ids = [x for x in ids if x != anchor]
            self.clearSelection()
            for i in range(self.count()):
                it = self.item(i)
                if it.data(ROLE_ID) in ids and (it.flags() & Qt.ItemFlag.ItemIsSelectable):
                    it.setSelected(True)
        finally:
            self._suppress = False
        self._on_selection_changed()

    def set_id_checked(self, ident: str, on: bool) -> None:
        """由行内勾选框触发：切换该项的选中态（保持其他选中项）。"""
        for i in range(self.count()):
            it = self.item(i)
            if it.data(ROLE_ID) == ident:
                self._toggle_check_of(it, on)
                return

    # ---------------- 拖放载荷
    def startDrag(self, supportedActions):
        if self.kind == "path":
            # 仅允许拖动 managed 行：锚点在行内不可选，但保险起见再过滤一次
            drag_ids = [it.data(ROLE_ID) for it in self.selectedItems()
                        if it.data(ROLE_KIND) == "managed"]
            if not drag_ids:
                return
            mime = make_mime("rows", drag_ids)
        else:
            drag_ids = self.selected_ids()
            if not drag_ids:
                return
            mime = make_mime("tools", drag_ids)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec_(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction, Qt.DropAction.MoveAction)

    def dragEnterEvent(self, e):
        payload = read_mime(e.mimeData())
        if payload is None or not payload[1]:
            self._set_drop_line(None)
            e.ignore()
            return
        kind, ids = payload
        # 左栏接收：tools（内部排序 / 拖入右栏）与 rows（右栏反向拖回 = 移出）
        if self.kind == "library" and kind not in ("tools", "rows"):
            self._set_drop_line(None)
            e.ignore()
            return
        e.acceptProposedAction()
        self._set_drop_line(self._drop_indicator_y(e.position().toPoint(), kind))

    def dragMoveEvent(self, e):
        payload = read_mime(e.mimeData())
        if payload is None or not payload[1]:
            self._set_drop_line(None)
            e.ignore()
            return
        kind, ids = payload
        if self.kind == "library" and kind not in ("tools", "rows"):
            self._set_drop_line(None)
            e.ignore()
            return
        e.acceptProposedAction()
        self._set_drop_line(self._drop_indicator_y(e.position().toPoint(), kind))

    def dragLeaveEvent(self, e):
        super().dragLeaveEvent(e)
        self._set_drop_line(None)

    def dropEvent(self, e):
        self._set_drop_line(None)
        payload = read_mime(e.mimeData())
        if payload is None:
            e.ignore()
            return
        kind, ids = payload
        source = e.source()
        if not self.owner:
            e.ignore()
            return
        e.acceptProposedAction()
        self.owner.handle_drop(self.kind, kind, ids, source, self, e.position().toPoint())

    # ---------------- 行右键菜单
    def _on_context_menu(self, pos: QPoint):
        if self.owner:
            self.owner.on_list_context_menu(self.kind, self, pos)


class LibraryRow(QFrame):
    """左栏条目：⠿ 名称 / 路径 / 目录缺失标黄 / 勾选。"""

    def __init__(self, toolchain, selected: bool, callbacks):
        super().__init__()
        self.setObjectName("row")
        self.toolchain_id = toolchain.id
        self.callbacks = callbacks
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(6)

        self._cb = QCheckBox()
        self._cb.setToolTip("勾选用于批量移入 / 拖入")
        self._cb.toggled.connect(lambda on: self._emit_toggle(on))
        lay.addWidget(self._cb)

        handle = QLabel("⠿")
        handle.setStyleSheet("color:#8a8f98;font-size:13px;")
        handle.setToolTip("拖拽 = 库内排序；拖到右栏 = 移入")
        lay.addWidget(handle)

        v = QVBoxLayout()
        v.setSpacing(0)
        name = QLabel(toolchain.name)
        name.setStyleSheet("font-weight:600;font-size:12px;")
        v.addWidget(name)
        path = QLabel(toolchain.entry_dir)
        path.setObjectName("path")
        v.addWidget(path)
        lay.addLayout(v, 1)

        if toolchain.dir_missing:
            warn = _chip("目录不存在", "#b25e09")
            warn.setToolTip("目录不存在 / 不可访问（软校验，可一键重设路径）")
            lay.addWidget(warn)

        lay.addWidget(_chip("报备库", "#9aa0a8"))
        self.set_checked(selected, emit=False)

    def _emit_toggle(self, on: bool):
        if self._cb.property("_prog"):
            return
        self.callbacks.on_toggle(self.toolchain_id, bool(on))

    def set_checked(self, on: bool, emit: bool = True):
        self._cb.setProperty("_prog", not emit)
        self._cb.setChecked(on)
        self._cb.setProperty("_prog", False)


class PathRow(QFrame):
    """右栏 PATH 行：锚点（锁定）或报备项（可勾选 / ▲▼ / 拖拽 / 右键）。"""

    def __init__(self, row, selected: bool, callbacks, missing: bool = False):
        super().__init__()
        self.row_uid = row.uid
        self.kind = row.kind
        self.callbacks = callbacks
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 2, 6, 2)
        lay.setSpacing(6)

        if row.kind == "managed":
            self._cb = QCheckBox()
            self._cb.setToolTip("勾选用于批量移出 / 整组拖动")
            self._cb.toggled.connect(lambda on: self._emit_toggle(on))
            lay.addWidget(self._cb)
            lay.addWidget(_chip("报备", "#1466d9"))
            h = QLabel("⠿")
            h.setStyleSheet("color:#8a8f98;font-size:13px;")
            h.setToolTip("拖到插入位换位，或拖回左栏「报备库」= 移出")
            lay.addWidget(h)
        else:
            pad = QWidget()
            pad.setFixedWidth(30)
            lay.addWidget(pad)
            lay.addWidget(_chip("锚点", "#8a8f98", "#ffffff"))
            lock = QLabel("🔒")
            lock.setToolTip("未报备路径：顺序固定，不可移动 / 删除 / 勾选")
            lay.addWidget(lock)

        v = QVBoxLayout()
        v.setSpacing(0)
        name = QLabel(row.display_name if row.kind == "managed" else row.value_raw)
        if row.kind == "managed":
            name.setStyleSheet("font-weight:600;font-size:12px;")
        else:
            name.setStyleSheet("color:#6b7280;font-size:12px;")
        v.addWidget(name)
        path = QLabel(row.value_raw if row.kind == "managed" else "")
        path.setObjectName("path")
        v.addWidget(path)
        lay.addLayout(v, 1)

        if row.kind == "managed":
            if missing:
                lay.addWidget(_chip("目录不存在", "#b25e09"))
            up = self._arrow("▲", "上移一格")
            down = self._arrow("▼", "下移一格")
            up.clicked.connect(lambda: callbacks.on_step(row.uid, up=True))
            down.clicked.connect(lambda: callbacks.on_step(row.uid, up=False))
            lay.addWidget(up)
            lay.addWidget(down)

    def _emit_toggle(self, on: bool):
        if self._cb.property("_prog"):
            return
        self.callbacks.on_toggle(self.row_uid, bool(on))

    @staticmethod
    def _arrow(text: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setFixedSize(20, 20)
        b.setAutoRaise(True)
        return b

    def set_checked(self, on: bool, emit: bool = True):
        if self.kind != "managed":
            return
        self._cb.setProperty("_prog", not emit)
        self._cb.setChecked(on)
        self._cb.setProperty("_prog", False)


# ============================================================================
# GUI：通用对话框
# ============================================================================
class PathsDialog(QDialog):
    """报备新目录 / 重设路径：只需要目录（可多行批量粘贴；单目录模式=编辑）。

    显示名由路径末级自动派生，不落盘；配置文件不保存名称字段。
    """

    def __init__(self, parent=None, title="报备新目录",
                 paths: list[str] | None = None, single: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(520)
        self._single = single
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.dir_edit = QPlainTextEdit()
        self.dir_edit.setPlaceholderText(r"例如 C:\Program Files\nodejs" + (""
            if single else "\n可每行一个目录，批量粘贴后一次报备"))
        if paths:
            self.dir_edit.setPlainText("\n".join(paths))
        if single:
            self.dir_edit.setMaximumHeight(70)
        else:
            self.dir_edit.setMaximumHeight(150)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        box = QWidget()
        h = QHBoxLayout(box)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.dir_edit, 1)
        h.addWidget(browse)
        form.addRow("入口目录", box)
        lay.addLayout(form)

        tip = QLabel("说明：只保存“可执行入口目录”（node.exe / python.exe 所在目录）。\n"
                     "写回文件：registered-paths.yaml（与程序同级）；显示名由路径末级派生。\n"
                     "同目录（不区分大小写 / 尾部反斜杠）只保留一条；若已在 PATH 中，报备即接管。")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.preview = QLabel("")
        self.preview.setObjectName("muted")
        lay.addWidget(self.preview)
        self.dir_edit.textChanged.connect(self._update_preview)
        self._update_preview()

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _update_preview(self):
        ps = self.paths()
        if ps and not self._single:
            self.preview.setText("将追加到配置末尾：" + "  ".join(
                f"{derive_display_name(p)}({p})" for p in ps))
        elif ps:
            self.preview.setText(f"显示名：{derive_display_name(ps[0])}")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(
            self, "选择工具链可执行目录", self.dir_edit.toPlainText().strip() or "")
        if d:
            self.dir_edit.setPlainText(d)

    def paths(self) -> list[str]:
        return [ln.strip() for ln in self.dir_edit.toPlainText().splitlines()
                if ln.strip()]

    def values(self):
        ps = self.paths()
        return ps[0] if self._single else ps


def info(parent, text: str, title: str = "提示"):
    QMessageBox.information(parent, title, text)


def warn(parent, text: str, title: str = "警告"):
    QMessageBox.warning(parent, title, text)


def error(parent, text: str, title: str = "错误"):
    QMessageBox.critical(parent, title, text)


def confirm(parent, text: str, title: str = "确认", yes="确认", no="取消") -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, text,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.button(QMessageBox.StandardButton.Yes).setText(yes)
    box.button(QMessageBox.StandardButton.No).setText(no)
    return box.exec() == QMessageBox.StandardButton.Yes


# ============================================================================
# GUI：主窗口（三区布局 + 底部应用操作条）
# ============================================================================
APP_QSS = """
* { font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif; font-size: 12px; }
QLabel#apptitle { font-size: 16px; font-weight: 700; color: #1f2937; }
QLabel#headline { font-size: 13px; font-weight: 600; color: #111827; }
QLabel#muted   { color: #6b7280; font-size: 11px; }
QLabel#status  { color: #0b57d0; }
QLabel#path    { color: #6b7280; font-size: 11px; font-family: Consolas, monospace; }
QListWidget { border: 1px solid #dfe3ea; border-radius: 6px; background: #ffffff;
              outline: none; padding: 2px; }
QListWidget::item { border-bottom: 1px solid #f2f4f7; border-radius: 4px; }
QListWidget::item:selected { background: #e8f0fe; }
QListWidget::item:selected:!active { background: #e8f0fe; }
QListWidget::item:hover { background: #f7f9fc; }
QPushButton { padding: 4px 10px; border-radius: 5px; border: 1px solid #cdd3dc;
              background: #ffffff; }
QPushButton:hover { background: #f2f6fc; border-color: #8ab4f8; }
QPushButton:disabled { color: #a8adb5; background: #f7f8fa; }
QPushButton#primary { background: #0b57d0; color: #fff; border: 1px solid #0b57d0; }
QPushButton#primary:hover { background: #1765d8; }
QPushButton#danger  { color: #b3261e; border-color: #f2b8b5; }
QLineEdit { border: 1px solid #cdd3dc; border-radius: 5px; padding: 3px 6px;
            background: #ffffff; }
QToolButton { border: none; color: #4b5563; }
QToolButton:hover { background: #eef2f7; border-radius: 4px; }
QFrame#card { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 6px; }
QSplitter::handle { background: transparent; }
"""


class ApplyWorker(QThread):
    """应用 / 确认：在工作线程写注册表，避免写入期间冻结界面。"""
    done = Signal(object)

    def __init__(self, engine: Engine, confirm_long: bool, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.confirm_long = confirm_long

    def run(self):
        self.done.emit(self.engine.apply(confirm_long=self.confirm_long))


class MainWindow(QWidget):
    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.setWindowTitle(APP_DISPLAY_NAME)
        self.resize(1180, 760)
        self._sel_left: set[str] = set()
        self._sel_right: set[str] = set()
        self._pending_apply_ok = False       # 确认成功后退出
        self._applying = False               # 写盘期间禁止一切变更操作（防 UI 与 worker 竞态）
        self._build_ui()
        self._refresh(keep_path_scroll=False)
        self._install_shortcuts()
        self._start_config_watcher()

    def _start_config_watcher(self):
        """轮询配置文件外部改动：提示是否重新载入，不自动覆盖界面状态。"""
        self._asking_reload = False
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._check_config_external)
        self._watch_timer.start(1500)

    def _check_config_external(self):
        if self._asking_reload or self._applying or not self.isVisible():
            return
        if self.engine.library.external_changed():
            self._asking_reload = True
            again = confirm(
                self,
                f"配置文件（{self.engine.library.path}）已被外部修改。\n"
                "是否重新载入？选择「否」保留界面当前状态（继续编辑将覆盖文件）。",
                "配置文件外部改动", yes="重新载入", no="暂不")
            self._asking_reload = False
            if again:
                self._on_reload_config(quiet=False)
            else:
                self.engine.library.mark_clean()   # 本次改动不再重复提示

    # ================================================================ UI 骨架
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        # ---- 顶栏
        top = QHBoxLayout()
        title = QLabel(APP_DISPLAY_NAME)
        title.setObjectName("apptitle")
        top.addWidget(title)
        top.addSpacing(12)
        self.sel_label = QLabel("已选 0 项（左 0 + 右 0）")
        self.sel_label.setObjectName("muted")
        top.addWidget(self.sel_label)
        self.config_label = QLabel("")
        self.config_label.setObjectName("muted")
        top.addWidget(self.config_label)
        top.addStretch(1)

        top.addWidget(QLabel("过滤 PATH："))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("按名称 / 路径过滤（不影响操作）")
        self.filter_edit.setMaximumWidth(300)
        self.filter_edit.textChanged.connect(self._apply_filter)
        top.addWidget(self.filter_edit)

        reload_cfg = QPushButton("⟳ 重新载入配置")
        reload_cfg.setToolTip("重读 registered-paths.yaml，刷新报备库（保留待应用变更）")
        reload_cfg.clicked.connect(self._on_reload_config)
        top.addWidget(reload_cfg)

        reread = QPushButton("↻ 重读注册表")
        reread.setToolTip("以注册表为事实源重建右栏并清空待应用变更（配置文件不动）")
        reread.clicked.connect(self._on_reread)
        top.addWidget(reread)
        root.addLayout(top)

        # ---- 三区
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_left())
        split.addWidget(self._build_center())
        split.addWidget(self._build_right())
        split.setSizes([380, 96, 640])
        root.addWidget(split, 1)

        # ---- 底部操作条
        bottom = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        bottom.addWidget(self.status_label)
        bottom.addStretch(1)

        self.pending_btn = QPushButton("待应用 0 项")
        self.pending_btn.setToolTip("查看 / 撤销待应用变更（Ctrl+Z 撤销最近一组）")
        self.pending_btn.clicked.connect(self._show_pending_dialog)
        bottom.addWidget(self.pending_btn)

        self.apply_btn = QPushButton("应用")
        self.apply_btn.setToolTip("全量写回用户级 PATH 整键，窗口保持打开")
        self.apply_btn.clicked.connect(lambda: self._do_apply(close_after=False))
        bottom.addWidget(self.apply_btn)

        self.confirm_btn = QPushButton("确认")
        self.confirm_btn.setToolTip("同「应用」，成功后关闭程序")
        self.confirm_btn.clicked.connect(lambda: self._do_apply(close_after=True))
        bottom.addWidget(self.confirm_btn)

        self.exit_btn = QPushButton("退出")
        self.exit_btn.clicked.connect(self.close)
        bottom.addWidget(self.exit_btn)
        root.addLayout(bottom)

    def _build_left(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        self.lib_head = QLabel("我的报备库")
        self.lib_head.setObjectName("headline")
        head.addWidget(self.lib_head)
        head.addStretch(1)
        add_btn = QPushButton("＋ 报备新目录")
        add_btn.clicked.connect(self._on_register)
        self.add_btn = add_btn
        head.addWidget(add_btn)
        v.addLayout(head)

        hint = QLabel("左栏拖拽 = 库内排序（实时写回配置文件）；把条目拖到右栏插入位 = 快捷移入")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self.lib_list = DragList("library")
        self.lib_list.owner = self
        v.addWidget(self.lib_list, 1)
        return box

    def _build_center(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(4, 0, 4, 0)
        v.addStretch(1)
        self.move_in_btn = QPushButton("移入 →")
        self.move_in_btn.setToolTip("把左栏选中组按库内顺序追加到用户级 PATH 底部")
        self.move_in_btn.clicked.connect(self._on_move_in)
        v.addWidget(self.move_in_btn)
        self.move_in_note = QLabel("→ 用户级底部")
        self.move_in_note.setObjectName("muted")
        self.move_in_note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.move_in_note)
        v.addSpacing(24)
        self.move_out_btn = QPushButton("← 移出")
        self.move_out_btn.setToolTip("把右栏选中报备项移出 PATH，按原顺序追加回报备库尾")
        self.move_out_btn.clicked.connect(self._on_move_out)
        v.addWidget(self.move_out_btn)
        self.move_out_note = QLabel("移出 N 项回库尾")
        self.move_out_note.setObjectName("muted")
        self.move_out_note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.move_out_note)
        v.addSpacing(18)
        undo = QPushButton("撤销")
        undo.setToolTip("撤销最近一组待应用变更（Ctrl+Z）")
        undo.clicked.connect(self._on_undo)
        self.undo_btn = undo
        v.addWidget(undo)
        self.undo_note = QLabel("仅待应用阶段")
        self.undo_note.setObjectName("muted")
        self.undo_note.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        v.addWidget(self.undo_note)
        v.addStretch(1)
        return box

    def _build_right(self) -> QWidget:
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        title = QLabel("PATH 总览")
        title.setObjectName("headline")
        head.addWidget(title)
        lvl = QLabel(f"{USER_LABEL} PATH · 本用户生效")
        lvl.setObjectName("muted")
        head.addWidget(lvl)
        head.addStretch(1)
        v.addLayout(head)
        self.path_list = DragList("path")
        self.path_list.owner = self
        v.addWidget(self.path_list, 1)
        self.right_hint = QLabel("")
        self.right_hint.setObjectName("muted")
        self.right_hint.setWordWrap(True)
        v.addWidget(self.right_hint)
        return box

    def _install_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Z"), self, activated=self._on_undo)
        QShortcut(QKeySequence("Ctrl+A"), self, activated=self._select_all_path)

    # ================================================================ 渲染
    def _row_missing(self, row: Row) -> bool:
        if row.kind != MANAGED or not row.toolchain_id:
            return False
        t = self.engine.library.by_id(row.toolchain_id)
        return bool(t and t.dir_missing)

    def _rebuild_left(self):
        keep = self._sel_left & {t.id for t in self.engine.left_items()}
        self._sel_left = keep
        self.lib_list.clear()
        items = self.engine.left_items()
        for t in items:
            w = LibraryRow(t, t.id in keep, callbacks=self)
            w.set_checked(t.id in keep, emit=False)
            self.lib_list.add_row("tool", t.id, w,
                                  search_text=f"{t.name} {t.entry_dir}")
        cfg = self.engine.library.config_name
        tag = "" if self.engine.config_ok else " · ⚠ 配置未加载（保护态）"
        self.lib_head.setText(f"报备库({cfg}) · 未加入 {len(items)} 项{tag}")
        self.config_label.setText(f"配置文件：{self.engine.library.path}"
                                  if self.engine.config_ok else "配置文件：未加载")

    def _rebuild_path(self):
        keep = self._sel_right
        self.path_list.clear()
        for r in self.engine.rows():
            sel = r.uid in keep and r.kind == MANAGED
            w = PathRow(r, sel, self, missing=self._row_missing(r))
            w.set_checked(sel, emit=False)
            kind_txt = "报备" if r.kind == MANAGED else "锚点"
            self.path_list.add_row(
                r.kind, r.uid, w,
                search_text=f"{r.display_name} {r.value_raw} {kind_txt}",
                selectable=(r.kind == MANAGED))
        self.path_list.select_ids(self._sel_right)
        self._apply_filter(self.filter_edit.text())
        self._update_right_hint()

    def _refresh(self, keep_path_scroll: bool = True):
        pos = self.path_list.verticalScrollBar().value() if keep_path_scroll else 0
        self._rebuild_left()
        self._rebuild_path()
        self.path_list.verticalScrollBar().setValue(min(pos, self.path_list.verticalScrollBar().maximum()))
        self._update_status()

    def _set_mutation_enabled(self, on: bool) -> None:
        """批量开关所有会变更引擎状态的按钮（写盘期间全部禁用）。

        按按钮文案动态收集，避免依赖按钮引用被改动。
        """
        btns = getattr(self, "_mut_btns", None)
        if btns is None:
            names = ("报备新目录", "重新载入配置", "重读注册表",
                     "移入", "移出", "撤销", "待应用", "应用", "确认", "退出")
            btns = [b for b in self.findChildren(QPushButton)
                    if any(n in b.text() for n in names)]
            self._mut_btns = btns
        for b in btns:
            b.setEnabled(on)

    def _update_status(self):
        lsel = len(self._sel_left)
        rsel = len(self._sel_right)
        self.sel_label.setText(f"已选 {lsel + rsel} 项（左 {lsel} + 右 {rsel}）")

        pend = self.engine.pending_count()
        entries = self.engine.pending_entries()
        parts = [f"待应用 {pend} 组 / {entries} 项"]
        missing = [t for t in self.engine.left_items() if t.dir_missing]
        missing += [t for t in self.engine.library.sorted()
                    if self.engine.toolchain_in_path(t.id) and t.dir_missing]
        if missing:
            parts.append(f"· ⚠ {len(missing)} 个目录不存在（标黄）")
        self.status_label.setText("  |  ".join(parts))

        self.pending_btn.setText(f"待应用 {entries} 项" if entries else "待应用 0 项")
        self.pending_btn.setEnabled(entries > 0)
        self.apply_btn.setEnabled(entries > 0)
        self.confirm_btn.setEnabled(entries > 0)

        self.move_in_btn.setText(f"移入 →（{lsel} 项）" if lsel else "移入 →")
        self.move_out_btn.setText(f"← 移出（{rsel} 项）" if rsel else "← 移出")
        self.move_out_btn.setEnabled(rsel > 0)

        if self._applying:                       # 写盘期间保持全禁，避免旁路重新启用
            self._set_mutation_enabled(False)

    def _update_right_hint(self):
        self.right_hint.setText(
            "灰色【锚点】行是未报备的既有路径：顺序固定，不可勾选 / 移动 / 删除。"
            "报备行可多选整组拖动落位，或直接拖回左栏「报备库」= 移出。")

    # ================================================================ 选中回调（widgets 调用）
    def on_panel_selection_changed(self, kind: str):
        if kind == "library":
            self._sel_left = set(self.lib_list.selected_ids())
        else:
            self._sel_right = set(self.path_list.selected_ids())
        self._update_status()

    def on_toggle(self, ident: str, on: bool):
        # 由行内勾选框触发（ident 可能是 toolchain id 或 row uid）
        if self.lib_list.has_id(ident):
            self.lib_list.set_id_checked(ident, on)
        elif self.path_list.has_id(ident):
            self.path_list.set_id_checked(ident, on)
        self.on_panel_selection_changed("library")
        self.on_panel_selection_changed("path")

    def on_step(self, uid: str, up: bool):
        if self._applying:
            return
        self._run_op(self.engine.step_move(uid, up))

    # ================================================================ 右键菜单
    def on_list_context_menu(self, kind: str, listw: DragList, pos):
        if self._applying:
            return
        item = listw.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        rkind = item.data(ROLE_KIND)
        ident = item.data(ROLE_ID)
        if kind == "library":
            t = self.engine.library.by_id(ident)
            if not t:
                return
            menu.addAction(f"编辑：{t.name}", lambda: self._on_edit(t.id))
            menu.addAction("删除报备", lambda: self._on_delete_library(t.id))
            menu.addSeparator()
            menu.addAction("移入 → 用户级底部", lambda: self._run_op(
                self.engine.move_in_selected([t.id])))
        else:
            if rkind == "anchor":
                act = menu.addAction("锚点路径顺序固定（只读，不可操作）")
                act.setEnabled(False)
            else:
                menu.addAction("移出本行（回库尾）", lambda: self._run_op(
                    self.engine.move_out_rows([ident])))
                menu.addAction("删除报备并移出 PATH…", lambda: self._on_delete_library_path(ident))
                menu.addSeparator()
                menu.addAction("上移一格", lambda: self._run_op(self.engine.step_move(ident, True)))
                menu.addAction("下移一格", lambda: self._run_op(self.engine.step_move(ident, False)))
        menu.exec(listw.viewport().mapToGlobal(pos))

    # ================================================================ 拖放（widgets 调用）
    def handle_drop(self, target_kind: str, payload_kind: str, ids: list[str],
                    source, target_list: DragList, pos):
        if not ids:
            return
        # 左栏内部排序
        if target_kind == "library" and payload_kind == "tools":
            current = target_list.all_ids()
            dragged = [x for x in current if x in set(ids)]
            if not dragged or len(set(dragged)) != len(set(ids)):
                return
            count_above = self._count_above(target_list, pos)   # 落点之上共有多少可视条目
            dragged_above = len([x for x in dragged
                                 if current.index(x) < count_above])
            new_idx = max(0, count_above - dragged_above)
            rest = [x for x in current if x not in set(dragged)]
            new_order = rest[:new_idx] + dragged + rest[new_idx:]
            if new_order == current:
                return
            self.engine.reorder_library(new_order)
            self._refresh()
            return
        # 反向拖回：右栏报备行拖回左栏「报备库」= 移出（拖回即移出）
        if target_kind == "library" and payload_kind == "rows":
            where = self._library_slot(target_list, pos)
            if where is None:
                return
            QTimer.singleShot(0, lambda: self._finish_drop(
                payload_kind, ids, where, mode="library"))
            return
        # 落到右栏
        if target_kind != "path":
            return
        where = self._path_slot(target_list, pos)
        if where is None:
            return
        QTimer.singleShot(0, lambda: self._finish_drop(payload_kind, ids, where))

    def _finish_drop(self, payload_kind: str, ids: list[str], where, mode="path"):
        if mode == "library":
            res = self.engine.move_out_drop(ids, where)
        elif payload_kind == "rows":
            res = self.engine.move_group_rows(ids, where)
        else:
            res = self.engine.move_in_drop(ids, where)
        self._after_result(res, quiet=True)

    def _count_above(self, listw: DragList, pos) -> int:
        """落点 y 之上共有多少（未隐藏）条目。"""
        count = 0
        for i in range(listw.count()):
            it = listw.item(i)
            if it.isHidden():
                continue
            rect = listw.visualItemRect(it)
            if not rect.isValid():
                continue
            if pos.y() < rect.top() + rect.height() / 2:
                break
            count += 1
        return count

    def _path_slot(self, listw: DragList, pos):
        """根据落点 y 计算插入位：'top' / 'bottom' / ('before', row_uid)。"""
        n = listw.count()
        rects = []
        for i in range(n):
            it = listw.item(i)
            if it.isHidden():
                continue
            rect = listw.visualItemRect(it)
            if not rect.isValid():
                continue
            rects.append((i, it, rect))
        if not rects:
            return "top"
        # 找包含 pos 的行，或第一个 top > pos 的行（即落在某行上方间隙）
        target_idx = None
        for i, it, rect in rects:
            if pos.y() < rect.top():
                target_idx = i
                break
            if rect.top() <= pos.y() <= rect.bottom():
                target_idx = i
                break
        if target_idx is None:                      # 最底部
            return "bottom"

        it = listw.item(target_idx)
        rect = listw.visualItemRect(it)
        if not (pos.y() > rect.top() + rect.height() / 2):   # 落在该行上半 → 排到它之前
            return where_before(it.data(ROLE_ID))
        # 下半 → 排到下一行之前（即落在该行之后）
        nxt = target_idx + 1
        while nxt < n:
            nit = listw.item(nxt)
            if nit.isHidden():
                nxt += 1
                continue
            return where_before(nit.data(ROLE_ID))
        return "bottom"

    def _library_slot(self, listw: DragList, pos):
        """反向拖回时按落点 y 计算配置文件插入位：'top' / 'bottom' / ('before', toolchain_id)。

        左栏每行 ROLE_ID = toolchain id；落到行间隙 = 插入该库位，落到空白区域 = 追加文件末尾。
        """
        n = listw.count()
        rects = []
        for i in range(n):
            it = listw.item(i)
            if it.isHidden():
                continue
            rect = listw.visualItemRect(it)
            if not rect.isValid():
                continue
            rects.append((i, it, rect))
        if not rects:                                  # 左栏为空 → 文件末尾
            return "bottom"
        target_idx = None
        for i, it, rect in rects:
            if pos.y() < rect.top():
                target_idx = i
                break
            if rect.top() <= pos.y() <= rect.bottom():
                target_idx = i
                break
        if target_idx is None:                         # 空白区域（所有行之下）→ 追加末尾
            return "bottom"
        it = listw.item(target_idx)
        rect = listw.visualItemRect(it)
        if not (pos.y() > rect.top() + rect.height() / 2):   # 该行上半 → 插入该行之前
            return where_before(it.data(ROLE_ID))
        # 下半 → 插入下一行之前；若已是最后一行则追加末尾
        nxt = target_idx + 1
        while nxt < n:
            nit = listw.item(nxt)
            if nit.isHidden():
                nxt += 1
                continue
            return where_before(nit.data(ROLE_ID))
        return "bottom"

    # ================================================================ 中央按钮 & 工具栏动作
    def _on_move_in(self):
        ids = self.lib_list.selected_ids()
        if not ids:
            info(self, "请先在左栏勾选 / 选中要移入的报备项")
            return
        if len(ids) > 1:
            names = [self.engine.library.by_id(x).name for x in ids
                     if self.engine.library.by_id(x)]
            if not confirm(self, "将按库内顺序批量移入以下条目到用户级 PATH 底部：\n"
                                + "\n".join(f"• {x}" for x in names),
                          "批量移入预览", yes="移入", no="取消"):
                return
        self._after_result(self.engine.move_in_selected(ids))

    def _on_move_out(self):
        uids = self.path_list.selected_ids()
        uids = [uid for uid in uids if self._row_by_uid(uid)]
        if not uids:
            info(self, "请先在右栏选中要移出的报备项（锚点不可选）")
            return
        rows = [self._row_by_uid(u) for u in uids]
        names = [r.display_name or r.value_raw for r in rows if r]
        if len(names) > 1 and not confirm(
                self, "将按右栏当前顺序批量移出以下条目（回库尾）：\n"
                + "\n".join(f"• {x}" for x in names), "批量移出预览",
                yes="移出", no="取消"):
            return
        self._after_result(self.engine.move_out_rows(uids))

    def _row_by_uid(self, uid: str):
        return next((r for r in self.engine.rows() if r.uid == uid), None)

    def _on_undo(self):
        if self._applying:
            return
        if self.engine.pending_count() == 0:
            return
        g = self.engine.pending[-1]
        if g.count > 1 and not confirm(
                self, f"将撤销最近一组共 {g.count} 项：{g.description}",
                "撤销整组", yes="撤销整组", no="取消"):
            return
        self._after_result(self.engine.undo_last())

    def _on_reread(self):
        self._after_result(self.engine.reread())

    def _on_reload_config(self, quiet: bool = True):
        res = self.engine.reload_config()
        if res.ok:
            self.engine.library.mark_clean()
        self._after_result(res, quiet=quiet)

    def _on_register(self):
        dlg = PathsDialog(self, title="报备新目录")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dirs = dlg.values()
        if not dirs:
            return
        res = self.engine.register_toolchain(dirs)
        self._after_result(res)

    def _on_edit(self, tid: str):
        t = self.engine.library.by_id(tid)
        if not t:
            return
        dlg = PathsDialog(self, title=f"重设路径：{t.name}（显示名由末级派生）",
                          paths=[t.entry_dir], single=True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        d = dlg.values()
        if not d:
            return
        self._after_result(self.engine.update_toolchain(tid, d))

    def _on_delete_library(self, tid: str):
        t = self.engine.library.by_id(tid)
        if not t:
            return
        if not confirm(self, f"从报备库删除“{t.name}”？\n路径：{t.entry_dir}",
                       "删除报备", yes="删除", no="取消"):
            return
        self._after_result(self.engine.delete_toolchain(tid))

    def _on_delete_library_path(self, uid: str):
        row = self._row_by_uid(uid)
        if not row or not row.toolchain_id:
            return
        t = self.engine.library.by_id(row.toolchain_id)
        if not t:
            return
        if not confirm(self,
                       f"该报备“{t.name}”目前在 PATH 中。\n"
                       "删除报备将同时从 PATH 移出该目录（生成待应用变更）。\n"
                       "若在应用前退出，该路径仍在 PATH 中，但已失去报备身份（变回锁定锚点）。",
                       "删除报备并移出 PATH", yes="移出并删除", no="取消"):
            return
        self._after_result(self.engine.delete_toolchain(t.id))

    # ================================================================ 待应用预览
    def _show_pending_dialog(self):
        if self._applying or self.engine.pending_count() == 0:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("待应用变更")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("以下变更尚未写入注册表。应用前可用 Ctrl+Z 撤销；落盘后不可回滚。"))
        lst = QListWidget()
        for g in self.engine.pending:
            lst.addItem(f"{g.description}  · {g.count} 项")
        v.addWidget(lst, 1)
        btns = QDialogButtonBox()
        undo_btn = btns.addButton("撤销所选组", QDialogButtonBox.ButtonRole.ActionRole)
        btns.addButton(QDialogButtonBox.StandardButton.Close)
        undo_btn.clicked.connect(lambda: self._undo_pending_selected(lst, dlg))
        v.addWidget(btns)
        dlg.resize(480, 320)
        dlg.exec()

    def _undo_pending_selected(self, lst: QListWidget, dlg: QDialog):
        row = lst.currentRow()
        if row < 0 or row >= len(self.engine.pending):
            return
        g = self.engine.pending[row]
        res = self.engine.undo_group(g.group_id)
        dlg.close()
        self._after_result(res)

    # ================================================================ 应用 / 确认
    def _do_apply(self, close_after: bool):
        if self.engine.pending_count() == 0:
            if close_after:
                self.close()
            else:
                info(self, "没有待应用的变更")
            return
        to_write, too_long = self.engine.apply_plan()
        if not to_write:
            info(self, "没有需要落盘的变更")
            return
        confirm_long = False
        if too_long:
            approx = len(self.engine._target_value())
            if confirm(self,
                       f"{USER_LABEL} PATH 拼接后约 {approx} 字符，"
                       f"可能超过上限（约 {PATH_LENGTH_LIMIT}）导致部分命令不可用。\n"
                       "是否仍然写入？选择「否」将放弃该项（保留待应用可撤销）。",
                       "PATH 超长警告", yes="仍然写入", no="放弃该项"):
                confirm_long = True
        problems = self.engine.anchor_invariant_check()
        if problems:
            error(self, "R2 锚点断言失败：PATH 可能已被外部程序改动。\n"
                  "请先点「重读注册表」再操作。\n\n" + "\n".join(problems))
            return
        self._applying = True
        self._set_mutation_enabled(False)
        self.status_label.setText("正在写入注册表，请稍候…")
        self._pending_apply_ok = close_after
        self._worker = ApplyWorker(self.engine, confirm_long, self)
        self._worker.done.connect(self._on_apply_done)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_apply_done(self, outcome):
        self._applying = False
        self._set_mutation_enabled(True)         # 先放开，_update_status 再按状态细化
        msgs = list(outcome.messages)
        if outcome.failed:
            error(self, "\n".join(msgs) or "写入失败", "落盘失败")
        elif outcome.skipped:
            warn(self, "\n".join(msgs) or "部分被放弃", "已放弃超长项")
        elif outcome.ok and msgs:
            info(self, "\n".join(msgs), "已应用")
        self._refresh()
        if self._pending_apply_ok and not outcome.failed:
            self.close()
        else:
            self._pending_apply_ok = False
            self._update_status()

    # ================================================================ 结果统一处理
    def _after_result(self, res, quiet: bool = False):
        if res is None:
            return
        if res.ok and res.message and not quiet:
            info(self, res.message)
        elif not res.ok:
            error(self, res.message or "操作失败")
        if res.warnings:
            warn(self, "\n".join(res.warnings), "提示")
        self._refresh()
        self._update_status()

    def _run_op(self, res):
        if res is None:
            return
        if not res.ok and res.message:
            error(self, res.message)
        elif res.warnings:
            warn(self, "\n".join(res.warnings), "提示")
        self._refresh()

    def _select_all_path(self):
        focus = self.focusWidget()
        target = self.path_list if focus is not self.lib_list else self.lib_list
        target.selectAll()

    # ================================================================ 过滤 & 关闭
    def _apply_filter(self, text: str):
        text = (text or "").strip().lower()
        for i in range(self.path_list.count()):
            it = self.path_list.item(i)
            if not text:
                it.setHidden(False)
                continue
            it.setHidden(text not in (it.data(ROLE_SEARCH) or ""))
        self._update_status()

    def closeEvent(self, e):
        if self._applying:                       # 写盘期间禁止关闭（避免 worker 线程随窗口销毁）
            e.ignore()
            return
        n = self.engine.pending_entries()
        if n:
            if not confirm(self,
                           f"还有 {n} 项未应用的变更，放弃并退出？\n"
                           "（左栏报备库排序已自动保存，不受影响）",
                           "确认退出", yes="放弃并退出", no="取消"):
                e.ignore()
                return
            self.engine.discard_pending()
        e.accept()


# ============================================================================
# 程序入口
# ============================================================================
def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName(APP_DISPLAY_NAME)
    app.setStyleSheet(APP_QSS)

    engine = Engine()
    res = engine.start()
    if not res.ok:
        QMessageBox.critical(None, "初始化失败", res.message)
        return 1
    win = MainWindow(engine)
    win.show()
    for w in (res.warnings or []):
        QMessageBox.warning(win, "提示", w)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
