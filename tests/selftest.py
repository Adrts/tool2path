#!/usr/bin/env python3
"""自测：覆盖设计文档 R0-R8 规则 + GUI 离屏冒烟（零第三方依赖，不需要 pytest）。

运行：
    .\\.venv\\Scripts\\python.exe tests\\selftest.py
退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tool2path as tpm                                                # noqa: E402
from tool2path import (                                                # noqa: E402
    ANCHOR, DEFAULT_CONFIG_NAME, Engine, MANAGED, RegistryApi,
    ConfigUnreadableError,
)


class FakeRegistry(RegistryApi):
    """内存假注册表：不触碰真实 HKCU。"""

    def __init__(self, user: str = "", user_type: int = tpm.REG_EXPAND_SZ):
        self.user = user
        self.user_type = user_type
        self.allow_write = True
        self.calls: list[tuple[str, str]] = []

    def read_user(self):
        return self.user, self.user_type

    def write_user(self, value, reg_type):
        self.calls.append(("user", value))
        if not self.allow_write:
            raise PermissionError("denied")
        self.user = value
        self.user_type = reg_type


def make_engine(reg, tmp: str, preset: str | None = None) -> Engine:
    cfg = os.path.join(tmp, DEFAULT_CONFIG_NAME)
    if preset is not None:
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(preset)
    return Engine(registry=reg, config_path=cfg)


def values(engine: Engine) -> list[str]:
    return [r.value_raw for r in engine.rows()]


def add(engine: Engine, d: str):
    return engine.register_toolchain(d)


def tool(engine: Engine, d: str):
    t = engine.library.by_norm(tpm.norm_path(d))
    assert t is not None, d
    return t


def uid_of(engine: Engine, value: str) -> str:
    for r in engine.rows():
        if r.value_raw == value:
            return r.uid
    raise AssertionError(f"{value} 不在右栏")


def lib_dirs(engine: Engine) -> list[str]:
    return [t.entry_dir for t in engine.library.sorted()]


# ============================================================================
# R0 / 配置文件
# ============================================================================
def test_registry_api_is_user_only():
    assert not hasattr(RegistryApi, "read_system")
    assert not hasattr(RegistryApi, "write_system")
    assert not hasattr(RegistryApi, "can_write_system")


def test_config_file_auto_created_on_missing():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        res = e.start()
        assert res.ok
        assert os.path.exists(e.library.path)
        assert "# Toolchain Path Manager" in open(e.library.path, encoding="utf-8").read()
        assert e.left_items() == []


def test_config_is_sole_source_and_display_derived():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp, preset=("# my header\n"
                                          "- C:\\Program Files\\nodejs\n"
                                          "\n"
                                          "- C:\\Python\\Scripts\n"))
        e.start()
        assert lib_dirs(e) == [r"C:\Program Files\nodejs", r"C:\Python\Scripts"]
        assert [t.name for t in e.library.sorted()] == ["nodejs", "Scripts"]
        e.library.reorder([t.id for t in reversed(e.library.sorted())])
        e.library.save()
        text = open(e.library.path, encoding="utf-8").read()
        assert text.index("Python") < text.index("nodejs")
        assert text.startswith("# my header")


def test_save_produces_no_temp_or_backup_files():
    """落盘不得产生 .bak / .tmp / 其他残留文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\A")
        add(e, r"C:\B")
        e.library.save()
        names = sorted(os.listdir(tmp))
        assert names == [DEFAULT_CONFIG_NAME], names


def test_config_duplicate_lines_collapsed_with_issue():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp, preset="- C:\\Go\\bin\n- c:\\go\\bin\\\n- C:\\Py\n")
        e.start()
        assert len(e.library.sorted()) == 2
        assert any("重复" in w for w in e.warnings)


def test_external_change_detected_and_reload():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp, preset="- C:\\Py\n")
        e.start()
        assert not e.library.external_changed()
        with open(e.library.path, "a", encoding="utf-8") as f:
            f.write("- C:\\Node\n")
        assert e.library.external_changed()
        e.library.mark_clean()
        assert not e.library.external_changed()


# ============================================================================
# 读取 / 分类
# ============================================================================
def test_start_classifies_anchor_and_managed():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Windows;C:\Go\bin")
        e = make_engine(reg, tmp, preset="- C:\\Go\\bin\n")
        e.start()
        assert [r.kind for r in e.rows()] == [ANCHOR, MANAGED]
        assert e.left_items() == []


def test_left_items_only_unjoined():
    with tempfile.TemporaryDirectory() as tmp:
        e = make_engine(FakeRegistry(), tmp)
        e.start()
        assert add(e, r"C:\Py").ok and add(e, r"C:\Node").ok
        assert {t.entry_dir for t in e.left_items()} == {r"C:\Py", r"C:\Node"}
        e.move_in_selected([tool(e, r"C:\Py").id])
        assert {t.entry_dir for t in e.left_items()} == {r"C:\Node"}


# ============================================================================
# R6 唯一性 / R1 接管
# ============================================================================
def test_duplicate_register_blocked_case_insensitive():
    with tempfile.TemporaryDirectory() as tmp:
        e = make_engine(FakeRegistry(), tmp)
        e.start()
        assert add(e, r"C:\Nodejs").ok
        r = add(e, "c:\\nodejs\\")
        assert not r.ok and "已报备" in r.message
        assert len(e.library.sorted()) == 1


def test_takeover_anchor_becomes_managed():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Windows\System32;C:\Program Files\nodejs")
        e = make_engine(reg, tmp)
        e.start()
        assert [r.kind for r in e.rows()] == [ANCHOR, ANCHOR]
        r = add(e, r"C:\Program Files\nodejs")
        assert r.ok and "接管" in r.message
        assert [x.kind for x in e.rows()] == [ANCHOR, MANAGED]
        assert not e.apply_plan()[0]                 # 值 / 位置不变 → 无需写回
        assert e.anchor_invariant_check() == []


# ============================================================================
# R4 / R7：移入 → 应用
# ============================================================================
def test_move_in_appends_user_bottom_and_apply():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\Go\bin")
        add(e, r"C:\Python\Scripts")
        ids = [tool(e, r"C:\Go\bin").id, tool(e, r"C:\Python\Scripts").id]
        assert e.move_in_selected(ids).ok
        assert values(e) == [r"C:\Go\bin", r"C:\Python\Scripts"]
        assert e.pending_count() == 1 and e.pending_entries() == 2
        assert e.anchor_invariant_check() == []
        out = e.apply()
        assert out.ok and not out.failed
        assert reg.user == r"C:\Go\bin;C:\Python\Scripts"
        assert reg.user_type == tpm.REG_EXPAND_SZ     # 类型保持不变
        assert e.pending_count() == 0
        assert lib_dirs(e) == [r"C:\Go\bin", r"C:\Python\Scripts"]   # 移入不改配置文件


def test_move_in_drop_to_top_slot():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Anchor")
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\Py")
        assert e.move_in_drop([tool(e, r"C:\Py").id], tpm.where_top()).ok
        assert values(e) == [r"C:\Py", r"C:\Anchor"]
        assert e.anchor_invariant_check() == []


def test_batch_move_in_skips_existing_keeps_group():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Go\bin")
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\Go\bin")
        add(e, r"C:\Py")
        add(e, r"C:\Rb")
        r = e.move_in_selected([tool(e, r"C:\Go\bin").id,
                                tool(e, r"C:\Py").id,
                                tool(e, r"C:\Rb").id])
        assert r.ok
        assert values(e) == [r"C:\Go\bin", r"C:\Py", r"C:\Rb"]
        assert any("Go" in w or "跳过" in w for w in (r.warnings or []))
        assert e.pending_entries() == 2


# ============================================================================
# 移出 / 回库尾
# ============================================================================
def test_move_out_appends_to_config_tail_and_undo():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        r = e.move_out_rows([uid_of(e, r"C:\B"), uid_of(e, r"C:\C")])
        assert r.ok
        assert values(e) == [r"C:\A"]
        assert lib_dirs(e) == [r"C:\A", r"C:\B", r"C:\C"]
        assert [t.entry_dir for t in e.left_items()] == [r"C:\B", r"C:\C"]
        e.undo_last()
        assert values(e) == [r"C:\A", r"C:\B", r"C:\C"]


def test_anchor_cannot_be_moved_out_or_dragged():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Anchor1;C:\Anchor2")
        e = make_engine(reg, tmp)
        e.start()
        assert not e.move_out_rows([uid_of(e, r"C:\Anchor1")]).ok
        assert not e.move_group_rows([uid_of(e, r"C:\Anchor1")], tpm.where_top()).ok
        assert not e.step_move(uid_of(e, r"C:\Anchor1"), up=True).ok
        assert not e.move_out_drop([uid_of(e, r"C:\Anchor1")], tpm.where_bottom()).ok


# ============================================================================
# R5 反向拖回（右 → 左 = 移出）
# ============================================================================
def test_move_out_drop_inserts_config_slot():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        r = e.move_out_drop([uid_of(e, r"C:\B"), uid_of(e, r"C:\C")],
                            tpm.where_before(tool(e, r"C:\A").id))
        assert r.ok
        assert values(e) == [r"C:\A"]
        assert lib_dirs(e) == [r"C:\B", r"C:\C", r"C:\A"]
        assert e.pending_count() == 1 and e.pending_entries() == 2
        e.undo_last()
        assert values(e) == [r"C:\A", r"C:\B", r"C:\C"]
        assert lib_dirs(e) == [r"C:\A", r"C:\B", r"C:\C"]


def test_move_out_drop_blank_area_appends_tail():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        assert e.move_out_drop([uid_of(e, r"C:\B")], tpm.where_bottom()).ok
        assert values(e) == [r"C:\A", r"C:\C"]
        assert lib_dirs(e) == [r"C:\A", r"C:\C", r"C:\B"]
        e.apply()
        assert reg.user == r"C:\A;C:\C"


def test_move_out_drop_group_order_is_right_display_order():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        assert e.move_out_drop([uid_of(e, r"C:\C"), uid_of(e, r"C:\B")],
                               tpm.where_top()).ok
        assert lib_dirs(e) == [r"C:\B", r"C:\C", r"C:\A"]
        assert values(e) == [r"C:\A"]


def test_move_group_keeps_relative_order():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        marker = uid_of(e, r"C:\A")
        r = e.move_group_rows([uid_of(e, r"C:\B"), uid_of(e, r"C:\C")],
                              tpm.where_before(marker))
        assert r.ok
        assert values(e) == [r"C:\B", r"C:\C", r"C:\A"]
        assert e.anchor_invariant_check() == []
        assert e.apply().ok
        assert reg.user == r"C:\B;C:\C;C:\A"


def test_move_group_drop_bottom():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        assert e.move_group_rows([uid_of(e, r"C:\A")], tpm.where_bottom()).ok
        assert values(e) == [r"C:\B", r"C:\C", r"C:\A"]
        assert e.anchor_invariant_check() == []


# ============================================================================
# ▲ ▼
# ============================================================================
def test_step_move_managed_only():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        uid_b = uid_of(e, r"C:\B")
        assert e.step_move(uid_b, up=False).ok
        assert values(e) == [r"C:\A", r"C:\C", r"C:\B"]
        assert e.step_move(uid_b, up=True).ok
        assert values(e) == [r"C:\A", r"C:\B", r"C:\C"]
        assert not e.step_move("not-exist", up=True).ok


def test_step_move_boundary_and_anchor_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A;C:\B;C:\C")
        e = make_engine(reg, tmp)
        e.start()
        for d in (r"C:\A", r"C:\B", r"C:\C"):
            add(e, d)
        assert e.step_move(uid_of(e, r"C:\A"), up=True).ok        # 顶部边界：ok 且无变化
        assert e.step_move(uid_of(e, r"C:\C"), up=False).ok       # 底部边界：ok 且无变化
        assert values(e) == [r"C:\A", r"C:\B", r"C:\C"]


def test_anchor_step_move_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\WinAnchor;C:\X")
        e = make_engine(reg, tmp)
        e.start()
        assert not e.step_move(uid_of(e, r"C:\WinAnchor"), up=True).ok
        assert not e.step_move(uid_of(e, r"C:\X"), up=False).ok


# ============================================================================
# 撤销 / 放弃
# ============================================================================
def test_undo_last_and_group():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\X")
        add(e, r"C:\Y")
        ix, iy = tool(e, r"C:\X").id, tool(e, r"C:\Y").id
        e.move_in_selected([ix])
        gid1 = e.pending[-1].group_id
        e.move_in_selected([iy])
        assert e.pending_count() == 2
        e.undo_last()
        assert values(e) == [r"C:\X"]
        assert e.pending_count() == 1
        e.undo_group(gid1)
        assert values(e) == []
        assert e.pending_count() == 0


def test_discard_pending_restores_prestate():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\X")
        add(e, r"C:\Y")
        e.move_in_selected([tool(e, r"C:\X").id, tool(e, r"C:\Y").id])
        e.discard_pending()
        assert values(e) == []
        assert e.pending_count() == 0
        assert len(e.library.sorted()) == 2


# ============================================================================
# R2 断言
# ============================================================================
def test_anchor_invariant_detects_manual_break():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\A1;C:\A2;C:\M")
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\M")
        assert e.anchor_invariant_check() == []
        rows = e.rows()
        i1 = next(i for i, r in enumerate(rows) if r.value_raw == r"C:\A1")
        i2 = next(i for i, r in enumerate(rows) if r.value_raw == r"C:\A2")
        rows[i1], rows[i2] = rows[i2], rows[i1]
        assert len(e.anchor_invariant_check()) == 1
        assert not e.apply().ok                        # 落盘前断言失败中止


# ============================================================================
# 删除报备
# ============================================================================
def test_delete_in_path_creates_move_out_pending():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Node")
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\Node")
        tid = tool(e, r"C:\Node").id
        assert e.toolchain_in_path(tid)
        assert e.delete_toolchain(tid).ok
        assert e.library.by_norm(tpm.norm_path(r"C:\Node")) is None
        assert e.pending_count() == 1 and e.pending_entries() == 1
        e.undo_last()
        assert e.library.by_norm(tpm.norm_path(r"C:\Node")) is not None
        assert values(e) == [r"C:\Node"]
        assert e.delete_toolchain(tid).ok
        e.apply()
        assert reg.user == ""


def test_delete_not_in_path_immediate():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\X")
        assert e.delete_toolchain(tool(e, r"C:\X").id).ok
        assert e.pending_count() == 0 and e.left_items() == []
        assert "C:\\X" not in open(e.library.path, encoding="utf-8").read()


# ============================================================================
# R8 失效目录：判定 / 重建 / 删除声明
# ============================================================================
def test_dir_missing_rule_for_toolchain():
    with tempfile.TemporaryDirectory() as tmp:
        d = os.path.join(tmp, "tool")
        t = tpm.Toolchain(id="a", name="tool", entry_dir=d)
        assert t.dir_missing
        os.makedirs(d)
        assert not t.dir_missing
        assert not tpm.Toolchain(id="b", name="npm",
                                 entry_dir=r"%USERPROFILE%\npm").dir_missing


def test_delete_decl_text_adapts_to_dir_state():
    """确认框第二句按目录现状自适应：目录在 → 承诺不动磁盘；目录不在 → 只说移除登记。"""
    with tempfile.TemporaryDirectory() as tmp:
        present = os.path.join(tmp, "present")
        os.makedirs(present)
        gone = os.path.join(tmp, "gone")
        yes = tpm.delete_decl_text("a", present)
        no = tpm.delete_decl_text("b", gone)
        assert "不会删除磁盘" in yes
        assert "不在磁盘上" in no and "不会删除磁盘" not in no
        var = tpm.delete_decl_text("c", r"%USERPROFILE%\npm")
        assert "不会删除磁盘" in var                 # 含 %VAR% 无法判定 → 保守承诺
        assert "失去报备身份" in tpm.delete_decl_text("d", gone, in_path_managed=True)


def test_row_missing_covers_anchor_and_managed():
    with tempfile.TemporaryDirectory() as tmp:
        present = os.path.join(tmp, "present")
        gone = os.path.join(tmp, "gone")
        var_row = r"%USERPROFILE%\npm"
        os.makedirs(present)
        reg = FakeRegistry(user=f"{present};{gone};{var_row}")
        e = make_engine(reg, tmp)
        e.start()
        by_val = {r.value_raw: r for r in e.rows()}
        assert not e.row_missing(by_val[present])
        assert e.row_missing(by_val[gone])              # 锚点也能判定（核心改动点）
        assert not e.row_missing(by_val[var_row])       # 含 %VAR% → 不参与判定
        assert add(e, gone).ok                          # 接管为报备项后仍判定失效
        assert by_val[gone].kind == MANAGED
        assert e.row_missing(by_val[gone])
        assert e.anchor_invariant_check() == []


def test_missing_count_covers_left_right_and_anchor():
    with tempfile.TemporaryDirectory() as tmp:
        present = os.path.join(tmp, "present")
        os.makedirs(present)
        left_gone = os.path.join(tmp, "left-gone")      # 左栏失效报备（不在 PATH）
        right_gone = os.path.join(tmp, "right-gone")    # 右栏失效报备（已在 PATH）
        anchor_gone = os.path.join(tmp, "anchor-gone")  # 右栏失效锚点
        reg = FakeRegistry(user=f"{right_gone};{present};{anchor_gone}")
        e = make_engine(reg, tmp, preset=f"# hdr\n- {left_gone}\n- {right_gone}\n")
        e.start()
        assert e.missing_count() == 3
        assert len(e.missing_rows()) == 2               # missing_rows 只覆盖右栏工作序列
        os.makedirs(left_gone)                          # 目录恢复存在 → 计数下降
        assert e.missing_count() == 2


def test_rebuild_dir_creates_multilevel_and_clears_missing():
    with tempfile.TemporaryDirectory() as tmp:
        deep = os.path.join(tmp, "a", "b", "c")
        reg = FakeRegistry(user=deep)
        e = make_engine(reg, tmp)
        e.start()
        assert e.missing_count() == 1
        res = e.rebuild_dir(deep)
        assert res.ok and os.path.isdir(deep)
        assert e.missing_count() == 0 and e.missing_rows() == []
        assert e.pending_count() == 0                   # 重建不进待应用


def test_rebuild_dir_failures_keep_files_intact():
    with tempfile.TemporaryDirectory() as tmp:
        e = make_engine(FakeRegistry(), tmp)
        e.start()
        assert not e.rebuild_dir("").ok
        assert not e.rebuild_dir("relative\\dir").ok    # 非绝对路径被拒
        f = os.path.join(tmp, "occupied")               # 同名文件占位：不覆盖
        with open(f, "w", encoding="utf-8") as fp:
            fp.write("keep")
        res = e.rebuild_dir(f)
        assert not res.ok and res.message
        assert os.path.isfile(f)
        assert open(f, encoding="utf-8").read() == "keep"
        res = e.rebuild_dir(os.path.join(f, "sub"))     # 父级是文件 → 创建必失败
        assert not res.ok and res.message


def test_delete_missing_library_immediate_write():
    with tempfile.TemporaryDirectory() as tmp:
        gone = os.path.join(tmp, "gone")
        keep = os.path.join(tmp, "keep")
        os.makedirs(keep)
        e = make_engine(FakeRegistry(), tmp, preset=f"# my header\n- {gone}\n- {keep}\n")
        e.start()
        assert e.missing_count() == 1
        assert e.delete_toolchain(tool(e, gone).id).ok
        assert e.pending_count() == 0                    # 不在 PATH → 即时写盘
        assert lib_dirs(e) == [keep]                     # 库序不变，仅少一行
        text = open(e.library.path, encoding="utf-8").read()
        assert text.startswith("# my header") and gone not in text


def test_delete_missing_managed_in_path_pending_and_undo():
    with tempfile.TemporaryDirectory() as tmp:
        gone = os.path.join(tmp, "gone")
        reg = FakeRegistry(user=gone)
        e = make_engine(reg, tmp, preset=f"- {gone}\n")
        e.start()
        tid = tool(e, gone).id
        assert e.toolchain_in_path(tid)
        assert e.delete_toolchain(tid).ok
        assert e.pending_count() == 1 and e.pending_entries() == 1
        assert values(e) == []
        assert e.undo_group(e.pending[-1].group_id).ok   # 整组撤销恢复报备与库序
        assert e.library.by_norm(tpm.norm_path(gone)) is not None
        assert values(e) == [gone]
        assert e.delete_toolchain(tid).ok
        assert e.apply().ok
        assert gone not in reg.user


def test_delete_missing_anchor_pending_only_config_untouched():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a")
        gone = os.path.join(tmp, "gone")
        os.makedirs(a)
        reg = FakeRegistry(user=f"{a};{gone}")
        e = make_engine(reg, tmp)
        e.start()
        uid = uid_of(e, gone)
        before = open(e.library.path, "rb").read()
        assert e.remove_missing_rows([uid]).ok
        assert values(e) == [a]
        assert e.pending_count() == 1 and e.pending_entries() == 1
        assert open(e.library.path, "rb").read() == before      # 配置文件逐字节不变
        assert e.anchor_invariant_check() == []
        assert e.apply().ok
        assert reg.user == a


def test_delete_missing_anchor_undo_restores_order():
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a")
        b = os.path.join(tmp, "b")
        gone = os.path.join(tmp, "gone")
        os.makedirs(a)
        os.makedirs(b)
        reg = FakeRegistry(user=f"{a};{gone};{b}")
        e = make_engine(reg, tmp)
        e.start()
        assert e.remove_missing_rows([uid_of(e, gone)]).ok
        assert values(e) == [a, b]
        assert e.undo_last().ok
        assert values(e) == [a, gone, b]                 # 位置与移除前一致
        assert e.anchor_invariant_check() == []


def test_remove_missing_rows_rejects_existing_and_managed():
    with tempfile.TemporaryDirectory() as tmp:
        present = os.path.join(tmp, "present")
        gone = os.path.join(tmp, "gone")
        os.makedirs(present)
        reg = FakeRegistry(user=f"{present};{gone}")
        e = make_engine(reg, tmp, preset=f"- {gone}\n")
        e.start()
        assert not e.remove_missing_rows([]).ok
        assert not e.remove_missing_rows([uid_of(e, present)]).ok   # 目录存在 → 拒绝
        assert not e.remove_missing_rows([uid_of(e, gone)]).ok      # 报备项 → 走报备渠道
        assert e.pending_count() == 0 and values(e) == [present, gone]


def test_config_unreadable_raises_on_start():
    """配置文件存在但读不出来：start() 抛 ConfigUnreadableError（由入口弹窗并退出）。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, DEFAULT_CONFIG_NAME)
        os.makedirs(cfg)                                 # 同名目录占位 → open() 抛 OSError
        e = Engine(registry=FakeRegistry(user="X"), config_path=cfg)
        try:
            e.start()
        except ConfigUnreadableError as err:
            assert err.path == cfg and err.reason
        else:
            raise AssertionError("start() 应当抛出 ConfigUnreadableError")


def test_unreadable_reload_raises_and_never_wipes_config():
    """运行中重载 / 刷新读不到文件：抛错，且报备库与磁盘文件都不被改动。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, DEFAULT_CONFIG_NAME)
        preset = "- C:\\tools\\a\n- C:\\tools\\b\n"
        e = make_engine(FakeRegistry(user="C:\\Windows"), tmp, preset=preset)
        e.start()
        assert lib_dirs(e) == ["C:\\tools\\a", "C:\\tools\\b"]
        os.remove(cfg)
        os.makedirs(cfg)                                 # 文件变成“读不到”
        for call in (e.reload_config, e.refresh):
            try:
                call()
            except ConfigUnreadableError:
                pass
            else:
                raise AssertionError(f"{call.__name__} 应当抛出 ConfigUnreadableError")
        assert lib_dirs(e) == ["C:\\tools\\a", "C:\\tools\\b"]   # 库未被清空
        os.rmdir(cfg)                                    # 占用解除
        e.library.save()
        text = open(cfg, encoding="utf-8").read()
        assert "C:\\tools\\a" in text and "C:\\tools\\b" in text  # 写回仍是完整清单


def test_undo_and_discard_never_wipe_config():
    """带待应用变更时撤销 / 放弃：磁盘文件必须逐字节不变（DEFECT-001 回归）。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = os.path.join(tmp, DEFAULT_CONFIG_NAME)
        reg = FakeRegistry(user="C:\\Windows")
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\tools\a")
        e.move_in_selected([tool(e, r"C:\tools\a").id])
        before = open(cfg, encoding="utf-8").read()
        assert e.pending_count() == 1
        assert e.undo_last().ok
        assert open(cfg, encoding="utf-8").read() == before
        e.move_in_selected([tool(e, r"C:\tools\a").id])
        e.discard_pending()
        assert open(cfg, encoding="utf-8").read() == before


# ============================================================================
# 写回失败 / 超长 / 重载
# ============================================================================
def test_write_failure_keeps_pending():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\T")
        assert e.move_in_selected([tool(e, r"C:\T").id]).ok
        reg.allow_write = False
        out = e.apply()
        assert out.failed and reg.user == ""
        assert e.pending_count() == 1
        reg.allow_write = True
        assert e.apply().ok
        assert reg.user == r"C:\T"
        assert e.pending_count() == 0


def test_too_long_needs_confirm():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry()
        e = make_engine(reg, tmp)
        e.start()
        old = tpm.PATH_LENGTH_LIMIT
        tpm.PATH_LENGTH_LIMIT = 10                     # 临时压低阈值
        try:
            long_dir = r"C:\a" + "\\x" * 8
            add(e, long_dir)
            e.move_in_selected([tool(e, long_dir).id])
            to_write, too_long = e.apply_plan()
            assert to_write and too_long
            out = e.apply()
            assert not out.written and out.skipped
            assert reg.user == "" and e.pending_count() == 1
            out2 = e.apply(confirm_long=True)
            assert out2.ok and out2.written
        finally:
            tpm.PATH_LENGTH_LIMIT = old


def test_reload_config_reclassifies_rows():
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Node;C:\Py")
        e = make_engine(reg, tmp)
        e.start()
        add(e, r"C:\Node")
        assert [r.kind for r in e.rows()] == [MANAGED, ANCHOR]
        with open(e.library.path, "a", encoding="utf-8") as f:
            f.write("- C:\\Py\n")
        r = e.reload_config()
        assert r.ok
        assert [x.kind for x in e.rows()] == [MANAGED, MANAGED]
        assert e.pending_count() == 0


def test_refresh_merges_reload_and_reread():
    """刷新 = 重读配置 + 重读注册表 + 重扫存在性；放弃全部待应用变更。"""
    with tempfile.TemporaryDirectory() as tmp:
        node = os.path.join(tmp, "node")
        py = os.path.join(tmp, "py")
        extra = os.path.join(tmp, "extra")
        gone = os.path.join(tmp, "gone")
        for d in (node, py, extra):
            os.makedirs(d)
        reg = FakeRegistry(user=node)
        e = make_engine(reg, tmp)
        e.start()
        add(e, node)                                   # node 已在 PATH → 报备即接管
        add(e, py)                                     # py 仅在报备库
        e.move_in_selected([tool(e, py).id])           # 生成 1 组待应用
        assert e.pending_count() == 1
        reg.user = f"{node};{gone}"                    # 外部改动注册表
        with open(e.library.path, "a", encoding="utf-8") as f:
            f.write(f"- {extra}\n")                    # 外部改动配置文件
        assert e.refresh().ok
        assert e.pending_count() == 0                  # 待应用被放弃
        assert values(e) == [node, gone]               # 右栏以注册表重建
        assert lib_dirs(e) == [node, py, extra]        # 配置文件已重载
        assert e.missing_count() == 1                  # 存在性已重扫（仅 gone 失效）
        assert e.anchor_invariant_check() == []

        cfg = os.path.join(tmp, "broken", DEFAULT_CONFIG_NAME)
        os.makedirs(cfg)                               # 配置文件读不到 → 直接抛错（入口弹窗并退出）
        e2 = Engine(registry=FakeRegistry(user="X"), config_path=cfg)
        try:
            e2.start()
        except ConfigUnreadableError:
            pass
        else:
            raise AssertionError("start() 应当抛出 ConfigUnreadableError")


# ============================================================================
# GUI 离屏冒烟
# ============================================================================
def test_gui_smoke_offscreen():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QPushButton

    from tool2path import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    with tempfile.TemporaryDirectory() as tmp:
        reg = FakeRegistry(user=r"C:\Windows\System32")
        e = make_engine(reg, tmp)
        e.start()
        assert os.path.exists(e.library.path)

        win = MainWindow(e)
        win.show()
        app.processEvents()

        assert win.lib_list.count() == 0
        assert win.path_list.count() == 1

        assert e.register_toolchain([r"C:\Windows\System32", r"C:\Python", r"C:\Node"]).ok
        win._refresh()
        app.processEvents()
        assert win.lib_list.count() == 2
        assert [t.name for t in e.library.sorted()] == ["System32", "Python", "Node"]

        if win.lib_list.count() > 0:                   # 左栏槽位映射可用
            pos = win.lib_list.mapFromGlobal(
                win.lib_list.viewport().mapToGlobal(
                    win.lib_list.visualItemRect(win.lib_list.item(0)).center()))
            assert win._library_slot(win.lib_list, pos) is not None

        py_id = e.library.by_norm(tpm.norm_path(r"C:\Python")).id
        node_id = e.library.by_norm(tpm.norm_path(r"C:\Node")).id
        assert e.move_in_selected([py_id, node_id]).ok
        win._refresh()
        app.processEvents()
        assert win.lib_list.count() == 0
        assert len(e.rows()) == 3

        if win.path_list.height() > 5:                 # 右栏槽位 + 提示线不崩溃
            pos = win.path_list.mapFromGlobal(
                win.path_list.viewport().mapToGlobal(
                    win.path_list.visualItemRect(win.path_list.item(0)).center()))
            where = win._path_slot(win.path_list, pos)
            assert where is not None
            y = win.path_list._drop_indicator_y(pos, "rows")
            win.path_list._set_drop_line(y or 0)
            win.path_list.grab()
            win.path_list._set_drop_line(None)

        assert e.undo_last().ok
        win._refresh()
        assert len(e.rows()) == 1

        win_id = e.library.by_norm(tpm.norm_path(r"C:\Windows\System32")).id
        assert e.delete_toolchain(win_id).ok
        win._refresh()
        assert e.pending_count() == 1
        e.undo_last()
        win._refresh()
        assert e.library.by_norm(tpm.norm_path(r"C:\Windows\System32")) is not None

        assert e.move_in_selected([py_id, node_id]).ok
        expected = ";".join(r.value_raw for r in e.rows())
        assert e.apply().ok
        win._refresh()
        assert reg.user == expected and e.pending_count() == 0

        def btns(txt):
            return [b for b in win.findChildren(QPushButton) if txt in b.text()]

        win._applying = True                           # 写盘锁：全部禁用
        win._set_mutation_enabled(False)
        for txt in ("报备新目录", "刷新", "移入", "移出",
                    "撤销", "待应用", "应用", "确认", "退出"):
            assert btns(txt) and all(not b.isEnabled() for b in btns(txt)), txt
        win._applying = False
        win._set_mutation_enabled(True)
        win._update_status()
        assert all(b.isEnabled() for b in btns("退出"))

        win.close()
        app.processEvents()


# ============================================================================
# GUI 离屏：失效行黄标与行内重建
# ============================================================================
def test_missing_ui_buttons_only_for_missing_rows():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from tool2path import LibraryRow, MainWindow, PathRow

    app = QApplication.instance() or QApplication(sys.argv)

    class Stub:
        def on_toggle(self, *a):
            pass

        def on_step(self, *a):
            pass

        def on_missing_menu(self, *a):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        present = os.path.join(tmp, "present")
        right_gone = os.path.join(tmp, "right-gone")
        left_gone = os.path.join(tmp, "left-gone")
        os.makedirs(present)
        reg = FakeRegistry(user=f"{right_gone};{present}")
        e = make_engine(reg, tmp, preset=f"# hdr\n- {left_gone}\n")
        e.start()

        # 左栏：只有失效报备行渲染黄标按钮
        assert LibraryRow(tool(e, left_gone), False, Stub())._missing_btn is not None
        assert LibraryRow(tpm.Toolchain(id="x", name="present", entry_dir=present),
                          False, Stub())._missing_btn is None
        # 右栏：失效锚点也渲染（此前锚点完全没有交互控件），正常行不渲染
        by_val = {r.value_raw: r for r in e.rows()}
        assert PathRow(by_val[right_gone], False, Stub(),
                       missing=e.row_missing(by_val[right_gone]))._missing_btn is not None
        assert PathRow(by_val[present], False, Stub(),
                       missing=e.row_missing(by_val[present]))._missing_btn is None

        win = MainWindow(e)
        win.show()
        app.processEvents()
        assert "2 个目录不存在" in win.status_label.text()   # 计数含失效锚点

        win._on_rebuild_dir("path", by_val[right_gone].uid)  # 成功静默刷新
        app.processEvents()
        assert os.path.isdir(right_gone)
        assert "1 个目录不存在" in win.status_label.text()
        win._on_rebuild_dir("library", tool(e, left_gone).id)
        app.processEvents()
        assert os.path.isdir(left_gone)
        assert "个目录不存在" not in win.status_label.text()

        win.close()
        app.processEvents()


# ============================================================================
# 运行器
# ============================================================================
def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed: list[tuple[str, str]] = []
    for fn in tests:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed.append((fn.__name__, traceback.format_exc()))
            print(f"  FAIL  {fn.__name__}")
    print(f"\n共 {len(tests)} 项，通过 {len(tests) - len(failed)}，失败 {len(failed)}")
    for name, tb in failed:
        print(f"\n--- {name} ---\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
