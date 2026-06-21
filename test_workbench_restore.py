"""
工作台恢复模块 - 完整回归测试
覆盖场景：跨重启、重新登录、权限隔离、冲突处理、异常回退、导出一致性
"""
import os
import sys
import csv
import json
import tempfile
import shutil

TEST_DB_DIR = tempfile.mkdtemp(prefix="workbench_restore_test_")
TEST_DB_PATH = os.path.join(TEST_DB_DIR, "test_workbench.db")

os.environ["WORKBENCH_RESTORE_TEST_DB"] = TEST_DB_PATH

import database as db_mod
db_mod.DB_PATH = TEST_DB_PATH

from database import init_db, seed_sample_data, get_connection
from services import (
    save_filter_scheme, get_filter_schemes, delete_filter_scheme,
    get_filter_scheme_by_id, get_borrow_records, get_all_users,
    submit_borrow, get_all_parts, BusinessException, _is_filter_empty,
    get_operation_logs, save_user_preference, get_user_preference
)
from exporter import export_borrow_records

from scheme_coordinator import (
    restore_workbench_state, save_last_filters, get_last_filters,
    log_query_operation, log_export_operation, verify_export_consistency,
    activate_scheme, deactivate_scheme, delete_scheme_and_cleanup,
    get_available_schemes, set_active_scheme_id, get_active_scheme_id,
    save_last_list_state, get_last_list_state, clear_all_user_state,
    RestoreResult, WorkbenchState, DeletedSchemeInfo,
    StatePersistence, PermissionGuard, RestoreCoordinator,
    FallbackHandler, SchemeOperations, ExportVerifier,
    save_workbench_full_state, load_workbench_full_state,
    rename_scheme, get_recycle_bin, restore_scheme_from_recycle,
    handle_user_switch, handle_corrupt_state, get_last_login_user_id
)

passed = 0
failed = 0


def assert_eq(desc, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
        print(f"  PASS: {desc}")
    else:
        failed += 1
        print(f"  FAIL: {desc} => expected {expected!r}, got {actual!r}")


def assert_true(desc, condition):
    assert_eq(desc, condition, True)


def assert_raises(desc, exc_type, func, *args, **kwargs):
    global passed, failed
    try:
        func(*args, **kwargs)
        failed += 1
        print(f"  FAIL: {desc} => no exception raised")
    except exc_type:
        passed += 1
        print(f"  PASS: {desc}")
    except Exception as e:
        failed += 1
        print(f"  FAIL: {desc} => wrong exception: {type(e).__name__}: {e}")


# ============================================================
# 测试组1: 完整工作台状态持久化与恢复
# ============================================================

def test_full_workbench_state_persistence():
    """测试1: 完整工作台状态的保存和加载（跨重启模拟）"""
    print("\n=== 回归测试1: 完整工作台状态持久化 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    state = WorkbenchState()
    state.filters = {"status": "approved", "keyword": "CPU"}
    state.sort_by = "created_at"
    state.sort_order = "asc"
    state.page = 3
    state.page_size = 50
    state.active_scheme_id = None
    state.active_scheme_name = None

    save_workbench_full_state(operator["id"], state)

    loaded = load_workbench_full_state(operator["id"])
    assert_true("加载状态不为None", loaded is not None)
    assert_eq("filters.status 一致", loaded.filters.get("status"), "approved")
    assert_eq("filters.keyword 一致", loaded.filters.get("keyword"), "CPU")
    assert_eq("sort_by 一致", loaded.sort_by, "created_at")
    assert_eq("sort_order 一致", loaded.sort_order, "asc")
    assert_eq("page 一致", loaded.page, 3)
    assert_eq("page_size 一致", loaded.page_size, 50)

    state_dict = loaded.to_dict()
    assert_true("to_dict 包含 filters", "filters" in state_dict)
    assert_true("to_dict 包含 sort_by", "sort_by" in state_dict)
    assert_true("to_dict 包含 page", "page" in state_dict)


def test_workbench_state_with_active_scheme():
    """测试2: 包含激活方案的完整工作台状态"""
    print("\n=== 回归测试2: 含激活方案的工作台状态 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("完整状态方案", supervisor["id"],
                             {"status": "pending_approval", "keyword": "scheme_state"},
                             scope="shared", role="supervisor")

    state = WorkbenchState()
    state.filters = {"status": "pending_approval", "keyword": "scheme_state"}
    state.sort_by = "part_code"
    state.sort_order = "asc"
    state.page = 2
    state.page_size = 30
    state.active_scheme_id = sid
    state.active_scheme_name = "完整状态方案"

    save_workbench_full_state(supervisor["id"], state)

    loaded = load_workbench_full_state(supervisor["id"])
    assert_eq("激活方案ID一致", loaded.active_scheme_id, sid)
    assert_eq("激活方案名称一致", loaded.active_scheme_name, "完整状态方案")
    assert_eq("页码一致", loaded.page, 2)
    assert_eq("每页数量一致", loaded.page_size, 30)


def test_workbench_state_corrupt_handling():
    """测试3: 损坏的状态数据安全处理"""
    print("\n=== 回归测试3: 损坏状态数据的安全处理 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    save_user_preference(operator["id"], "workbench_full_state", "这不是合法的JSON{{{")

    loaded = load_workbench_full_state(operator["id"])
    assert_eq("损坏数据返回 None", loaded, None)

    result = handle_corrupt_state(operator["id"])
    assert_true("损坏处理返回成功", result.success)
    assert_eq("回退级别为 corrupt", result.fallback_level, "corrupt")
    assert_true("有损坏警告", len(result.warnings) > 0)
    assert_true("filters 已清空", _is_filter_empty(result.state.filters))

    loaded_after = load_workbench_full_state(operator["id"])
    assert_eq("清理后状态为空", loaded_after, None)


# ============================================================
# 测试组2: 四层恢复协调机制
# ============================================================

def test_four_level_restore_priority():
    """测试4: 四层恢复优先级（激活方案 > 完整状态 > 上次筛选 > 默认）"""
    print("\n=== 回归测试4: 四层恢复优先级验证 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    clear_all_user_state(supervisor["id"])

    save_last_filters(supervisor["id"], {"status": "returned", "keyword": "level3"})

    state = WorkbenchState()
    state.filters = {"status": "approved", "keyword": "level2"}
    state.page = 5
    save_workbench_full_state(supervisor["id"], state)

    sid = save_filter_scheme("优先级测试方案", supervisor["id"],
                             {"status": "pending_approval", "keyword": "level1"},
                             scope="personal", role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)

    result = restore_workbench_state(supervisor["id"], supervisor["role"])
    assert_true("恢复成功", result.success)
    assert_true("优先从激活方案恢复", result.scheme is not None)
    assert_eq("使用方案的filters", result.filters.get("keyword"), "level1")
    assert_eq("回退级别为 none", result.fallback_level, "none")


def test_restore_fallback_level_scheme_deleted():
    """测试5: 激活方案被删除时回退到完整状态"""
    print("\n=== 回归测试5: 方案删除后回退到完整状态 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    clear_all_user_state(supervisor["id"])

    state = WorkbenchState()
    state.filters = {"status": "approved", "keyword": "fallback_full"}
    state.page = 3
    state.sort_by = "part_code"
    state.sort_order = "asc"
    save_workbench_full_state(supervisor["id"], state)

    sid = save_filter_scheme("待删除方案5", supervisor["id"],
                             {"status": "pending_approval", "keyword": "to_delete5"},
                             scope="personal", role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)

    full_state_after = load_workbench_full_state(supervisor["id"])
    full_state_after.active_scheme_id = sid
    full_state_after.active_scheme_name = "待删除方案5"
    full_state_after.filters = {"status": "pending_approval", "keyword": "to_delete5"}
    full_state_after.page = 5
    full_state_after.page_size = 50
    save_workbench_full_state(supervisor["id"], full_state_after)

    delete_filter_scheme(sid, supervisor["id"], supervisor["role"])

    result = restore_workbench_state(supervisor["id"], supervisor["role"])
    assert_true("恢复成功（回退）", result.success)
    assert_eq("完整状态中的filters保留", result.filters.get("keyword"), "to_delete5")
    assert_eq("完整状态中的page保留", result.state.page, 5)
    assert_eq("完整状态中的page_size保留", result.state.page_size, 50)
    assert_eq("完整状态中的sort_by保留", result.state.sort_by, "part_code")
    assert_eq("回退级别为 full_state", result.fallback_level, "full_state")
    assert_true("有回退警告", len(result.warnings) > 0)
    assert_true("方案为None（已删除）", result.scheme is None)


def test_restore_fallback_to_last_filters():
    """测试6: 无完整状态时回退到上次筛选条件"""
    print("\n=== 回归测试6: 回退到上次筛选条件 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    clear_all_user_state(operator["id"])

    save_last_filters(operator["id"], {"status": "borrowed", "keyword": "fallback_last"})
    save_last_list_state(operator["id"], page=7, page_size=15)

    result = restore_workbench_state(operator["id"], operator["role"])
    assert_true("恢复成功", result.success)
    assert_eq("回退级别为 last_filters", result.fallback_level, "last_filters")
    assert_eq("filters 从 last_filters 恢复", result.filters.get("keyword"), "fallback_last")
    assert_eq("page 从 list_state 恢复", result.state.page, 7)
    assert_eq("page_size 从 list_state 恢复", result.state.page_size, 15)


def test_restore_fallback_to_default():
    """测试7: 无任何历史状态时使用默认视图"""
    print("\n=== 回归测试7: 无历史状态时使用默认视图 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    clear_all_user_state(operator["id"])

    result = restore_workbench_state(operator["id"], operator["role"])
    assert_true("恢复成功", result.success)
    assert_eq("回退级别为 default", result.fallback_level, "default")
    assert_true("filters 为空", _is_filter_empty(result.filters))
    assert_eq("默认 page 为 1", result.state.page, 1)
    assert_eq("默认 page_size 为 20", result.state.page_size, 20)
    assert_true("有默认提示", len(result.warnings) >= 1)


# ============================================================
# 测试组3: 权限隔离
# ============================================================

def test_permission_isolation_personal_schemes():
    """测试8: 个人方案的权限隔离"""
    print("\n=== 回归测试8: 个人方案权限隔离 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operators = [u for u in users if u["role"] == "operator"]
    if len(operators) < 2:
        print("  SKIP: 需要至少两个操作员")
        return

    op1, op2 = operators[0], operators[1]

    sid1 = save_filter_scheme("操作员1私有", op1["id"],
                              {"keyword": "op1_private"},
                              scope="personal", role="operator")

    op1_schemes = get_available_schemes(op1["id"], op1["role"])
    op1_names = [s["name"] for s in op1_schemes]
    assert_true("操作员1能看到自己的方案", "操作员1私有" in op1_names)

    op2_schemes = get_available_schemes(op2["id"], op2["role"])
    op2_names = [s["name"] for s in op2_schemes]
    assert_true("操作员2看不到操作员1的个人方案", "操作员1私有" not in op2_names)

    assert_raises("操作员2不能激活操作员1的个人方案", BusinessException,
                  activate_scheme, op2["id"], sid1, "operator")


def test_permission_shared_scheme_access():
    """测试9: 共享方案的访问权限"""
    print("\n=== 回归测试9: 共享方案访问权限 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("共享权限测试", supervisor["id"],
                             {"keyword": "shared_perm"},
                             scope="shared", role="supervisor")

    sv_schemes = get_available_schemes(supervisor["id"], supervisor["role"])
    sv_names = [s["name"] for s in sv_schemes]
    assert_true("主管能看到自己的共享方案", "共享权限测试" in sv_names)

    op_schemes = get_available_schemes(operator["id"], operator["role"])
    op_names = [s["name"] for s in op_schemes]
    assert_true("操作员能看到共享方案", "共享权限测试" in op_names)

    scheme = activate_scheme(operator["id"], sid, operator["role"])
    assert_true("操作员可以激活共享方案", scheme is not None)


def test_permission_edit_boundary():
    """测试10: 编辑权限边界"""
    print("\n=== 回归测试10: 编辑权限边界 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    personal_sid = save_filter_scheme("编辑权限测试", supervisor["id"],
                                      {"keyword": "edit_perm"},
                                      scope="personal", role="supervisor")

    scheme = get_filter_scheme_by_id(personal_sid)
    assert_true("主管可以编辑自己的个人方案",
                PermissionGuard.can_edit_scheme(scheme, supervisor["id"], "supervisor"))
    assert_true("操作员不能编辑主管的个人方案",
                not PermissionGuard.can_edit_scheme(scheme, operator["id"], "operator"))

    shared_sid = save_filter_scheme("共享编辑测试", supervisor["id"],
                                    {"keyword": "shared_edit"},
                                    scope="shared", role="supervisor")
    shared_scheme = get_filter_scheme_by_id(shared_sid)

    assert_true("主管可以编辑共享方案",
                PermissionGuard.can_edit_scheme(shared_scheme, supervisor["id"], "supervisor"))
    assert_true("操作员不能编辑共享方案",
                not PermissionGuard.can_edit_scheme(shared_scheme, operator["id"], "operator"))


def test_restore_no_permission_scheme():
    """测试11: 无权限方案的恢复回退"""
    print("\n=== 回归测试11: 无权限方案恢复回退 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    clear_all_user_state(operator["id"])

    sid = save_filter_scheme("越权测试方案", supervisor["id"],
                             {"keyword": "no_perm"},
                             scope="personal", role="supervisor")

    state = WorkbenchState()
    state.active_scheme_id = sid
    state.active_scheme_name = "越权测试方案"
    state.filters = {"keyword": "no_perm"}
    state.page = 2
    state.page_size = 30
    save_workbench_full_state(operator["id"], state)

    set_active_scheme_id(operator["id"], sid)

    save_last_filters(operator["id"], {"status": "approved"})
    save_last_list_state(operator["id"], page=5, page_size=15)

    result = restore_workbench_state(operator["id"], operator["role"])
    assert_true("恢复成功（回退）", result.success)
    assert_true("方案为 None（越权被清退）", result.scheme is None)
    assert_eq("回退级别为 full_state", result.fallback_level, "full_state")
    assert_eq("回退使用完整状态中的page", result.state.page, 2)
    assert_eq("回退使用完整状态中的page_size", result.state.page_size, 30)
    assert_true("激活方案已被清理", get_active_scheme_id(operator["id"]) is None)
    assert_true("有回退警告", len(result.warnings) > 0)


# ============================================================
# 测试组4: 重命名功能
# ============================================================

def test_rename_scheme_success():
    """测试12: 方案重命名成功"""
    print("\n=== 回归测试12: 方案重命名成功 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("原名方案", supervisor["id"],
                             {"keyword": "rename_test"},
                             scope="personal", role="supervisor")

    renamed = rename_scheme(sid, "新名方案", supervisor["id"], supervisor["role"])
    assert_eq("新名称正确", renamed["name"], "新名方案")
    assert_eq("ID 不变", renamed["id"], sid)

    reloaded = get_filter_scheme_by_id(sid)
    assert_eq("重新读取名称正确", reloaded["name"], "新名方案")


def test_rename_scheme_name_conflict():
    """测试13: 重命名时同名冲突"""
    print("\n=== 回归测试13: 重命名同名冲突 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    save_filter_scheme("冲突目标方案", supervisor["id"],
                       {"keyword": "a"}, scope="personal", role="supervisor")

    sid = save_filter_scheme("待重命名方案", supervisor["id"],
                             {"keyword": "b"}, scope="personal", role="supervisor")

    assert_raises("重命名为已存在的名称应报错", BusinessException,
                  rename_scheme, sid, "冲突目标方案",
                  supervisor["id"], supervisor["role"])


def test_rename_same_name_allowed():
    """测试14: 重命名为原名称（自我同名）允许"""
    print("\n=== 回归测试14: 自我同名重命名允许 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("原名不改", supervisor["id"],
                             {"keyword": "same_name"},
                             scope="personal", role="supervisor")

    renamed = rename_scheme(sid, "原名不改", supervisor["id"], supervisor["role"])
    assert_eq("同名称更新成功", renamed["name"], "原名不改")


def test_rename_permission_check():
    """测试15: 无权限不能重命名"""
    print("\n=== 回归测试15: 重命名权限检查 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("重命名权限测试", supervisor["id"],
                             {"keyword": "rename_perm"},
                             scope="personal", role="supervisor")

    assert_raises("操作员不能重命名主管个人方案", BusinessException,
                  rename_scheme, sid, "被篡改",
                  operator["id"], "operator")


def test_rename_updates_active_state():
    """测试16: 重命名激活方案时同步更新工作台状态"""
    print("\n=== 回归测试16: 重命名激活方案同步更新状态 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("激活重命名测试", supervisor["id"],
                             {"keyword": "active_rename"},
                             scope="personal", role="supervisor")

    activate_scheme(supervisor["id"], sid, supervisor["role"])

    state_before = load_workbench_full_state(supervisor["id"])
    assert_eq("重命名前状态中的方案名", state_before.active_scheme_name, "激活重命名测试")

    rename_scheme(sid, "激活后已改名", supervisor["id"], supervisor["role"])

    state_after = load_workbench_full_state(supervisor["id"])
    assert_eq("重命名后状态中的方案名已更新", state_after.active_scheme_name, "激活后已改名")


# ============================================================
# 测试组5: 回收站与删除回退
# ============================================================

def test_soft_delete_moves_to_recycle():
    """测试17: 软删除方案移入回收站"""
    print("\n=== 回归测试17: 软删除移入回收站 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("回收站测试方案", supervisor["id"],
                             {"status": "approved", "keyword": "recycle_test"},
                             scope="personal", role="supervisor")

    was_active = delete_scheme_and_cleanup(sid, supervisor["id"], supervisor["role"])

    recycle = get_recycle_bin(supervisor["id"])
    assert_true("回收站中有项目", len(recycle) > 0)

    recycle_names = [item.name for item in recycle]
    assert_true("被删方案在回收站中", "回收站测试方案" in recycle_names)

    deleted = get_filter_scheme_by_id(sid)
    assert_true("原方案已删除", deleted is None)


def test_restore_from_recycle_bin():
    """测试18: 从回收站恢复方案"""
    print("\n=== 回归测试18: 从回收站恢复方案 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("恢复测试方案", supervisor["id"],
                             {"status": "pending_approval", "keyword": "restore_me"},
                             scope="shared", role="supervisor")

    delete_scheme_and_cleanup(sid, supervisor["id"], supervisor["role"])

    restored = restore_scheme_from_recycle(supervisor["id"], "恢复测试方案", supervisor["role"])
    assert_true("恢复成功", restored is not None)
    assert_eq("恢复后名称正确", restored["name"], "恢复测试方案")
    assert_eq("恢复后 scope 正确", restored["scope"], "shared")
    assert_eq("恢复后 filters 正确", restored["filters"].get("keyword"), "restore_me")

    recycle_after = get_recycle_bin(supervisor["id"])
    recycle_names = [item.name for item in recycle_after]
    assert_true("恢复后从回收站移除", "恢复测试方案" not in recycle_names)


def test_restore_from_recycle_name_conflict():
    """测试19: 回收站恢复时同名冲突"""
    print("\n=== 回归测试19: 回收站恢复同名冲突 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("冲突恢复方案", supervisor["id"],
                             {"keyword": "conflict_restore"},
                             scope="personal", role="supervisor")

    delete_scheme_and_cleanup(sid, supervisor["id"], supervisor["role"])

    save_filter_scheme("冲突恢复方案", supervisor["id"],
                       {"keyword": "new_version"},
                       scope="personal", role="supervisor")

    assert_raises("恢复时同名冲突应报错", BusinessException,
                  restore_scheme_from_recycle,
                  supervisor["id"], "冲突恢复方案", supervisor["role"])


def test_recycle_bin_empty():
    """测试20: 空回收站处理"""
    print("\n=== 回归测试20: 空回收站处理 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    clear_all_user_state(operator["id"])

    recycle = get_recycle_bin(operator["id"])
    assert_eq("空回收站返回空列表", len(recycle), 0)

    assert_raises("恢复不存在的方案应报错", BusinessException,
                  restore_scheme_from_recycle,
                  operator["id"], "不存在的方案", "operator")


def test_recycle_preserves_filters():
    """测试21: 回收站保留完整筛选条件"""
    print("\n=== 回归测试21: 回收站保留完整筛选条件 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {
        "status": "approved",
        "keyword": "完整条件",
        "borrower_id": supervisor["id"],
        "date_from": "2025-01-01",
        "date_to": "2025-12-31"
    }

    sid = save_filter_scheme("完整条件方案", supervisor["id"],
                             filters, scope="personal", role="supervisor")

    delete_scheme_and_cleanup(sid, supervisor["id"], supervisor["role"])

    recycle = get_recycle_bin(supervisor["id"])
    target = [item for item in recycle if item.name == "完整条件方案"]
    assert_true("找到回收站中的方案", len(target) == 1)

    assert_eq("回收站保留 status", target[0].filters.get("status"), "approved")
    assert_eq("回收站保留 keyword", target[0].filters.get("keyword"), "完整条件")
    assert_eq("回收站保留 borrower_id", target[0].filters.get("borrower_id"), supervisor["id"])
    assert_eq("回收站保留 date_from", target[0].filters.get("date_from"), "2025-01-01")
    assert_eq("回收站保留 date_to", target[0].filters.get("date_to"), "2025-12-31")


# ============================================================
# 测试组6: 账号切换检测
# ============================================================

def test_user_switch_detection():
    """测试22: 账号切换检测"""
    print("\n=== 回归测试22: 账号切换检测 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    from scheme_coordinator import StatePersistence
    StatePersistence.save_last_user_id(supervisor["id"])

    last_user = get_last_login_user_id()
    assert_eq("上次登录用户正确", last_user, supervisor["id"])

    handle_user_switch(supervisor["id"], operator["id"])

    new_last_user = get_last_login_user_id()
    assert_eq("切换后上次登录用户更新", new_last_user, operator["id"])


def test_user_state_isolation():
    """测试23: 不同用户的状态完全隔离"""
    print("\n=== 回归测试23: 用户状态隔离 ===")
    users = get_all_users()
    operators = [u for u in users if u["role"] == "operator"]
    if len(operators) < 2:
        print("  SKIP: 需要至少两个操作员")
        return

    op1, op2 = operators[0], operators[1]

    save_last_filters(op1["id"], {"status": "approved", "keyword": "user1"})
    state1 = WorkbenchState()
    state1.filters = {"status": "approved", "keyword": "user1"}
    state1.page = 10
    save_workbench_full_state(op1["id"], state1)

    save_last_filters(op2["id"], {"status": "pending_approval", "keyword": "user2"})
    state2 = WorkbenchState()
    state2.filters = {"status": "pending_approval", "keyword": "user2"}
    state2.page = 20
    save_workbench_full_state(op2["id"], state2)

    result1 = restore_workbench_state(op1["id"], op1["role"])
    result2 = restore_workbench_state(op2["id"], op2["role"])

    assert_eq("用户1的 filters 正确", result1.filters.get("keyword"), "user1")
    assert_eq("用户2的 filters 正确", result2.filters.get("keyword"), "user2")
    assert_eq("用户1的 page 正确", result1.state.page, 10)
    assert_eq("用户2的 page 正确", result2.state.page, 20)


# ============================================================
# 测试组7: 导出一致性验证
# ============================================================

def test_export_consistency_matches_list():
    """测试24: CSV导出与列表查询完全一致"""
    print("\n=== 回归测试24: CSV导出与列表查询一致性 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    parts = get_all_parts()
    if not parts:
        print("  SKIP: 无备件数据")
        return
    part = parts[0]
    if part["available_stock"] < 3:
        print("  SKIP: 备件库存不足")
        return

    submit_borrow(part["id"], supervisor["id"], 1, "一致性测试1")
    submit_borrow(part["id"], supervisor["id"], 1, "一致性测试2")
    submit_borrow(part["id"], supervisor["id"], 1, "一致性测试3")

    filters = {"status": "approved", "borrower_id": supervisor["id"]}

    list_records = get_borrow_records(**filters)
    assert_true("列表查询有结果", len(list_records) >= 3)

    export_path = os.path.join(TEST_DB_DIR, "consistency_test24.csv")
    export_count = export_borrow_records(export_path, **filters)
    assert_eq("导出数量与列表数量一致", export_count, len(list_records))

    result = verify_export_consistency(export_path, filters)
    assert_true("一致性校验通过", result["consistent"])
    assert_eq("数据库数量一致", result["db_count"], len(list_records))
    assert_eq("CSV数量一致", result["csv_count"], len(list_records))

    os.remove(export_path)


def test_export_consistency_empty_result():
    """测试25: 空结果的导出一致性"""
    print("\n=== 回归测试25: 空结果导出一致性 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"keyword": "不存在的关键字_xyz_123"}

    list_records = get_borrow_records(**filters)
    assert_eq("列表查询为空", len(list_records), 0)

    export_path = os.path.join(TEST_DB_DIR, "empty_export.csv")
    export_count = export_borrow_records(export_path, **filters)
    assert_eq("导出数量为0", export_count, 0)

    result = verify_export_consistency(export_path, filters)
    assert_true("空结果一致性校验通过", result["consistent"])

    os.remove(export_path)


def test_export_consistency_failure_detection():
    """测试26: 导出不一致检测（模拟损坏的CSV）"""
    print("\n=== 回归测试26: 导出不一致检测 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved"}
    list_records = get_borrow_records(**filters)
    if len(list_records) == 0:
        print("  SKIP: 无数据")
        return

    export_path = os.path.join(TEST_DB_DIR, "damaged_export.csv")
    with open(export_path, "w", encoding="utf-8-sig") as f:
        f.write("记录编号,备件编码\n")
        f.write("FAKE001,SP-999\n")

    result = verify_export_consistency(export_path, filters)
    assert_true("不一致被检测到", not result["consistent"])
    assert_true("有不一致原因说明", len(result["reason"]) > 0)

    os.remove(export_path)


def test_export_operation_log_traceable():
    """测试27: 导出操作日志可追溯"""
    print("\n=== 回归测试27: 导出操作日志可追溯 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    before_logs = get_operation_logs(limit=200)

    filters = {"status": "approved"}
    records = get_borrow_records(**filters)

    log_export_operation(supervisor["id"], filters, len(records),
                         "traceable_test.csv", scheme_id=None)

    after_logs = get_operation_logs(limit=200)

    export_logs = [l for l in after_logs if l["action"] == "export_borrow_records"]
    assert_true("找到导出操作日志", len(export_logs) > 0)

    if export_logs:
        log = export_logs[0]
        assert_eq("操作人正确", log["operator_name"], supervisor["display_name"])
        assert_eq("操作成功", log["success"], 1)
        assert_true("详情包含文件名", "traceable_test.csv" in (log.get("detail") or ""))
        assert_true("详情包含记录数", str(len(records)) in (log.get("detail") or ""))


# ============================================================
# 测试组8: 同名冲突处理
# ============================================================

def test_name_conflict_personal_same_user():
    """测试28: 同一用户的个人方案同名冲突"""
    print("\n=== 回归测试28: 同用户个人方案同名冲突 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    save_filter_scheme("同名测试28", operator["id"],
                       {"keyword": "a"}, scope="personal", role="operator")

    assert_raises("同名个人方案重复创建应报错", BusinessException,
                  save_filter_scheme, "同名测试28", operator["id"],
                  {"keyword": "b"}, scope="personal", role="operator")


def test_name_conflict_personal_different_users():
    """测试29: 不同用户的个人方案可以同名"""
    print("\n=== 回归测试29: 不同用户个人方案可同名 ===")
    users = get_all_users()
    operators = [u for u in users if u["role"] == "operator"]
    if len(operators) < 2:
        print("  SKIP: 需要至少两个操作员")
        return

    op1, op2 = operators[0], operators[1]

    sid1 = save_filter_scheme("同名不同人", op1["id"],
                              {"keyword": "op1"}, scope="personal", role="operator")

    sid2 = save_filter_scheme("同名不同人", op2["id"],
                              {"keyword": "op2"}, scope="personal", role="operator")

    assert_true("两个都创建成功", sid1 > 0 and sid2 > 0)
    assert_true("两个方案ID不同", sid1 != sid2)

    s1 = get_filter_scheme_by_id(sid1)
    s2 = get_filter_scheme_by_id(sid2)
    assert_eq("方案1的 owner 正确", s1["owner_id"], op1["id"])
    assert_eq("方案2的 owner 正确", s2["owner_id"], op2["id"])


def test_name_conflict_shared_vs_personal():
    """测试30: 共享方案与个人方案同名冲突"""
    print("\n=== 回归测试30: 共享与个人方案同名冲突 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    save_filter_scheme("共享同名冲突", supervisor["id"],
                       {"keyword": "shared"}, scope="shared", role="supervisor")

    assert_raises("操作员创建与共享同名的个人方案应报错", BusinessException,
                  save_filter_scheme, "共享同名冲突", operator["id"],
                  {"keyword": "personal"}, scope="personal", role="operator")


# ============================================================
# 测试组9: 异常回退场景
# ============================================================

def test_fallback_scheme_deletion_was_active():
    """测试31: 激活方案被删除后的回退"""
    print("\n=== 回归测试31: 激活方案删除后回退 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    clear_all_user_state(supervisor["id"])

    save_last_filters(supervisor["id"], {"status": "approved", "keyword": "fallback_after_del"})

    sid = save_filter_scheme("待删激活方案", supervisor["id"],
                             {"keyword": "to_delete_active"},
                             scope="personal", role="supervisor")

    set_active_scheme_id(supervisor["id"], sid)

    result = FallbackHandler.handle_scheme_deletion(
        supervisor["id"], sid, supervisor["role"])

    assert_true("回退处理成功", result.success)
    assert_eq("回退到上次筛选条件", result.filters.get("keyword"), "fallback_after_del")
    assert_eq("回退级别为 last_filters", result.fallback_level, "last_filters")

    assert_true("激活方案已清空", get_active_scheme_id(supervisor["id"]) is None)


def test_fallback_scheme_deletion_no_history():
    """测试32: 方案删除且无历史筛选时回退到默认"""
    print("\n=== 回归测试32: 无历史时方案删除回退到默认 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    clear_all_user_state(operator["id"])

    sid = save_filter_scheme("待删无历史", operator["id"],
                             {"keyword": "no_history_del"},
                             scope="personal", role="operator")

    set_active_scheme_id(operator["id"], sid)

    result = FallbackHandler.handle_scheme_deletion(
        operator["id"], sid, operator["role"])

    assert_true("回退成功", result.success)
    assert_eq("回退级别为 default", result.fallback_level, "default")
    assert_true("filters 为空", _is_filter_empty(result.filters))
    assert_true("有回退警告", len(result.warnings) > 0)


def test_handle_corrupt_state_clean_reset():
    """测试33: 配置损坏后的干净重置"""
    print("\n=== 回归测试33: 配置损坏干净重置 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    save_user_preference(operator["id"], "workbench_full_state", "损坏的数据{{{")
    save_user_preference(operator["id"], "last_filters", "也是坏的")
    save_user_preference(operator["id"], "last_list_state", "同样坏的")

    result = handle_corrupt_state(operator["id"])

    assert_true("处理成功", result.success)
    assert_eq("回退级别为 corrupt", result.fallback_level, "corrupt")
    assert_true("filters 已重置为空", _is_filter_empty(result.filters))
    assert_eq("page 重置为默认", result.state.page, 1)
    assert_eq("page_size 重置为默认", result.state.page_size, 20)

    last_filters = get_last_filters(operator["id"])
    assert_true("last_filters 已清空", _is_filter_empty(last_filters))

    list_state = get_last_list_state(operator["id"])
    assert_true("list_state 已清空", len(list_state) == 0)


def test_empty_filters_with_active_scheme():
    """测试34: 空筛选条件但有激活方案时的处理"""
    print("\n=== 回归测试34: 空筛选但有激活方案 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("空筛选测试方案", supervisor["id"],
                             {"status": "returned", "keyword": "empty_filters_test"},
                             scope="personal", role="supervisor")

    activate_scheme(supervisor["id"], sid, supervisor["role"])

    result = FallbackHandler.handle_empty_filters(supervisor["id"])

    assert_true("处理成功", result.success)
    assert_true("从激活方案恢复了条件", result.scheme is not None)
    assert_eq("使用方案的 filters", result.filters.get("keyword"), "empty_filters_test")
    assert_eq("回退级别为 none", result.fallback_level, "none")


# ============================================================
# 测试组10: 操作日志可追溯性
# ============================================================

def test_restore_operation_logged():
    """测试35: 恢复操作日志完整"""
    print("\n=== 回归测试35: 恢复操作日志记录 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("日志恢复测试", supervisor["id"],
                            {"keyword": "log_restore"},
                            scope="personal", role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)

    before_count = len([l for l in get_operation_logs(limit=500)
                        if l["action"] == "restore_filter_state"])

    restore_workbench_state(supervisor["id"], supervisor["role"])

    after_logs = get_operation_logs(limit=500)
    restore_logs = [l for l in after_logs if l["action"] == "restore_filter_state"]

    assert_true("有恢复操作日志", len(restore_logs) >= before_count + 1)

    if restore_logs:
        latest = restore_logs[0]
        assert_eq("操作人正确", latest["operator_name"], supervisor["display_name"])
        assert_eq("操作成功", latest["success"], 1)
        assert_true("详情包含方案名", "日志恢复测试" in (latest.get("detail") or ""))


def test_query_operation_logged():
    """测试36: 查询操作日志记录"""
    print("\n=== 回归测试36: 查询操作日志记录 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": "log_query_test"}
    records = get_borrow_records(**filters)

    before_count = len([l for l in get_operation_logs(limit=500)
                        if l["action"] == "query_borrow_records"])

    log_query_operation(supervisor["id"], filters, len(records))

    after_logs = get_operation_logs(limit=500)
    query_logs = [l for l in after_logs if l["action"] == "query_borrow_records"]

    assert_true("有查询操作日志", len(query_logs) >= before_count + 1)


def test_rename_operation_logged():
    """测试37: 重命名操作日志记录"""
    print("\n=== 回归测试37: 重命名操作日志 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("原日志名", supervisor["id"],
                             {"keyword": "log_rename"},
                             scope="personal", role="supervisor")

    rename_scheme(sid, "新日志名", supervisor["id"], supervisor["role"])

    logs = get_operation_logs(limit=500)
    rename_logs = [l for l in logs if l["action"] == "rename_scheme"]

    assert_true("有重命名操作日志", len(rename_logs) > 0)

    if rename_logs:
        log = rename_logs[0]
        assert_true("详情包含新旧名称", "原日志名" in (log.get("detail") or ""))
        assert_true("详情包含新旧名称", "新日志名" in (log.get("detail") or ""))


# ============================================================
# 测试组11: 完整链路集成测试
# ============================================================

def test_full_workbench_flow():
    """测试38: 完整工作台链路（保存→激活→查询→导出→校验→删除→回退→恢复）"""
    print("\n=== 回归测试38: 完整工作台链路集成测试 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    parts = get_all_parts()
    if not parts:
        print("  SKIP: 无备件数据")
        return
    part = parts[0]
    if part["available_stock"] < 2:
        print("  SKIP: 备件库存不足")
        return

    submit_borrow(part["id"], supervisor["id"], 1, "完整链路测试A")
    submit_borrow(part["id"], supervisor["id"], 1, "完整链路测试B")

    filters = {"status": "approved", "borrower_id": supervisor["id"]}

    sid = save_filter_scheme("完整链路方案", supervisor["id"],
                             filters, scope="shared", role="supervisor")

    scheme = activate_scheme(supervisor["id"], sid, supervisor["role"])
    assert_eq("激活成功", scheme["id"], sid)

    records = get_borrow_records(**filters)
    assert_true("查询有结果", len(records) >= 2)

    log_query_operation(supervisor["id"], filters, len(records))

    export_path = os.path.join(TEST_DB_DIR, "full_flow_test.csv")
    count = export_borrow_records(export_path, **filters)
    log_export_operation(supervisor["id"], filters, count,
                         os.path.basename(export_path), scheme_id=sid)

    verify_result = verify_export_consistency(export_path, filters)
    assert_true("导出一致性校验通过", verify_result["consistent"])

    was_active = delete_scheme_and_cleanup(sid, supervisor["id"], supervisor["role"])
    assert_true("方案是激活状态", was_active)

    recycle = get_recycle_bin(supervisor["id"])
    recycle_names = [item.name for item in recycle]
    assert_true("方案在回收站中", "完整链路方案" in recycle_names)

    restored = restore_scheme_from_recycle(supervisor["id"], "完整链路方案", supervisor["role"])
    assert_true("从回收站恢复成功", restored is not None)
    assert_eq("恢复后名称正确", restored["name"], "完整链路方案")

    renamed = rename_scheme(restored["id"], "完整链路已改名",
                            supervisor["id"], supervisor["role"])
    assert_eq("重命名成功", renamed["name"], "完整链路已改名")

    result_after = restore_workbench_state(supervisor["id"], supervisor["role"])
    assert_true("重新恢复工作台成功", result_after.success)

    logs = get_operation_logs(limit=500)
    actions = {l["action"] for l in logs}
    assert_true("有保存方案日志", "save_filter_scheme" in actions)
    assert_true("有查询日志", "query_borrow_records" in actions)
    assert_true("有导出日志", "export_borrow_records" in actions)
    assert_true("有恢复日志", "restore_from_scheme" in actions or "restore_filter_state" in actions)
    assert_true("有重命名日志", "rename_scheme" in actions)
    assert_true("有回收站操作日志", "move_scheme_to_recycle" in actions)

    os.remove(export_path)


# ============================================================
# 测试组12: 数据模型验证
# ============================================================

def test_workbench_state_data_model():
    """测试39: WorkbenchState 数据模型完整性"""
    print("\n=== 回归测试39: WorkbenchState 数据模型 ===")
    state = WorkbenchState()

    assert_eq("默认 filters 为空 dict", state.filters, {})
    assert_eq("默认 sort_by", state.sort_by, "created_at")
    assert_eq("默认 sort_order", state.sort_order, "desc")
    assert_eq("默认 page", state.page, 1)
    assert_eq("默认 page_size", state.page_size, 20)
    assert_eq("默认 active_scheme_id", state.active_scheme_id, None)
    assert_eq("默认 active_scheme_name", state.active_scheme_name, None)

    state_dict = state.to_dict()
    assert_true("to_dict 返回 dict", isinstance(state_dict, dict))

    restored = WorkbenchState.from_dict(state_dict)
    assert_eq("from_dict 后 page 一致", restored.page, state.page)
    assert_eq("from_dict 后 filters 一致", restored.filters, state.filters)


def test_restore_result_data_model():
    """测试40: RestoreResult 数据模型完整性"""
    print("\n=== 回归测试40: RestoreResult 数据模型 ===")
    r1 = RestoreResult()
    assert_eq("默认 success 为 False", r1.success, False)
    assert_eq("默认 scheme 为 None", r1.scheme, None)
    assert_eq("默认 fallback_level", r1.fallback_level, "none")
    assert_eq("默认 warnings 为空列表", r1.warnings, [])

    r2 = RestoreResult(success=True, scheme={"id": 1, "name": "test"},
                       fallback_reason="test reason",
                       warnings=["w1", "w2"],
                       fallback_level="scheme")
    assert_eq("success 设置正确", r2.success, True)
    assert_eq("scheme 设置正确", r2.scheme["name"], "test")
    assert_eq("fallback_reason 设置正确", r2.fallback_reason, "test reason")
    assert_eq("fallback_level 设置正确", r2.fallback_level, "scheme")
    assert_eq("warnings 数量正确", len(r2.warnings), 2)

    r2.filters = {"status": "approved"}
    assert_eq("filters setter 生效", r2.filters.get("status"), "approved")


# ============================================================
# 主测试入口
# ============================================================

if __name__ == "__main__":
    try:
        init_db()
        seed_sample_data()

        print("=" * 70)
        print("工作台恢复模块 - 完整回归测试")
        print("=" * 70)

        test_full_workbench_state_persistence()
        test_workbench_state_with_active_scheme()
        test_workbench_state_corrupt_handling()

        test_four_level_restore_priority()
        test_restore_fallback_level_scheme_deleted()
        test_restore_fallback_to_last_filters()
        test_restore_fallback_to_default()

        test_permission_isolation_personal_schemes()
        test_permission_shared_scheme_access()
        test_permission_edit_boundary()
        test_restore_no_permission_scheme()

        test_rename_scheme_success()
        test_rename_scheme_name_conflict()
        test_rename_same_name_allowed()
        test_rename_permission_check()
        test_rename_updates_active_state()

        test_soft_delete_moves_to_recycle()
        test_restore_from_recycle_bin()
        test_restore_from_recycle_name_conflict()
        test_recycle_bin_empty()
        test_recycle_preserves_filters()

        test_user_switch_detection()
        test_user_state_isolation()

        test_export_consistency_matches_list()
        test_export_consistency_empty_result()
        test_export_consistency_failure_detection()
        test_export_operation_log_traceable()

        test_name_conflict_personal_same_user()
        test_name_conflict_personal_different_users()
        test_name_conflict_shared_vs_personal()

        test_fallback_scheme_deletion_was_active()
        test_fallback_scheme_deletion_no_history()
        test_handle_corrupt_state_clean_reset()
        test_empty_filters_with_active_scheme()

        test_restore_operation_logged()
        test_query_operation_logged()
        test_rename_operation_logged()

        test_full_workbench_flow()

        test_workbench_state_data_model()
        test_restore_result_data_model()

        print(f"\n{'=' * 70}")
        print(f"工作台恢复回归测试完成: {passed} 通过, {failed} 失败")
        print(f"{'=' * 70}")
    finally:
        try:
            shutil.rmtree(TEST_DB_DIR)
        except Exception:
            pass

    if failed > 0:
        sys.exit(1)
