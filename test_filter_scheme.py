import os
import sys
import csv
import tempfile
import shutil

TEST_DB_DIR = tempfile.mkdtemp(prefix="filter_scheme_test_")
TEST_DB_PATH = os.path.join(TEST_DB_DIR, "test_spare_parts.db")

os.environ["FILTER_SCHEME_TEST_DB"] = TEST_DB_PATH

import database as db_mod
db_mod.DB_PATH = TEST_DB_PATH

from database import init_db, seed_sample_data
from services import (
    save_filter_scheme, get_filter_schemes, delete_filter_scheme,
    get_filter_scheme_by_id, get_borrow_records, get_all_users,
    submit_borrow, get_all_parts, BusinessException, _is_filter_empty,
    _serialize_filters, _deserialize_filters,
    save_user_preference, get_user_preference,
    set_active_scheme_id, get_active_scheme_id
)
from exporter import export_borrow_records

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


def test_filter_helpers():
    print("\n=== 测试1: 筛选条件序列化/反序列化辅助函数 ===")
    filters = {"status": "pending_approval", "keyword": "CPU", "borrower_id": 2,
               "date_from": "2025-01-01", "date_to": "", "extra_none": None}
    json_str = _serialize_filters(filters)
    restored = _deserialize_filters(json_str)
    assert_eq("status 保留", restored.get("status"), "pending_approval")
    assert_eq("keyword 保留", restored.get("keyword"), "CPU")
    assert_eq("borrower_id 保留", restored.get("borrower_id"), 2)
    assert_eq("date_from 保留", restored.get("date_from"), "2025-01-01")
    assert_true("date_to 空串被清除", "date_to" not in restored)
    assert_true("extra_none 被清除", "extra_none" not in restored)
    assert_true("全空判断", _is_filter_empty({}))
    assert_true("全空判断-含空值", _is_filter_empty({"a": "", "b": None}))
    assert_true("非空判断", not _is_filter_empty({"status": "approved"}))


def test_save_and_list_scheme():
    print("\n=== 测试2: 保存方案 & 按权限列出方案 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid1 = save_filter_scheme("待审批方案", supervisor["id"],
                              {"status": "pending_approval"}, scope="personal",
                              role="supervisor")
    assert_true("主管个人方案 ID > 0", sid1 > 0)

    sid2 = save_filter_scheme("全量共享方案", supervisor["id"],
                              {"keyword": "CPU"}, scope="shared",
                              role="supervisor")
    assert_true("共享方案 ID > 0", sid2 > 0)

    sid3 = save_filter_scheme("我的借出", operator["id"],
                              {"status": "approved", "borrower_id": operator["id"]},
                              scope="personal", role="operator")
    assert_true("操作员个人方案 ID > 0", sid3 > 0)

    sv_schemes = get_filter_schemes(supervisor["id"], "supervisor")
    sv_names = [s["name"] for s in sv_schemes]
    assert_true("主管能看到自己的个人方案", "待审批方案" in sv_names)
    assert_true("主管能看到共享方案", "全量共享方案" in sv_names)
    assert_true("主管看不到操作员的个人方案", "我的借出" not in sv_names)

    op_schemes = get_filter_schemes(operator["id"], "operator")
    op_names = [s["name"] for s in op_schemes]
    assert_true("操作员能看到自己的个人方案", "我的借出" in op_names)
    assert_true("操作员能看到共享方案", "全量共享方案" in op_names)
    assert_true("操作员看不到主管的个人方案", "待审批方案" not in op_names)

    for s in sv_schemes:
        assert_true("主管方案列表含 owner_name 字段", "owner_name" in s)
    for s in op_schemes:
        assert_true("操作员方案列表含 owner_name 字段", "owner_name" in s)


def test_same_name_conflict():
    print("\n=== 测试3: 同名方案冲突 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    save_filter_scheme("唯一名", operator["id"], {"keyword": "test"},
                       scope="personal", role="operator")
    assert_raises("同名个人方案重复保存应报错", BusinessException,
                  save_filter_scheme, "唯一名", operator["id"],
                  {"keyword": "other"}, scope="personal", role="operator")

    save_filter_scheme("共享唯一", supervisor["id"], {"keyword": "shared1"},
                       scope="shared", role="supervisor")
    assert_raises("共享方案同名冲突应报错", BusinessException,
                  save_filter_scheme, "共享唯一", supervisor["id"],
                  {"keyword": "shared2"}, scope="shared", role="supervisor")

    other_op = [u for u in users if u["role"] == "operator"][1] if len([u for u in users if u["role"] == "operator"]) > 1 else None
    if other_op:
        sid_other = save_filter_scheme("唯一名", other_op["id"], {"keyword": "diff_user"},
                                       scope="personal", role="operator")
        assert_true("不同用户可以有同名个人方案", sid_other > 0)

    assert_raises("个人方案与共享方案同名冲突", BusinessException,
                  save_filter_scheme, "共享唯一", operator["id"],
                  {"keyword": "clash"}, scope="personal", role="operator")


def test_empty_filter_reject():
    print("\n=== 测试4: 空条件保存拒绝 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]
    assert_raises("全空条件保存应报错", BusinessException,
                  save_filter_scheme, "空方案", operator["id"],
                  {}, scope="personal", role="operator")
    assert_raises("含空值条件保存应报错", BusinessException,
                  save_filter_scheme, "空方案2", operator["id"],
                  {"status": "", "keyword": ""}, scope="personal", role="operator")

    sid = save_filter_scheme("条件后清空测试", operator["id"],
                             {"status": "approved"}, scope="personal", role="operator")
    assert_raises("更新时条件清空应报错", BusinessException,
                  save_filter_scheme, "条件后清空测试", operator["id"],
                  {"status": "", "keyword": ""}, scope="personal",
                  scheme_id=sid, role="operator")


def test_update_scheme():
    print("\n=== 测试5: 更新已有方案 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("待更新方案", supervisor["id"],
                             {"status": "approved"}, scope="personal",
                             role="supervisor")
    new_sid = save_filter_scheme("已更新方案", supervisor["id"],
                                 {"status": "returned"}, scope="shared",
                                 scheme_id=sid, role="supervisor")
    assert_eq("更新后 ID 不变", new_sid, sid)
    updated = get_filter_scheme_by_id(sid)
    assert_eq("名称已更新", updated["name"], "已更新方案")
    assert_eq("scope 已更新", updated["scope"], "shared")
    assert_eq("filters 已更新", updated["filters"].get("status"), "returned")

    sid_op = save_filter_scheme("操作员方案", operator["id"],
                                {"keyword": "mine"}, scope="personal",
                                role="operator")
    assert_raises("他人不能更新自己的方案", BusinessException,
                  save_filter_scheme, "被篡改", supervisor["id"],
                  {"keyword": "hacked"}, scope="personal",
                  scheme_id=sid_op, role="supervisor")


def test_delete_scheme_and_fallback():
    print("\n=== 测试6: 删除方案 & 被删后视图回退 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    sid = save_filter_scheme("即将删除", supervisor["id"],
                             {"status": "rollback"}, scope="personal",
                             role="supervisor")
    scheme = get_filter_scheme_by_id(sid)
    assert_true("删除前能查到方案", scheme is not None)

    set_active_scheme_id(supervisor["id"], sid)
    assert_eq("激活方案已持久化", get_active_scheme_id(supervisor["id"]), sid)

    delete_filter_scheme(sid, supervisor["id"], "supervisor")
    gone = get_filter_scheme_by_id(sid)
    assert_true("删除后查不到方案", gone is None)

    op_schemes = get_filter_schemes(supervisor["id"], "supervisor")
    assert_true("删除后方案不在列表中", "即将删除" not in [s["name"] for s in op_schemes])

    set_active_scheme_id(supervisor["id"], sid)
    stale = get_active_scheme_id(supervisor["id"])
    stale_scheme = get_filter_scheme_by_id(stale) if stale else None
    assert_true("被删方案的 ID 在 get_filter_scheme_by_id 返回 None",
                stale_scheme is None)


def test_permission_boundary():
    print("\n=== 测试7: 权限边界 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("主管专属方案", supervisor["id"],
                             {"status": "rejected"}, scope="personal",
                             role="supervisor")
    assert_raises("操作员不能删除主管个人方案", BusinessException,
                  delete_filter_scheme, sid, operator["id"], "operator")
    assert_raises("操作员不能创建共享方案", BusinessException,
                  save_filter_scheme, "操作员共享", operator["id"],
                  {"keyword": "hack"}, scope="shared", role="operator")
    delete_filter_scheme(sid, supervisor["id"], "supervisor")

    shared_sid = save_filter_scheme("共享可删方案", supervisor["id"],
                                    {"keyword": "test"}, scope="shared",
                                    role="supervisor")
    assert_raises("操作员不能删除共享方案", BusinessException,
                  delete_filter_scheme, shared_sid, operator["id"], "operator")
    delete_filter_scheme(shared_sid, supervisor["id"], "supervisor")
    assert_true("主管可以删除自己的共享方案", True)

    other_sv = None
    for u in users:
        if u["role"] == "supervisor" and u["id"] != supervisor["id"]:
            other_sv = u
            break
    if other_sv:
        other_personal = save_filter_scheme("另一主管私有", other_sv["id"],
                                            {"keyword": "private"}, scope="personal",
                                            role="supervisor")
        assert_raises("主管不能删除其他主管的私有方案", BusinessException,
                      delete_filter_scheme, other_personal, supervisor["id"], "supervisor")
        other_shared = save_filter_scheme("另一主管共享", other_sv["id"],
                                          {"keyword": "pub"}, scope="shared",
                                          role="supervisor")
        delete_filter_scheme(other_shared, supervisor["id"], "supervisor")
        assert_true("主管可以删除他人创建的共享方案", True)


def test_borrow_records_date_filter():
    print("\n=== 测试8: get_borrow_records 支持 date_from/date_to ===")
    all_records = get_borrow_records()
    if len(all_records) == 0:
        print("  SKIP: 无借还记录数据，跳过日期筛选测试")
        return
    sample_date = all_records[0]["created_at"][:10]
    filtered = get_borrow_records(date_from=sample_date, date_to=sample_date + "z")
    assert_true("日期筛选返回列表", isinstance(filtered, list))


def test_export_with_filter_params():
    print("\n=== 测试9: 按筛选参数导出 CSV ===")
    export_path = os.path.join(TEST_DB_DIR, "test_export.csv")
    count = export_borrow_records(export_path, status=None, borrower_id=None,
                                  keyword=None, date_from=None, date_to=None)
    assert_true("导出文件存在", os.path.exists(export_path))
    assert_true("导出行数 >= 0", count >= 0)
    if count > 0:
        with open(export_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
        assert_true("CSV 有表头行", len(rows) >= 1)
        assert_true("CSV 有数据行", len(rows) >= 2)
    os.remove(export_path)


def test_restart_persistence():
    print("\n=== 测试10: 跨重启保留（模拟重新打开数据库连接） ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    sid = save_filter_scheme("持久化测试", supervisor["id"],
                             {"status": "approved", "keyword": "内存"},
                             scope="shared", role="supervisor")
    import sqlite3
    from database import get_connection
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM filter_schemes WHERE id = ?", (sid,)).fetchone()
        assert_true("数据库中方案记录存在", row is not None)
        assert_eq("数据库中名称一致", dict(row)["name"], "持久化测试")

    restored = get_filter_scheme_by_id(sid)
    assert_true("通过服务层能重新读出方案", restored is not None)
    assert_eq("filters 反序列化正确", restored["filters"].get("keyword"), "内存")


def test_operation_log_consistency():
    print("\n=== 测试11: 操作日志一致性 ===")
    from services import get_operation_logs
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    sid = save_filter_scheme("日志验证方案", supervisor["id"],
                             {"status": "borrowed"}, scope="personal",
                             role="supervisor")
    logs = get_operation_logs(limit=50)
    found = any(
        log["action"] == "save_filter_scheme" and "日志验证方案" in (log.get("detail") or "")
        for log in logs
    )
    assert_true("保存方案操作记录在操作日志中", found)

    delete_filter_scheme(sid, supervisor["id"], "supervisor")
    logs2 = get_operation_logs(limit=50)
    found2 = any(
        log["action"] == "delete_filter_scheme" and "日志验证方案" in (log.get("detail") or "")
        for log in logs2
    )
    assert_true("删除方案操作记录在操作日志中", found2)


def test_user_preference_persistence():
    print("\n=== 测试12: 用户偏好跨重启持久化 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    assert_true("初始无偏好返回 None", get_user_preference(operator["id"], "last_active_scheme_id") is None)
    assert_true("初始无激活方案", get_active_scheme_id(operator["id"]) is None)

    sid = save_filter_scheme("偏好测试方案", operator["id"],
                             {"keyword": "pref"}, scope="personal",
                             role="operator")
    set_active_scheme_id(operator["id"], sid)
    assert_eq("偏好已写入", get_active_scheme_id(operator["id"]), sid)

    from database import get_connection
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE user_id = ? AND pref_key = ?",
            (operator["id"], "last_active_scheme_id")
        ).fetchone()
        assert_true("数据库中偏好记录存在", row is not None)
        assert_eq("数据库中偏好值一致", row["pref_value"], str(sid))

    restored = get_active_scheme_id(operator["id"])
    assert_eq("重读偏好值正确", restored, sid)

    set_active_scheme_id(operator["id"], None)
    assert_true("清除偏好后返回 None", get_active_scheme_id(operator["id"]) is None)


def test_active_scheme_restore_after_relogin():
    print("\n=== 测试13: 重新登录后恢复上次激活方案 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("重启恢复方案", supervisor["id"],
                             {"status": "approved", "keyword": "restore"},
                             scope="shared", role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)

    restored_id = get_active_scheme_id(supervisor["id"])
    assert_eq("重新获取激活方案 ID 正确", restored_id, sid)

    scheme = get_filter_scheme_by_id(restored_id)
    assert_true("通过恢复的 ID 能查到方案", scheme is not None)
    assert_eq("恢复方案的名称正确", scheme["name"], "重启恢复方案")
    assert_eq("恢复方案的 filters 正确", scheme["filters"].get("keyword"), "restore")


def test_export_consistency_with_scheme():
    print("\n=== 测试14: 按方案导出与列表筛选结果一致 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("导出一致性方案", operator["id"],
                             {"status": "approved"}, scope="personal",
                             role="operator")
    scheme = get_filter_scheme_by_id(sid)

    list_records = get_borrow_records(status=scheme["filters"].get("status"))
    export_path = os.path.join(TEST_DB_DIR, "test_scheme_export.csv")
    export_count = export_borrow_records(export_path,
                                          status=scheme["filters"].get("status"))
    assert_eq("导出记录数与列表查询一致", export_count, len(list_records))

    if export_count > 0:
        with open(export_path, "r", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            csv_rows = list(reader)
        assert_eq("CSV 数据行数与列表一致", len(csv_rows) - 1, len(list_records))
    os.remove(export_path)


def test_delete_active_scheme_safe_fallback():
    print("\n=== 测试15: 删除当前激活方案后安全回退 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("待删除激活方案", supervisor["id"],
                             {"keyword": "fallback"}, scope="personal",
                             role="supervisor")
    set_active_scheme_id(supervisor["id"], sid)
    assert_eq("方案已设为激活", get_active_scheme_id(supervisor["id"]), sid)

    delete_filter_scheme(sid, supervisor["id"], "supervisor")

    stale_id = get_active_scheme_id(supervisor["id"])
    stale_scheme = get_filter_scheme_by_id(stale_id) if stale_id else None
    assert_true("被删方案的 ID 对应的方案已不存在", stale_scheme is None)

    set_active_scheme_id(supervisor["id"], None)
    assert_true("清除激活后偏好为 None", get_active_scheme_id(supervisor["id"]) is None)

    all_records = get_borrow_records()
    assert_true("默认状态下仍能查询全部记录", isinstance(all_records, list))


def test_operator_shared_scope_blocked():
    print("\n=== 测试16: 操作员创建共享方案被阻止 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    assert_raises("操作员新建 shared 方案报错", BusinessException,
                  save_filter_scheme, "非法共享", operator["id"],
                  {"keyword": "hack"}, scope="shared", role="operator")

    sid = save_filter_scheme("个人转共享测试", operator["id"],
                             {"keyword": "mine"}, scope="personal",
                             role="operator")
    assert_raises("操作员更新个人方案为 shared 报错", BusinessException,
                  save_filter_scheme, "个人转共享测试", operator["id"],
                  {"keyword": "mine"}, scope="shared",
                  scheme_id=sid, role="operator")


def test_same_name_update_excludes_self():
    print("\n=== 测试17: 更新同名方案时排除自身 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("自我更名", supervisor["id"],
                             {"keyword": "rename"}, scope="personal",
                             role="supervisor")

    new_sid = save_filter_scheme("自我更名", supervisor["id"],
                                 {"keyword": "renamed"}, scope="personal",
                                 scheme_id=sid, role="supervisor")
    assert_eq("同方案同名称更新成功 ID 不变", new_sid, sid)

    updated = get_filter_scheme_by_id(sid)
    assert_eq("filters 已更新", updated["filters"].get("keyword"), "renamed")


def test_different_users_same_name_personal():
    print("\n=== 测试18: 不同用户可以有同名个人方案 ===")
    users = get_all_users()
    operators = [u for u in users if u["role"] == "operator"]
    if len(operators) < 2:
        print("  SKIP: 需要至少两个操作员用户")
        return

    sid1 = save_filter_scheme("我的筛选", operators[0]["id"],
                              {"keyword": "op1"}, scope="personal",
                              role="operator")
    sid2 = save_filter_scheme("我的筛选", operators[1]["id"],
                              {"keyword": "op2"}, scope="personal",
                              role="operator")
    assert_true("不同用户同名个人方案各自创建成功", sid1 > 0 and sid2 > 0)
    assert_true("不同用户的方案 ID 不同", sid1 != sid2)

    s1 = get_filter_scheme_by_id(sid1)
    s2 = get_filter_scheme_by_id(sid2)
    assert_eq("方案1的 keyword", s1["filters"].get("keyword"), "op1")
    assert_eq("方案2的 keyword", s2["filters"].get("keyword"), "op2")


def test_scheme_list_and_log_export_triplet_consistency():
    print("\n=== 测试19: 列表刷新/CSV导出/操作日志 三处结果一致 ===")
    from services import get_operation_logs
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    sid = save_filter_scheme("一致性验证方案", supervisor["id"],
                             {"status": "pending_approval"}, scope="shared",
                             role="supervisor")
    scheme = get_filter_scheme_by_id(sid)

    list_result = get_borrow_records(status=scheme["filters"].get("status"))
    export_path = os.path.join(TEST_DB_DIR, "test_triplet.csv")
    export_count = export_borrow_records(export_path,
                                          status=scheme["filters"].get("status"))
    assert_eq("列表与导出数量一致", len(list_result), export_count)

    logs = get_operation_logs(limit=100)
    scheme_log = [l for l in logs if l["action"] == "save_filter_scheme"
                  and "一致性验证方案" in (l.get("detail") or "")]
    assert_true("操作日志中有保存方案记录", len(scheme_log) > 0)
    if scheme_log:
        assert_eq("日志中 target_id 与方案 ID 一致", scheme_log[0].get("target_id"), sid)

    os.remove(export_path)


if __name__ == "__main__":
    try:
        init_db()
        seed_sample_data()
        test_filter_helpers()
        test_save_and_list_scheme()
        test_same_name_conflict()
        test_empty_filter_reject()
        test_update_scheme()
        test_delete_scheme_and_fallback()
        test_permission_boundary()
        test_borrow_records_date_filter()
        test_export_with_filter_params()
        test_restart_persistence()
        test_operation_log_consistency()
        test_user_preference_persistence()
        test_active_scheme_restore_after_relogin()
        test_export_consistency_with_scheme()
        test_delete_active_scheme_safe_fallback()
        test_operator_shared_scope_blocked()
        test_same_name_update_excludes_self()
        test_different_users_same_name_personal()
        test_scheme_list_and_log_export_triplet_consistency()
        print(f"\n{'='*60}")
        print(f"测试完成: {passed} 通过, {failed} 失败")
        print(f"{'='*60}")
    finally:
        try:
            shutil.rmtree(TEST_DB_DIR)
        except Exception:
            pass
    if failed > 0:
        sys.exit(1)
