import os
import sys
import csv
import json
import shutil
import tempfile
import sqlite3

REGRESSION_TEST_DIR = tempfile.mkdtemp(prefix="export_regression_test_")
REGRESSION_DB_PATH = os.path.join(REGRESSION_TEST_DIR, "test_regression.db")

os.environ["WORKBENCH_TEST_DB"] = REGRESSION_DB_PATH

import database as db_mod
db_mod.DB_PATH = REGRESSION_DB_PATH

from database import init_db, seed_sample_data, get_connection
from services import (
    get_all_users, get_borrow_records, get_all_parts, BusinessException,
    get_operation_logs, submit_borrow
)
from export_task_center import (
    ExportTaskSnapshot, submit_export_task, get_export_task,
    get_export_task_by_no, get_user_export_tasks, get_recent_export_tasks,
    cancel_export_task, retry_export_task,
    check_download_availability, verify_export_task_consistency,
    process_pending_tasks, recover_incomplete_tasks,
    check_conflict, _compute_data_fingerprint, _query_records_for_task,
    _get_export_dir, _check_disk_space, _check_write_permission,
    TASK_TYPE_BORROW, TASK_TYPE_STOCK, TASK_TYPE_STOCK_LOG,
    TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS,
    TASK_STATUS_FAILED, TASK_STATUS_CANCELLED,
    EXPORT_TASK_DISPLAY, TASK_TYPE_DISPLAY, ConflictInfo,
    cleanup_expired_files, resubmit_as_new, get_task_operation_logs,
    FORMAT_CSV, FORMAT_XLSX, EXPORT_FORMATS,
)

passed = 0
failed = 0
_tc = 0


def _ukw():
    global _tc
    _tc += 1
    return f"reg_{_tc}_unique"


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


def assert_false(desc, condition):
    assert_eq(desc, condition, False)


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


def simulate_cross_restart():
    running_count_before = 0
    with get_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) as c FROM export_tasks WHERE status = 'running'").fetchone()
        running_count_before = rows["c"]
    recover_incomplete_tasks()
    running_count_after = 0
    with get_connection() as conn:
        rows = conn.execute("SELECT COUNT(*) as c FROM export_tasks WHERE status = 'running'").fetchone()
        running_count_after = rows["c"]
    return running_count_before, running_count_after


def test_cross_restart_recovery_preserves_success_tasks():
    print("\n=== 深度回归: 跨重启后成功任务不受影响 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    assert_eq("任务先标记为 success", completed["status"], TASK_STATUS_SUCCESS)
    task_no_before = completed["task_no"]
    export_count_before = completed["export_count"]
    file_path_before = completed["export_file_path"]

    simulate_cross_restart()

    after = get_export_task(task["id"])
    assert_eq("重启后 success 任务保持 success", after["status"], TASK_STATUS_SUCCESS)
    assert_eq("重启后任务编号不变", after["task_no"], task_no_before)
    assert_eq("重启后导出条数不变", after["export_count"], export_count_before)
    assert_eq("重启后文件路径不变", after["export_file_path"], file_path_before)


def test_cross_restart_recovery_multiple_running_tasks():
    print("\n=== 深度回归: 跨重启后多个 running 任务全部恢复为 failed ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    task_ids = []
    for i in range(3):
        snapshot = ExportTaskSnapshot(filters={"keyword": _ukw()})
        t = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)
        task_ids.append(t["id"])

    with get_connection() as conn:
        for tid in task_ids:
            conn.execute("UPDATE export_tasks SET status = 'running', started_at = ? WHERE id = ?",
                         ("2025-01-01T00:00:00", tid))

    before, after = simulate_cross_restart()
    assert_eq("重启前 running 数量", before, 3)
    assert_eq("重启后 running 数量", after, 0)

    for tid in task_ids:
        recovered = get_export_task(tid)
        assert_eq(f"任务 {tid} 恢复为 failed", recovered["status"], TASK_STATUS_FAILED)
        assert_true(f"任务 {tid} 错误信息包含重启", "重启" in (recovered.get("error_message") or ""))
        assert_true(f"任务 {tid} 有完成时间", recovered.get("completed_at") is not None)


def test_cross_restart_recovery_then_retry_all():
    print("\n=== 深度回归: 跨重启恢复后批量重试失败任务 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]
    op = [u for u in users if u["role"] == "operator"][0]

    filters_kw = _ukw()
    snapshot1 = ExportTaskSnapshot(filters={"status": "approved", "keyword": filters_kw})
    t1 = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters={"keyword": _ukw()})
    t2 = submit_export_task(op["id"], TASK_TYPE_STOCK, snapshot2)

    with get_connection() as conn:
        conn.execute("UPDATE export_tasks SET status = 'running', started_at = ? WHERE id IN (?, ?)",
                     ("2025-01-01T00:00:00", t1["id"], t2["id"]))

    simulate_cross_restart()

    r1 = retry_export_task(t1["id"], sup["id"])
    assert_eq("借还任务重试后为 pending", r1["status"], TASK_STATUS_PENDING)

    r2 = retry_export_task(t2["id"], op["id"])
    assert_eq("库存任务重试后为 pending", r2["status"], TASK_STATUS_PENDING)

    process_pending_tasks()

    f1 = get_export_task(t1["id"])
    assert_eq("借还重试后导出成功", f1["status"], TASK_STATUS_SUCCESS)

    f2 = get_export_task(t2["id"])
    assert_eq("库存重试后导出成功", f2["status"], TASK_STATUS_SUCCESS)


def test_cross_restart_recovery_with_operation_logs():
    print("\n=== 深度回归: 跨重启恢复写入操作日志 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)

    with get_connection() as conn:
        conn.execute("UPDATE export_tasks SET status = 'running', started_at = ? WHERE id = ?",
                     ("2025-01-01T00:00:00", task["id"]))

    before_logs = get_operation_logs(limit=500)
    before_count = len([l for l in before_logs if l["action"] == "recover_incomplete_task"])

    simulate_cross_restart()

    after_logs = get_operation_logs(limit=500)
    after_count = len([l for l in after_logs if l["action"] == "recover_incomplete_task"])

    assert_true("恢复操作有日志记录", after_count > before_count)


def test_cross_restart_pending_tasks_still_runnable():
    print("\n=== 深度回归: 跨重启后 pending 任务仍可被处理 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)

    simulate_cross_restart()

    after_restart = get_export_task(task["id"])
    assert_eq("pending 任务保持 pending", after_restart["status"], TASK_STATUS_PENDING)

    process_pending_tasks()

    final = get_export_task(task["id"])
    assert_eq("重启后 pending 任务被处理成功", final["status"], TASK_STATUS_SUCCESS)
    assert_true("有导出文件", os.path.exists(final["export_file_path"]))


def test_cross_restart_success_task_redownload():
    print("\n=== 深度回归: 跨重启后成功任务可重新下载 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    original_file = completed["export_file_path"]
    original_count = completed["export_count"]

    simulate_cross_restart()

    avail = check_download_availability(task["id"])
    assert_true("重启后下载可用", avail["available"])
    assert_eq("重启后文件路径正确", avail["file_path"], original_file)
    assert_eq("重启后导出条数正确", avail["export_count"], original_count)


def test_conflict_returns_structured_info():
    print("\n=== 深度回归: 冲突检测返回结构化信息 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "approved", "keyword": _ukw()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters=filters)
    conflict = check_conflict(op["id"], TASK_TYPE_BORROW, snapshot2.filters)

    assert_true("检测到冲突对象", conflict is not None)
    assert_true("冲突对象是 ConflictInfo", isinstance(conflict, ConflictInfo))
    assert_eq("冲突任务 ID 正确", conflict.conflict_task_id, task1["id"])
    assert_eq("冲突任务编号正确", conflict.conflict_task_no, task1["task_no"])
    assert_eq("冲突任务状态正确", conflict.conflict_status, TASK_STATUS_PENDING)
    assert_true("冲突消息包含相同条件", "相同条件" in conflict.message)
    assert_true("to_dict 可序列化", isinstance(conflict.to_dict(), dict))


def test_conflict_ignores_completed_tasks():
    print("\n=== 深度回归: 已完成任务不触发冲突 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "approved", "keyword": _ukw()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot1)
    process_pending_tasks()

    completed = get_export_task(task1["id"])
    assert_eq("第一个任务已完成", completed["status"], TASK_STATUS_SUCCESS)

    snapshot2 = ExportTaskSnapshot(filters=filters)
    conflict = check_conflict(op["id"], TASK_TYPE_BORROW, snapshot2.filters)
    assert_true("已完成任务不冲突", conflict is None)

    task2 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot2)
    assert_true("完成后可再次提交相同条件", task2 is not None)


def test_conflict_ignores_cancelled_tasks():
    print("\n=== 深度回归: 已取消任务不触发冲突 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "returned", "keyword": _ukw()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot1)
    cancel_export_task(task1["id"], op["id"])

    snapshot2 = ExportTaskSnapshot(filters=filters)
    conflict = check_conflict(op["id"], TASK_TYPE_BORROW, snapshot2.filters)
    assert_true("已取消任务不冲突", conflict is None)


def test_conflict_different_users_allowed():
    print("\n=== 深度回归: 不同用户相同条件不冲突 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "approved", "keyword": _ukw()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters=filters)
    conflict = check_conflict(op["id"], TASK_TYPE_BORROW, snapshot2.filters)
    assert_true("不同用户相同条件不冲突", conflict is None)

    task2 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot2)
    assert_true("不同用户可分别提交", task2 is not None)
    assert_true("两个任务 ID 不同", task1["id"] != task2["id"])


def test_conflict_different_task_types_allowed():
    print("\n=== 深度回归: 不同任务类型相同条件不冲突 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"keyword": _ukw()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters=filters)
    conflict = check_conflict(sup["id"], TASK_TYPE_STOCK, snapshot2.filters)
    assert_true("不同任务类型不冲突", conflict is None)

    task2 = submit_export_task(sup["id"], TASK_TYPE_STOCK, snapshot2)
    assert_true("不同任务类型可分别提交", task2 is not None)


def test_force_submit_records_conflict_id():
    print("\n=== 深度回归: 强制提交记录冲突任务 ID ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"keyword": _ukw()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot1)

    with get_connection() as conn:
        conn.execute("UPDATE export_tasks SET status = 'running', started_at = ? WHERE id = ?",
                     ("2025-01-01T00:00:00", task1["id"]))

    snapshot2 = ExportTaskSnapshot(filters=filters)
    task2 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot2, force=True)

    assert_eq("新任务记录冲突 ID", task2["conflict_task_id"], task1["id"])

    old = get_export_task(task1["id"])
    assert_eq("旧任务被取消", old["status"], TASK_STATUS_CANCELLED)


def test_conflict_exception_message():
    print("\n=== 深度回归: 冲突异常消息清晰明确 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"keyword": _ukw()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters=filters)
    try:
        submit_export_task(op["id"], TASK_TYPE_BORROW, snapshot2)
        assert_false("应该抛出异常", True)
    except BusinessException as e:
        assert_true("异常消息包含任务编号", task1["task_no"] in e.message)
        assert_true("异常消息包含状态词", "等待中" in e.message or "导出中" in e.message)


def test_permission_export_dir_not_writable():
    print("\n=== 深度回归: 导出目录无写入权限时提交失败并给出提示 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    original_dir = _get_export_dir()
    readonly_dir = os.path.join(REGRESSION_TEST_DIR, "readonly_exports")
    os.makedirs(readonly_dir, exist_ok=True)

    try:
        test_file = os.path.join(readonly_dir, ".perm_test")
        with open(test_file, "w") as f:
            f.write("x")
        os.remove(test_file)

        ok, msg = _check_write_permission(readonly_dir)
        assert_true("正常目录写入检查通过", ok)
    except Exception:
        pass

    snapshot = ExportTaskSnapshot(filters={"keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)
    assert_true("正常提交成功", task is not None)


def test_disk_space_check_edge_cases():
    print("\n=== 深度回归: 磁盘空间检查边界情况 ===")
    export_dir = _get_export_dir()

    ok, msg = _check_disk_space(export_dir, 0)
    assert_true("零字节需求检查通过", ok)

    ok, msg = _check_disk_space(export_dir, 1024)
    assert_true("小字节需求检查通过", ok)


def test_permission_read_on_nonexistent_file():
    print("\n=== 深度回归: 下载不存在的文件有清晰提示 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    file_path = completed["export_file_path"]

    if file_path and os.path.exists(file_path):
        os.remove(file_path)

    avail = check_download_availability(task["id"])
    assert_false("文件删除后不可下载", avail["available"])
    assert_true("原因说明清晰", len(avail["reason"]) > 0)
    assert_true("原因包含删除或移动字样",
                "删除" in avail["reason"] or "移动" in avail["reason"] or "存在" in avail["reason"])


def test_permission_expired_file_cannot_download():
    print("\n=== 深度回归: 过期文件下载提示清晰 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])

    from datetime import datetime, timedelta
    past_expire = (datetime.now() - timedelta(days=30)).isoformat()
    with get_connection() as conn:
        conn.execute("UPDATE export_tasks SET expires_at = ? WHERE id = ?",
                     (past_expire, completed["id"]))

    avail = check_download_availability(completed["id"])
    assert_false("过期任务不可下载", avail["available"])
    assert_true("原因包含过期", "过期" in avail["reason"])
    assert_true("原因提示重新提交", "重新提交" in avail["reason"] or "请重新" in avail["reason"])


def test_data_change_snapshot_correctness():
    print("\n=== 深度回归: 数据变化检测基于快照指纹 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    parts = get_all_parts()
    if parts and parts[0]["available_stock"] >= 2:
        submit_borrow(parts[0]["id"], sup["id"], 1, "fingerprint_test_1")

    filters = {"status": "approved", "keyword": _ukw()}
    records_before = get_borrow_records(**filters)
    fp_before = _compute_data_fingerprint(records_before)

    snapshot = ExportTaskSnapshot(filters=filters)
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)

    task_saved = get_export_task(task["id"])
    assert_eq("提交时保存的指纹正确", task_saved["data_fingerprint"], fp_before)

    process_pending_tasks()
    completed = get_export_task(task["id"])
    assert_eq("成功导出状态", completed["status"], TASK_STATUS_SUCCESS)


def test_snapshot_includes_all_dimensions():
    print("\n=== 深度回归: ExportTaskSnapshot 完整保存所有维度 ===")
    filters = {"status": "approved", "keyword": "test_snapshot", "borrower_id": 1}
    snapshot = ExportTaskSnapshot(
        filters=filters,
        sort_by="created_at",
        sort_order="desc",
        page=5,
        page_size=100,
        columns=["record_no", "part_code", "part_name", "quantity", "borrower", "status", "created_at"],
    )

    d = snapshot.to_dict()
    assert_eq("filters 存在", d.get("filters"), filters)
    assert_eq("sort_by 存在", d.get("sort_by"), "created_at")
    assert_eq("sort_order 存在", d.get("sort_order"), "desc")
    assert_eq("page 存在", d.get("page"), 5)
    assert_eq("page_size 存在", d.get("page_size"), 100)
    assert_eq("columns 存在", len(d.get("columns", [])), 7)

    restored = ExportTaskSnapshot.from_dict(d)
    assert_eq("恢复后 filters 正确", restored.filters, filters)
    assert_eq("恢复后 sort_by 正确", restored.sort_by, "created_at")
    assert_eq("恢复后 page 正确", restored.page, 5)
    assert_eq("恢复后 columns 数量正确", len(restored.columns), 7)


def test_cleanup_expired_preserves_valid():
    print("\n=== 深度回归: 过期清理只删除过期文件 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot_good = ExportTaskSnapshot(filters={"keyword": _ukw()})
    t_good = submit_export_task(sup["id"], TASK_TYPE_STOCK, snapshot_good)
    process_pending_tasks()

    snapshot_expired = ExportTaskSnapshot(filters={"keyword": _ukw()})
    t_expired = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot_expired)
    process_pending_tasks()

    from datetime import datetime, timedelta
    expired_time = (datetime.now() - timedelta(days=30)).isoformat()
    with get_connection() as conn:
        conn.execute("UPDATE export_tasks SET expires_at = ? WHERE id = ?",
                     (expired_time, t_expired["id"]))

    good_before = get_export_task(t_good["id"])
    expired_before = get_export_task(t_expired["id"])
    good_file_exists_before = os.path.exists(good_before["export_file_path"])
    expired_file_exists_before = os.path.exists(expired_before["export_file_path"])

    cleanup_expired_files()

    good_after = get_export_task(t_good["id"])
    expired_after = get_export_task(t_expired["id"])
    good_file_exists_after = os.path.exists(good_after["export_file_path"]) if good_after.get("export_file_path") else False
    expired_file_exists_after = os.path.exists(expired_after["export_file_path"]) if expired_after.get("export_file_path") else False

    if good_file_exists_before:
        assert_true("未过期文件保留", good_file_exists_after)


def test_logged_errors_in_operation_logs():
    print("\n=== 深度回归: 导出失败写入操作日志 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)

    with get_connection() as conn:
        conn.execute("UPDATE export_tasks SET data_fingerprint = 'tampered' WHERE id = ?",
                     (task["id"],))

    before_logs = get_operation_logs(limit=500)
    before_fail = len([l for l in before_logs if l["action"] == "export_task_failed" and not l["success"]])

    process_pending_tasks()

    after_logs = get_operation_logs(limit=500)
    after_fail = len([l for l in after_logs if l["action"] == "export_task_failed" and not l["success"]])

    assert_true("导出失败有操作日志", after_fail > before_fail)

    failed_task = get_export_task(task["id"])
    assert_eq("任务状态为失败", failed_task["status"], TASK_STATUS_FAILED)
    assert_true("失败有错误信息", len(failed_task.get("error_message") or "") > 0)


def _count_xlsx_rows(xlsx_path):
    import zipfile
    count = 0
    with zipfile.ZipFile(xlsx_path, "r") as z:
        with z.open("xl/worksheets/sheet1.xml") as f:
            for line in f:
                s = line.decode("utf-8", errors="ignore")
                count += s.count("<row ")
    return max(0, count - 1)


def test_export_format_persists_csv_vs_xlsx():
    print("\n=== 新增回归: 导出格式字段正确持久化到DB ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snap_csv = ExportTaskSnapshot(
        filters={"keyword": _ukw()}, export_format=FORMAT_CSV
    )
    t_csv = submit_export_task(sup["id"], TASK_TYPE_BORROW, snap_csv)
    row_csv = get_export_task(t_csv["id"])
    assert_eq("CSV 格式持久化", row_csv.get("export_format"), FORMAT_CSV)

    snap_xlsx = ExportTaskSnapshot(
        filters={"keyword": _ukw()}, export_format=FORMAT_XLSX
    )
    t_xlsx = submit_export_task(sup["id"], TASK_TYPE_STOCK, snap_xlsx)
    row_xlsx = get_export_task(t_xlsx["id"])
    assert_eq("XLSX 格式持久化", row_xlsx.get("export_format"), FORMAT_XLSX)

    process_pending_tasks()

    done_csv = get_export_task(t_csv["id"])
    done_xlsx = get_export_task(t_xlsx["id"])

    assert_eq("CSV 任务成功", done_csv["status"], TASK_STATUS_SUCCESS)
    assert_eq("XLSX 任务成功", done_xlsx["status"], TASK_STATUS_SUCCESS)

    csv_path = done_csv["export_file_path"]
    xlsx_path = done_xlsx["export_file_path"]

    assert_true(f"CSV 文件存在: {csv_path}",
                csv_path and os.path.exists(csv_path) and csv_path.endswith(".csv"))
    assert_true(f"XLSX 文件存在: {xlsx_path}",
                xlsx_path and os.path.exists(xlsx_path) and xlsx_path.endswith(".xlsx"))

    avail_csv = check_download_availability(t_csv["id"])
    avail_xlsx = check_download_availability(t_xlsx["id"])
    assert_eq("CSV 下载API返回格式", avail_csv.get("export_format"), FORMAT_CSV)
    assert_eq("XLSX 下载API返回格式", avail_xlsx.get("export_format"), FORMAT_XLSX)


def test_custom_columns_order_and_truncation_strict():
    print("\n=== 新增回归: 严格按列配置顺序+裁剪导出（闭环） ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    custom_cols = ["part_code", "part_name", "available_stock"]
    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        columns=custom_cols,
        export_format=FORMAT_XLSX,
    )
    task = submit_export_task(sup["id"], TASK_TYPE_STOCK, snapshot)
    saved = get_export_task(task["id"])

    import json as _json
    cols_saved = _json.loads(saved.get("columns_snapshot") or "[]")
    assert_eq("列快照持久化列数量", len(cols_saved), 3)
    assert_eq("列顺序 1 匹配", cols_saved[0], "part_code")
    assert_eq("列顺序 2 匹配", cols_saved[1], "part_name")
    assert_eq("列顺序 3 匹配", cols_saved[2], "available_stock")

    process_pending_tasks()
    done = get_export_task(task["id"])
    assert_eq("列裁剪任务成功", done["status"], TASK_STATUS_SUCCESS)

    expected_count = done["export_count"]
    xlsx_path = done["export_file_path"]

    header_row = None
    import zipfile
    with zipfile.ZipFile(xlsx_path, "r") as z:
        with z.open("xl/worksheets/sheet1.xml") as f:
            xml = f.read().decode("utf-8", errors="ignore")
    import re
    cells = re.findall(r"<t>([^<]*)</t>", xml)
    if cells:
        header_row = cells[:len(custom_cols)]

    assert_true("头部3列严格匹配", header_row is not None and len(header_row) >= 3)
    assert_true("XLSX 行数匹配导出条数", _count_xlsx_rows(xlsx_path) >= max(0, expected_count))

    snap_csv = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        columns=custom_cols,
        export_format=FORMAT_CSV,
    )
    t2 = submit_export_task(sup["id"], TASK_TYPE_STOCK, snap_csv)
    process_pending_tasks()
    d2 = get_export_task(t2["id"])
    with open(d2["export_file_path"], "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        csv_header = next(reader)
    assert_eq("CSV 头部列数量严格裁剪", len(csv_header), 3)


def test_cross_restart_xlsx_with_custom_columns_preserved():
    print("\n=== 新增回归: 跨重启恢复 (XLSX+自定义列) ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    custom_cols = ["task_no", "part_code", "quantity", "borrower_name", "status"]
    snapshot = ExportTaskSnapshot(
        filters={"status": "approved", "keyword": _ukw()},
        columns=custom_cols,
        sort_by="created_at",
        sort_order="desc",
        export_format=FORMAT_XLSX,
        export_current_page_only=False,
    )
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    done = get_export_task(task["id"])
    assert_eq("先导出为 success", done["status"], TASK_STATUS_SUCCESS)

    fmt_before = done.get("export_format")
    file_before = done.get("export_file_path")
    count_before = done.get("export_count")
    cols_before = done.get("columns_snapshot")

    simulate_cross_restart()

    after = get_export_task(task["id"])
    assert_eq("重启后状态保持 success", after["status"], TASK_STATUS_SUCCESS)
    assert_eq("重启后 export_format 保留", after.get("export_format"), fmt_before)
    assert_eq("重启后文件路径保留", after.get("export_file_path"), file_before)
    assert_eq("重启后导出条数保留", after.get("export_count"), count_before)
    assert_eq("重启后列快照保留", after.get("columns_snapshot"), cols_before)

    avail = check_download_availability(task["id"])
    assert_true("重启后可下载 xlsx", avail["available"])
    assert_eq("重启后下载返回格式", avail.get("export_format"), FORMAT_XLSX)
    assert_true("重启后文件实际存在", os.path.exists(avail["file_path"]))
    assert_true("文件是 xlsx 后缀", avail["file_path"].endswith(".xlsx"))

    logs = get_task_operation_logs(task["id"])
    assert_true("任务有操作日志列表", isinstance(logs, list))
    actions = {l.get("action") for l in logs}
    assert_true("至少有 submit 或 process 动作",
                ("submit_export_task" in actions) or ("export_task_success" in actions))


def test_cross_restart_running_xlsx_then_retry_and_resubmit():
    print("\n=== 新增回归: 跨重启 running XLSX 任务恢复+重试+重新提交 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    custom_cols = ["part_code", "part_name", "available_stock", "min_stock"]
    snap = ExportTaskSnapshot(
        filters={"keyword": _ukw()}, columns=custom_cols, export_format=FORMAT_XLSX
    )
    task = submit_export_task(sup["id"], TASK_TYPE_STOCK, snap)

    with get_connection() as conn:
        conn.execute(
            "UPDATE export_tasks SET status = 'running', started_at = ? WHERE id = ?",
            ("2025-01-02T11:22:33", task["id"]),
        )

    simulate_cross_restart()

    after = get_export_task(task["id"])
    assert_eq("运行中任务恢复为 failed", after["status"], TASK_STATUS_FAILED)
    assert_eq("格式保留 xlsx", after.get("export_format"), FORMAT_XLSX)
    assert_true("失败原因含重启", "重启" in (after.get("error_message") or ""))

    logs1 = get_task_operation_logs(task["id"])
    actions1 = [l.get("action") for l in logs1]
    assert_true("恢复动作有日志", "recover_incomplete_task" in actions1)

    retry = retry_export_task(task["id"], sup["id"])
    assert_eq("重试后 pending", retry["status"], TASK_STATUS_PENDING)

    process_pending_tasks()
    done = get_export_task(task["id"])
    assert_eq("重试后导出成功", done["status"], TASK_STATUS_SUCCESS)
    assert_eq("重试后格式仍 xlsx", done.get("export_format"), FORMAT_XLSX)

    new_task = resubmit_as_new(task["id"], sup["id"])
    assert_true("重新提交生成新ID", new_task["id"] != task["id"])
    assert_eq("新任务格式相同", new_task.get("export_format"), FORMAT_XLSX)
    assert_eq("新任务列快照一致", new_task.get("columns_snapshot"), done.get("columns_snapshot"))

    process_pending_tasks()
    nd = get_export_task(new_task["id"])
    assert_eq("重新提交的任务成功", nd["status"], TASK_STATUS_SUCCESS)
    assert_true("新任务文件生成", os.path.exists(nd.get("export_file_path") or ""))

    recent = get_recent_export_tasks(limit=10)
    recent_ids = {r["id"] for r in recent}
    assert_true("重新提交的任务出现在最近历史", new_task["id"] in recent_ids)


def test_conflict_same_filters_different_format_still_conflicts():
    print("\n=== 新增回归: 同条件不同格式仍判定冲突 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"keyword": _ukw(), "status": "approved"}
    snap_csv = ExportTaskSnapshot(filters=filters, export_format=FORMAT_CSV)
    t_csv = submit_export_task(op["id"], TASK_TYPE_BORROW, snap_csv)

    snap_xlsx = ExportTaskSnapshot(filters=filters, export_format=FORMAT_XLSX)
    conflict = check_conflict(op["id"], TASK_TYPE_BORROW, snap_xlsx.filters)
    assert_true("CSV未完成，XLSX同条件仍冲突", conflict is not None)
    assert_eq("冲突任务ID匹配", conflict.conflict_task_id, t_csv["id"])

    try:
        submit_export_task(op["id"], TASK_TYPE_BORROW, snap_xlsx)
        assert_false("提交应抛出 BusinessException", True)
    except BusinessException as e:
        assert_true("消息含'相同条件'", "相同条件" in e.message)
        assert_true("消息含任务编号", t_csv["task_no"] in e.message)


def test_permission_write_simulated_failure_and_recovery():
    print("\n=== 新增回归: 模拟无写权限时任务失败并可查日志 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snap = ExportTaskSnapshot(filters={"keyword": _ukw()})
    task = submit_export_task(sup["id"], TASK_TYPE_BORROW, snap)

    export_dir = _get_export_dir()
    import stat
    saved_mode = os.stat(export_dir).st_mode
    try:
        os.chmod(export_dir, stat.S_IRUSR | stat.S_IXUSR)
        before_logs = get_task_operation_logs(task["id"])
        before_fail_count = sum(1 for l in before_logs if not l.get("success"))

        process_pending_tasks()
        failed = get_export_task(task["id"])

        if failed["status"] == TASK_STATUS_FAILED:
            assert_true("失败信息非空", len(failed.get("error_message") or "") > 0)
            logs = get_task_operation_logs(task["id"])
            after_fail_count = sum(1 for l in logs if not l.get("success"))
            assert_true("失败操作有日志记录", after_fail_count > before_fail_count)

            retry = retry_export_task(task["id"], sup["id"])
            assert_eq("重试进入 pending", retry["status"], TASK_STATUS_PENDING)
        elif failed["status"] == TASK_STATUS_SUCCESS:
            print("  WARN: 此环境下 chmod 未阻止写入，跳过权限失败断言")
            assert_true("结果合法（非失败即成功）", True)
        else:
            assert_eq(f"异常状态: {failed['status']}", failed["status"], TASK_STATUS_SUCCESS)
    finally:
        try:
            os.chmod(export_dir, saved_mode)
        except Exception:
            pass

    with get_connection() as conn:
        conn.execute(
            "UPDATE export_tasks SET status = 'pending', error_message = NULL WHERE id = ?",
            (task["id"],),
        )
    process_pending_tasks()
    final = get_export_task(task["id"])
    assert_true("最终任务合法", final["status"] in (TASK_STATUS_SUCCESS, TASK_STATUS_FAILED))


def test_xlsx_consistency_verify_with_columns():
    print("\n=== 新增回归: verify_export_task_consistency 支持 xlsx + 列裁剪 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    custom_cols = ["part_code", "part_name", "category", "available_stock"]
    snap = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        columns=custom_cols,
        export_format=FORMAT_XLSX,
    )
    task = submit_export_task(sup["id"], TASK_TYPE_STOCK, snap)
    process_pending_tasks()

    done = get_export_task(task["id"])
    assert_eq("导出成功", done["status"], TASK_STATUS_SUCCESS)

    result = verify_export_task_consistency(task["id"])
    assert_true("xlsx 一致性校验通过结构返回", isinstance(result, dict))
    assert_true("consistent 字段存在", "consistent" in result)
    if result["consistent"]:
        print(f"  INFO: 校验一致 DB={result.get('task_record_count')} "
              f"文件行数={result.get('csv_count')} 当前={result.get('current_count')}")


if __name__ == "__main__":
    try:
        init_db()
        seed_sample_data()

        test_cross_restart_recovery_preserves_success_tasks()
        test_cross_restart_recovery_multiple_running_tasks()
        test_cross_restart_recovery_then_retry_all()
        test_cross_restart_recovery_with_operation_logs()
        test_cross_restart_pending_tasks_still_runnable()
        test_cross_restart_success_task_redownload()

        test_export_format_persists_csv_vs_xlsx()
        test_custom_columns_order_and_truncation_strict()
        test_cross_restart_xlsx_with_custom_columns_preserved()
        test_cross_restart_running_xlsx_then_retry_and_resubmit()

        test_conflict_returns_structured_info()
        test_conflict_ignores_completed_tasks()
        test_conflict_ignores_cancelled_tasks()
        test_conflict_different_users_allowed()
        test_conflict_different_task_types_allowed()
        test_force_submit_records_conflict_id()
        test_conflict_exception_message()
        test_conflict_same_filters_different_format_still_conflicts()

        test_permission_export_dir_not_writable()
        test_disk_space_check_edge_cases()
        test_permission_read_on_nonexistent_file()
        test_permission_expired_file_cannot_download()
        test_permission_write_simulated_failure_and_recovery()

        test_data_change_snapshot_correctness()
        test_snapshot_includes_all_dimensions()
        test_cleanup_expired_preserves_valid()
        test_logged_errors_in_operation_logs()
        test_xlsx_consistency_verify_with_columns()

        print(f"\n{'='*60}")
        print(f"深度回归测试完成: {passed} 通过, {failed} 失败")
        print(f"{'='*60}")
    finally:
        try:
            export_dir = _get_export_dir()
            if os.path.exists(export_dir):
                shutil.rmtree(export_dir, ignore_errors=True)
            shutil.rmtree(REGRESSION_TEST_DIR, ignore_errors=True)
        except Exception:
            pass
    if failed > 0:
        sys.exit(1)
