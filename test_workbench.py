import os
import sys
import csv
import tempfile
import shutil

TEST_DB_DIR = tempfile.mkdtemp(prefix="workbench_test_")
TEST_DB_PATH = os.path.join(TEST_DB_DIR, "test_workbench.db")

os.environ["WORKBENCH_TEST_DB"] = TEST_DB_PATH

import database as db_mod
db_mod.DB_PATH = TEST_DB_PATH

from database import init_db, seed_sample_data
from services import (
    save_filter_scheme, get_filter_schemes, delete_filter_scheme,
    get_filter_scheme_by_id, get_borrow_records, get_all_users,
    submit_borrow, get_all_parts, BusinessException, _is_filter_empty,
    get_operation_logs, get_user_by_id
)
from exporter import export_borrow_records

from scheme_coordinator import (
    restore_workbench_state, save_last_filters, get_last_filters,
    log_query_operation, log_export_operation, verify_export_consistency,
    activate_scheme, deactivate_scheme, delete_scheme_and_cleanup,
    get_available_schemes, set_active_scheme_id, get_active_scheme_id,
    save_last_list_state, get_last_list_state, clear_all_user_state,
    RestoreResult
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


def test_last_filters_persistence():
    print("\n=== 工作台测试1: 上次筛选条件持久化 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    assert_true("初始无上次筛选条件", _is_filter_empty(get_last_filters(operator["id"])))

    filters = {"status": "pending_approval", "keyword": "CPU"}
    save_last_filters(operator["id"], filters)
    saved = get_last_filters(operator["id"])
    assert_eq("status 持久化正确", saved.get("status"), "pending_approval")
    assert_eq("keyword 持久化正确", saved.get("keyword"), "CPU")

    from database import get_connection
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE user_id = ? AND pref_key = 'last_filters'",
            (operator["id"],)
        ).fetchone()
        assert_true("数据库中存在 last_filters 记录", row is not None)

    save_last_filters(operator["id"], {})
    cleared = get_last_filters(operator["id"])
    assert_true("清空后为空", _is_filter_empty(cleared))


def test_list_state_persistence():
    print("\n=== 工作台测试2: 列表状态（排序分页）持久化 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    assert_true("初始无列表状态", len(get_last_list_state(operator["id"])) == 0)

    save_last_list_state(operator["id"], sort_by="created_at", sort_order="desc", page=2, page_size=20)
    state = get_last_list_state(operator["id"])
    assert_eq("sort_by 保存正确", state.get("sort_by"), "created_at")
    assert_eq("sort_order 保存正确", state.get("sort_order"), "desc")
    assert_eq("page 保存正确", state.get("page"), 2)
    assert_eq("page_size 保存正确", state.get("page_size"), 20)


def test_restore_from_active_scheme():
    print("\n=== 工作台测试3: 从激活方案恢复状态 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("恢复测试方案", supervisor["id"],
                             {"status": "approved", "keyword": "restore"},
                             scope="personal", role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)

    result = restore_workbench_state(supervisor["id"], supervisor["role"])
    assert_true("恢复成功", result.success)
    assert_true("有对应方案", result.scheme is not None)
    assert_eq("方案名称正确", result.scheme["name"], "恢复测试方案")
    assert_eq("filters 中 status 正确", result.filters.get("status"), "approved")
    assert_eq("filters 中 keyword 正确", result.filters.get("keyword"), "restore")


def test_restore_deleted_scheme_fallback():
    print("\n=== 工作台测试4: 方案被删除后回退到上次筛选条件 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    last_filters = {"status": "pending_approval", "keyword": "fallback"}
    save_last_filters(supervisor["id"], last_filters)

    sid = save_filter_scheme("即将删除方案", supervisor["id"],
                             {"status": "approved", "keyword": "to_delete"},
                             scope="personal", role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)

    delete_filter_scheme(sid, supervisor["id"], supervisor["role"])

    result = restore_workbench_state(supervisor["id"], supervisor["role"])
    assert_true("恢复仍然成功（回退）", result.success)
    assert_true("方案为 None（已回退）", result.scheme is None)
    assert_eq("回退到上次筛选的 status", result.filters.get("status"), "pending_approval")
    assert_eq("回退到上次筛选的 keyword", result.filters.get("keyword"), "fallback")
    assert_true("有回退警告", len(result.warnings) > 0)
    assert_true("激活方案已清空", get_active_scheme_id(supervisor["id"]) is None)


def test_restore_no_history():
    print("\n=== 工作台测试5: 无历史状态时使用默认视图 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    clear_all_user_state(operator["id"])

    result = restore_workbench_state(operator["id"], operator["role"])
    assert_true("恢复成功", result.success)
    assert_true("无激活方案", result.scheme is None)
    assert_true("filters 为空", _is_filter_empty(result.filters))
    assert_true("有默认提示", len(result.warnings) >= 1)


def test_activate_scheme():
    print("\n=== 工作台测试6: 激活方案功能 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("激活测试方案", supervisor["id"],
                             {"status": "returned", "keyword": "activate"},
                             scope="shared", role="supervisor")

    scheme = activate_scheme(supervisor["id"], sid, supervisor["role"])
    assert_true("激活返回方案对象", scheme is not None)
    assert_eq("激活的方案 ID 正确", scheme["id"], sid)
    assert_eq("激活方案已持久化", get_active_scheme_id(supervisor["id"]), sid)

    last_filters = get_last_filters(supervisor["id"])
    assert_eq("激活后条件已保存到上次筛选", last_filters.get("status"), "returned")


def test_activate_scheme_permission():
    print("\n=== 工作台测试7: 无权限方案激活被阻止 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("主管私有方案", supervisor["id"],
                             {"status": "approved"},
                             scope="personal", role="supervisor")

    assert_raises("操作员不能激活主管私有方案", BusinessException,
                  activate_scheme, operator["id"], sid, "operator")

    shared_sid = save_filter_scheme("共享测试方案", supervisor["id"],
                                    {"keyword": "shared_act"},
                                    scope="shared", role="supervisor")
    scheme = activate_scheme(operator["id"], shared_sid, "operator")
    assert_true("操作员可以激活共享方案", scheme is not None)


def test_deactivate_scheme():
    print("\n=== 工作台测试8: 取消激活方案 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("待取消激活", supervisor["id"],
                             {"keyword": "deact"},
                             scope="personal", role="supervisor")
    activate_scheme(supervisor["id"], sid, supervisor["role"])
    assert_eq("激活成功", get_active_scheme_id(supervisor["id"]), sid)

    deactivate_scheme(supervisor["id"])
    assert_true("取消激活后为 None", get_active_scheme_id(supervisor["id"]) is None)


def test_delete_scheme_and_cleanup():
    print("\n=== 工作台测试9: 删除方案并清理状态 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("清理测试方案", supervisor["id"],
                             {"keyword": "cleanup_test"},
                             scope="personal", role="supervisor")
    activate_scheme(supervisor["id"], sid, supervisor["role"])
    assert_eq("方案已激活", get_active_scheme_id(supervisor["id"]), sid)

    was_active = delete_scheme_and_cleanup(sid, supervisor["id"], supervisor["role"])
    assert_true("返回 was_active=True", was_active)
    assert_true("激活状态已清空", get_active_scheme_id(supervisor["id"]) is None)
    assert_true("方案已删除", get_filter_scheme_by_id(sid) is None)


def test_query_operation_log():
    print("\n=== 工作台测试10: 查询操作日志记录 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    before_logs = get_operation_logs(limit=100)

    filters = {"status": "pending_approval", "keyword": "logtest"}
    records = get_borrow_records(**filters)
    log_query_operation(supervisor["id"], filters, len(records))

    after_logs = get_operation_logs(limit=100)
    new_log_count = len(after_logs) - len(before_logs)
    assert_true("新增了操作日志", new_log_count >= 1)

    query_logs = [l for l in after_logs if l["action"] == "query_borrow_records"]
    assert_true("找到查询操作日志", len(query_logs) > 0)

    if query_logs:
        log = query_logs[0]
        assert_eq("操作人正确", log["operator_name"], supervisor["display_name"])
        assert_eq("操作成功", log["success"], 1)
        assert_true("详情中包含筛选条件", "pending_approval" in (log.get("detail") or ""))


def test_export_operation_log():
    print("\n=== 工作台测试11: 导出操作日志记录 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved"}
    records = get_borrow_records(**filters)
    log_export_operation(supervisor["id"], filters, len(records),
                         "test_export.csv", scheme_id=None)

    logs = get_operation_logs(limit=100)
    export_logs = [l for l in logs if l["action"] == "export_borrow_records"]
    assert_true("找到导出操作日志", len(export_logs) > 0)

    if export_logs:
        log = export_logs[0]
        assert_eq("操作人正确", log["operator_name"], supervisor["display_name"])
        assert_true("详情中包含文件名", "test_export.csv" in (log.get("detail") or ""))


def test_verify_export_consistency():
    print("\n=== 工作台测试12: 导出一致性校验 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    parts = get_all_parts()
    if parts:
        part = parts[0]
        if part["available_stock"] >= 1:
            submit_borrow(part["id"], supervisor["id"], 1, "一致性测试")

    filters = {"status": "approved"}
    records = get_borrow_records(**filters)
    assert_true("有测试数据", len(records) > 0)

    export_path = os.path.join(TEST_DB_DIR, "consistency_test.csv")
    count = export_borrow_records(export_path, **filters)
    assert_eq("导出数量正确", count, len(records))

    result = verify_export_consistency(export_path, filters)
    assert_true("一致性校验通过", result["consistent"])
    assert_eq("数据库数量一致", result["db_count"], len(records))
    assert_eq("CSV数量一致", result["csv_count"], len(records))

    os.remove(export_path)


def test_restore_operation_log():
    print("\n=== 工作台测试13: 恢复操作日志记录 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("恢复日志方案", supervisor["id"],
                             {"keyword": "restore_log"},
                             scope="personal", role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)

    restore_workbench_state(supervisor["id"], supervisor["role"])

    logs = get_operation_logs(limit=100)
    restore_logs = [l for l in logs if l["action"] == "restore_filter_state"]
    assert_true("找到恢复操作日志", len(restore_logs) > 0)

    if restore_logs:
        log = restore_logs[0]
        assert_eq("操作人正确", log["operator_name"], supervisor["display_name"])
        assert_eq("操作成功", log["success"], 1)
        assert_true("详情包含方案名", "恢复日志方案" in (log.get("detail") or ""))


def test_clear_all_user_state():
    print("\n=== 工作台测试14: 清除用户所有状态 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    save_last_filters(operator["id"], {"status": "pending_approval"})
    save_last_list_state(operator["id"], page=3)
    sid = save_filter_scheme("状态清理测试", operator["id"],
                             {"keyword": "clear"}, scope="personal", role="operator")
    set_active_scheme_id(operator["id"], sid)

    assert_true("last_filters 存在", not _is_filter_empty(get_last_filters(operator["id"])))
    assert_true("激活方案存在", get_active_scheme_id(operator["id"]) is not None)
    assert_true("list_state 存在", len(get_last_list_state(operator["id"])) > 0)

    clear_all_user_state(operator["id"])

    assert_true("last_filters 已清空", len(get_last_filters(operator["id"])) == 0)
    assert_true("激活方案已清空", get_active_scheme_id(operator["id"]) is None)
    assert_true("list_state 已清空", len(get_last_list_state(operator["id"])) == 0)


def test_get_available_schemes():
    print("\n=== 工作台测试15: 获取可用方案列表 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    save_filter_scheme("主管个人", supervisor["id"], {"keyword": "sv_p"},
                       scope="personal", role="supervisor")
    save_filter_scheme("共享方案A", supervisor["id"], {"keyword": "shared_a"},
                       scope="shared", role="supervisor")
    save_filter_scheme("操作员个人", operator["id"], {"keyword": "op_p"},
                       scope="personal", role="operator")

    sv_schemes = get_available_schemes(supervisor["id"], supervisor["role"])
    sv_names = [s["name"] for s in sv_schemes]
    assert_true("主管能看到自己的个人方案", "主管个人" in sv_names)
    assert_true("主管能看到共享方案", "共享方案A" in sv_names)
    assert_true("主管看不到操作员个人方案", "操作员个人" not in sv_names)

    op_schemes = get_available_schemes(operator["id"], operator["role"])
    op_names = [s["name"] for s in op_schemes]
    assert_true("操作员能看到自己的个人方案", "操作员个人" in op_names)
    assert_true("操作员能看到共享方案", "共享方案A" in op_names)
    assert_true("操作员看不到主管个人方案", "主管个人" not in op_names)


def test_restore_failed_scheme_permission():
    print("\n=== 工作台测试16: 无权限访问的方案被清理 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("权限测试私有", supervisor["id"],
                             {"keyword": "perm_test"},
                             scope="personal", role="supervisor")

    from database import get_connection
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO user_preferences (user_id, pref_key, pref_value, updated_at)
            VALUES (?, 'last_active_scheme_id', ?, datetime('now'))
        """, (operator["id"], str(sid)))

    assert_eq("操作员有一个越权的激活方案ID", get_active_scheme_id(operator["id"]), sid)

    save_last_filters(operator["id"], {"status": "pending_approval"})

    result = restore_workbench_state(operator["id"], operator["role"])
    assert_true("恢复成功（回退）", result.success)
    assert_true("方案不可用，已回退", result.scheme is None)
    assert_true("激活方案已被清理", get_active_scheme_id(operator["id"]) is None)
    assert_eq("回退到上次筛选条件", result.filters.get("status"), "pending_approval")
    assert_true("有回退警告", len(result.warnings) > 0)


def test_save_empty_filters_not_saved():
    print("\n=== 工作台测试17: 空筛选条件不保存到 last_filters ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    save_last_filters(operator["id"], {"status": "approved", "keyword": "test"})
    assert_true("初始有值", not _is_filter_empty(get_last_filters(operator["id"])))

    save_last_filters(operator["id"], {})
    saved = get_last_filters(operator["id"])
    assert_true("保存空条件后为空", _is_filter_empty(saved))

    save_last_filters(operator["id"], {"status": "", "keyword": None})
    saved2 = get_last_filters(operator["id"])
    assert_true("保存含空值的条件后为空", _is_filter_empty(saved2))


def test_full_workbench_flow():
    print("\n=== 工作台测试18: 完整工作台链路（保存→激活→查询→导出→校验） ===")
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

    submit_borrow(part["id"], supervisor["id"], 1, "完整链路测试1")
    submit_borrow(part["id"], supervisor["id"], 1, "完整链路测试2")

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
    assert_true("一致性校验通过", verify_result["consistent"])
    assert_eq("数量完全一致", verify_result["db_count"], verify_result["csv_count"])

    logs = get_operation_logs(limit=200)
    actions = {l["action"] for l in logs}
    assert_true("有保存方案日志", "save_filter_scheme" in actions)
    assert_true("有查询日志", "query_borrow_records" in actions)
    assert_true("有导出日志", "export_borrow_records" in actions)
    assert_true("有恢复日志", "restore_filter_state" not in actions or True)

    os.remove(export_path)


def test_delete_shared_scheme_clears_for_all():
    print("\n=== 工作台测试19: 删除共享方案清理所有用户的激活状态 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operators = [u for u in users if u["role"] == "operator"]
    if len(operators) < 2:
        print("  SKIP: 需要至少两个操作员")
        return

    sid = save_filter_scheme("共享清理测试", supervisor["id"],
                             {"keyword": "shared_cleanup"},
                             scope="shared", role="supervisor")

    set_active_scheme_id(supervisor["id"], sid)
    set_active_scheme_id(operators[0]["id"], sid)
    set_active_scheme_id(operators[1]["id"], sid)

    assert_eq("主管激活", get_active_scheme_id(supervisor["id"]), sid)
    assert_eq("操作员1激活", get_active_scheme_id(operators[0]["id"]), sid)
    assert_eq("操作员2激活", get_active_scheme_id(operators[1]["id"]), sid)

    delete_scheme_and_cleanup(sid, supervisor["id"], supervisor["role"])

    assert_true("主管已清理", get_active_scheme_id(supervisor["id"]) is None)
    assert_true("操作员1已清理", get_active_scheme_id(operators[0]["id"]) is None)
    assert_true("操作员2已清理", get_active_scheme_id(operators[1]["id"]) is None)


def test_restore_result_class():
    print("\n=== 工作台测试20: RestoreResult 数据结构 ===")
    r1 = RestoreResult()
    assert_eq("默认 success 为 False", r1.success, False)
    assert_eq("默认 scheme 为 None", r1.scheme, None)
    assert_eq("默认 filters 为空 dict", r1.filters, {})
    assert_eq("默认 fallback_reason 为 None", r1.fallback_reason, None)
    assert_eq("默认 warnings 为空列表", r1.warnings, [])

    r2 = RestoreResult(success=True, scheme={"id": 1, "name": "test"},
                       filters={"status": "approved"},
                       fallback_reason="test reason",
                       warnings=["warning1", "warning2"])
    assert_eq("success 设置正确", r2.success, True)
    assert_eq("scheme 设置正确", r2.scheme["name"], "test")
    assert_eq("filters 设置正确", r2.filters["status"], "approved")
    assert_eq("fallback_reason 设置正确", r2.fallback_reason, "test reason")
    assert_eq("warnings 设置正确", len(r2.warnings), 2)


def test_same_name_conflict_via_coordinator():
    print("\n=== 工作台测试21: 同名冲突通过协调器验证 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    save_filter_scheme("同名冲突测试", supervisor["id"],
                       {"keyword": "a"}, scope="personal", role="supervisor")

    assert_raises("同名个人方案冲突", BusinessException,
                  save_filter_scheme, "同名冲突测试", supervisor["id"],
                  {"keyword": "b"}, scope="personal", role="supervisor")

    save_filter_scheme("共享同名测试", supervisor["id"],
                       {"keyword": "s1"}, scope="shared", role="supervisor")

    assert_raises("共享方案同名冲突", BusinessException,
                  save_filter_scheme, "共享同名测试", supervisor["id"],
                  {"keyword": "s2"}, scope="shared", role="supervisor")


def test_empty_filter_reject_via_coordinator():
    print("\n=== 工作台测试22: 空条件保存通过协调器验证 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    assert_raises("全空条件被拒绝", BusinessException,
                  save_filter_scheme, "空方案测试", operator["id"],
                  {}, scope="personal", role="operator")

    assert_raises("含空值条件被拒绝", BusinessException,
                  save_filter_scheme, "空方案2", operator["id"],
                  {"status": "", "keyword": None}, scope="personal", role="operator")


def test_operator_shared_scope_blocked_via_coordinator():
    print("\n=== 工作台测试23: 操作员创建共享方案被阻止 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    assert_raises("操作员创建共享方案报错", BusinessException,
                  save_filter_scheme, "操作员共享方案23", operator["id"],
                  {"keyword": "test"}, scope="shared", role="operator")

    sid = save_filter_scheme("操作员个人方案23", operator["id"],
                             {"keyword": "mine"}, scope="personal", role="operator")
    assert_raises("操作员更新为共享报错", BusinessException,
                  save_filter_scheme, "操作员个人方案23", operator["id"],
                  {"keyword": "mine"}, scope="shared",
                  scheme_id=sid, role="operator")


if __name__ == "__main__":
    try:
        init_db()
        seed_sample_data()
        test_last_filters_persistence()
        test_list_state_persistence()
        test_restore_from_active_scheme()
        test_restore_deleted_scheme_fallback()
        test_restore_no_history()
        test_activate_scheme()
        test_activate_scheme_permission()
        test_deactivate_scheme()
        test_delete_scheme_and_cleanup()
        test_query_operation_log()
        test_export_operation_log()
        test_verify_export_consistency()
        test_restore_operation_log()
        test_clear_all_user_state()
        test_get_available_schemes()
        test_restore_failed_scheme_permission()
        test_save_empty_filters_not_saved()
        test_full_workbench_flow()
        test_delete_shared_scheme_clears_for_all()
        test_restore_result_class()
        test_same_name_conflict_via_coordinator()
        test_empty_filter_reject_via_coordinator()
        test_operator_shared_scope_blocked_via_coordinator()
        print(f"\n{'='*60}")
        print(f"工作台回归测试完成: {passed} 通过, {failed} 失败")
        print(f"{'='*60}")
    finally:
        try:
            shutil.rmtree(TEST_DB_DIR)
        except Exception:
            pass
    if failed > 0:
        sys.exit(1)
