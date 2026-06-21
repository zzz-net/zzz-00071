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
    _serialize_filters, _deserialize_filters
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
                              {"status": "pending_approval"}, scope="personal")
    assert_true("主管个人方案 ID > 0", sid1 > 0)

    sid2 = save_filter_scheme("全量共享方案", supervisor["id"],
                              {"keyword": "CPU"}, scope="shared")
    assert_true("共享方案 ID > 0", sid2 > 0)

    sid3 = save_filter_scheme("我的借出", operator["id"],
                              {"status": "approved", "borrower_id": operator["id"]},
                              scope="personal")
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


def test_same_name_conflict():
    print("\n=== 测试3: 同名方案冲突 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]
    save_filter_scheme("唯一名", operator["id"], {"keyword": "test"}, scope="personal")
    assert_raises("同名方案重复保存应报错", BusinessException,
                  save_filter_scheme, "唯一名", operator["id"],
                  {"keyword": "other"}, scope="personal")


def test_empty_filter_reject():
    print("\n=== 测试4: 空条件保存拒绝 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]
    assert_raises("全空条件保存应报错", BusinessException,
                  save_filter_scheme, "空方案", operator["id"],
                  {}, scope="personal")
    assert_raises("含空值条件保存应报错", BusinessException,
                  save_filter_scheme, "空方案2", operator["id"],
                  {"status": "", "keyword": ""}, scope="personal")


def test_update_scheme():
    print("\n=== 测试5: 更新已有方案 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    sid = save_filter_scheme("待更新方案", supervisor["id"],
                             {"status": "approved"}, scope="personal")
    new_sid = save_filter_scheme("已更新方案", supervisor["id"],
                                 {"status": "returned"}, scope="shared",
                                 scheme_id=sid)
    assert_eq("更新后 ID 不变", new_sid, sid)
    updated = get_filter_scheme_by_id(sid)
    assert_eq("名称已更新", updated["name"], "已更新方案")
    assert_eq("scope 已更新", updated["scope"], "shared")
    assert_eq("filters 已更新", updated["filters"].get("status"), "returned")


def test_delete_scheme_and_fallback():
    print("\n=== 测试6: 删除方案 & 被删后视图回退 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    sid = save_filter_scheme("即将删除", supervisor["id"],
                             {"status": "rollback"}, scope="personal")
    scheme = get_filter_scheme_by_id(sid)
    assert_true("删除前能查到方案", scheme is not None)
    delete_filter_scheme(sid, supervisor["id"], "supervisor")
    gone = get_filter_scheme_by_id(sid)
    assert_true("删除后查不到方案", gone is None)

    op_schemes = get_filter_schemes(supervisor["id"], "supervisor")
    assert_true("删除后方案不在列表中", "即将删除" not in [s["name"] for s in op_schemes])


def test_permission_boundary():
    print("\n=== 测试7: 权限边界 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    sid = save_filter_scheme("主管专属方案", supervisor["id"],
                             {"status": "rejected"}, scope="personal")
    assert_raises("操作员不能删除主管个人方案", BusinessException,
                  delete_filter_scheme, sid, operator["id"], "operator")
    delete_filter_scheme(sid, supervisor["id"], "supervisor")

    shared_sid = save_filter_scheme("共享可删方案", supervisor["id"],
                                    {"keyword": "test"}, scope="shared")
    delete_filter_scheme(shared_sid, supervisor["id"], "supervisor")
    assert_true("主管可以删除自己的共享方案", True)


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
                             scope="shared")
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
                             {"status": "borrowed"}, scope="personal")
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
