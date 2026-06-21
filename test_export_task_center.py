import os
import sys
import csv
import json
import shutil
import tempfile

TEST_DB_DIR = tempfile.mkdtemp(prefix="export_task_test_")
TEST_DB_PATH = os.path.join(TEST_DB_DIR, "test_export_task.db")

os.environ["WORKBENCH_TEST_DB"] = TEST_DB_PATH

import database as db_mod
db_mod.DB_PATH = TEST_DB_PATH

from database import init_db, seed_sample_data, get_connection
from services import (
    save_filter_scheme, get_filter_schemes, delete_filter_scheme,
    get_filter_scheme_by_id, get_borrow_records, get_all_users,
    submit_borrow, get_all_parts, BusinessException, _is_filter_empty,
    get_operation_logs, get_user_by_id
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
    TASK_STATUS_FAILED, TASK_STATUS_CANCELLED, TASK_STATUS_PENDING_CONFIRMATION,
    EXPORT_TASK_DISPLAY, TASK_TYPE_DISPLAY,
)

passed = 0
failed = 0
_test_counter = 0


def _unique_keyword():
    global _test_counter
    _test_counter += 1
    return f"test_{_test_counter}_unique"


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


def test_submit_borrow_export_task():
    print("\n=== 导出任务测试1: 提交借还记录导出任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(
        filters=filters,
        sort_by="created_at",
        sort_order="desc",
        page=1,
        page_size=20,
    )
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    assert_true("任务创建成功", task is not None)
    assert_eq("任务类型正确", task["task_type"], TASK_TYPE_BORROW)
    assert_eq("任务状态为 pending", task["status"], TASK_STATUS_PENDING)
    assert_true("有任务编号", task["task_no"].startswith("ET"))
    assert_true("有预计条数", task["record_count"] >= 0)
    assert_true("有数据指纹", task["data_fingerprint"] is not None and len(task["data_fingerprint"]) > 0)

    filters_data = json.loads(task["filters_snapshot"])
    assert_eq("筛选条件保存正确", filters_data.get("status"), "approved")


def test_submit_stock_export_task():
    print("\n=== 导出任务测试2: 提交库存明细导出任务 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    filters = {"keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(filters=filters)
    task = submit_export_task(operator["id"], TASK_TYPE_STOCK, snapshot)

    assert_true("任务创建成功", task is not None)
    assert_eq("任务类型正确", task["task_type"], TASK_TYPE_STOCK)
    assert_eq("任务状态为 pending", task["status"], TASK_STATUS_PENDING)


def test_task_snapshot_persistence():
    print("\n=== 导出任务测试3: 任务快照完整保存（筛选、排序、分页、列配置） ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(
        filters=filters,
        sort_by="part_code",
        sort_order="asc",
        page=3,
        page_size=50,
        columns=["record_no", "part_code", "part_name"],
    )
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    saved_filters = json.loads(task["filters_snapshot"])
    assert_eq("筛选条件 status 保存正确", saved_filters.get("status"), "approved")

    if task.get("sort_snapshot"):
        sort_data = json.loads(task["sort_snapshot"])
        assert_eq("排序字段保存正确", sort_data.get("sort_by"), "part_code")
        assert_eq("排序方向保存正确", sort_data.get("sort_order"), "asc")

    if task.get("page_snapshot"):
        page_data = json.loads(task["page_snapshot"])
        assert_eq("页码保存正确", page_data.get("page"), 3)
        assert_eq("每页条数保存正确", page_data.get("page_size"), 50)

    if task.get("columns_snapshot"):
        columns_data = json.loads(task["columns_snapshot"])
        assert_eq("列配置保存正确", len(columns_data), 3)


def test_process_pending_task():
    print("\n=== 导出任务测试4: 处理等待中的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(filters=filters)
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    task_id = task["id"]

    process_pending_tasks()

    updated = get_export_task(task_id)
    assert_true("任务已完成", updated["status"] == TASK_STATUS_SUCCESS)
    assert_true("有导出文件路径", updated.get("export_file_path") is not None and len(updated["export_file_path"]) > 0)
    assert_true("有导出条数", updated.get("export_count", 0) >= 0)
    assert_true("有完成时间", updated.get("completed_at") is not None)

    if updated.get("export_file_path") and os.path.exists(updated["export_file_path"]):
        with open(updated["export_file_path"], "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            csv_rows = list(reader)
        assert_eq("CSV行数与导出条数一致", len(csv_rows), updated["export_count"])


def test_stock_task_export():
    print("\n=== 导出任务测试5: 库存明细导出 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(operator["id"], TASK_TYPE_STOCK, snapshot)
    process_pending_tasks()

    updated = get_export_task(task["id"])
    assert_eq("库存任务导出成功", updated["status"], TASK_STATUS_SUCCESS)
    assert_true("有导出文件", updated.get("export_file_path") is not None)


def test_stock_log_task_export():
    print("\n=== 导出任务测试6: 库存变动导出 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={})
    task = submit_export_task(supervisor["id"], TASK_TYPE_STOCK_LOG, snapshot)
    process_pending_tasks()

    updated = get_export_task(task["id"])
    assert_eq("库存变动任务导出成功", updated["status"], TASK_STATUS_SUCCESS)
    assert_true("有导出文件", updated.get("export_file_path") is not None)


def test_cancel_pending_task():
    print("\n=== 导出任务测试7: 取消等待中的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    assert_eq("初始状态为 pending", task["status"], TASK_STATUS_PENDING)

    cancelled = cancel_export_task(task["id"], supervisor["id"])
    assert_eq("取消后状态为 cancelled", cancelled["status"], TASK_STATUS_CANCELLED)
    assert_true("有完成时间", cancelled.get("completed_at") is not None)


def test_cancel_other_user_task_blocked():
    print("\n=== 导出任务测试8: 不能取消他人的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    assert_raises("操作员不能取消主管的任务", BusinessException,
                  cancel_export_task, task["id"], operator["id"])


def test_cancel_completed_task_blocked():
    print("\n=== 导出任务测试9: 不能取消已完成的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    assert_eq("任务已完成", completed["status"], TASK_STATUS_SUCCESS)

    assert_raises("不能取消已完成的任务", BusinessException,
                  cancel_export_task, task["id"], supervisor["id"])


def test_retry_failed_task():
    print("\n=== 导出任务测试10: 重试失败的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    task_id = task["id"]

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, error_message = ? WHERE id = ?
        """, (TASK_STATUS_FAILED, "模拟失败", task_id))

    retried = retry_export_task(task_id, supervisor["id"])
    assert_eq("重试后状态为 pending", retried["status"], TASK_STATUS_PENDING)
    assert_true("错误信息已清空", retried.get("error_message") is None)
    assert_true("有新的数据指纹", retried["data_fingerprint"] is not None)


def test_retry_non_failed_task_blocked():
    print("\n=== 导出任务测试11: 只能重试失败的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    assert_raises("不能重试等待中的任务", BusinessException,
                  retry_export_task, task["id"], supervisor["id"])


def test_conflict_detection():
    print("\n=== 导出任务测试12: 冲突检测 - 相同条件重复提交 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot)
    assert_eq("第一个任务创建成功", task1["status"], TASK_STATUS_PENDING)

    snapshot2 = ExportTaskSnapshot(filters=filters)
    assert_raises("相同条件重复提交被拒绝", BusinessException,
                  submit_export_task, operator["id"], TASK_TYPE_BORROW, snapshot2)


def test_conflict_force_submit():
    print("\n=== 导出任务测试13: 强制提交处理冲突 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot)
    task1_id = task1["id"]

    snapshot2 = ExportTaskSnapshot(filters=filters)
    task2 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot2, force=True)
    assert_true("强制提交成功", task2 is not None)
    assert_eq("新任务状态为 pending", task2["status"], TASK_STATUS_PENDING)

    old_task = get_export_task(task1_id)
    assert_eq("旧任务被取消", old_task["status"], TASK_STATUS_CANCELLED)


def test_conflict_different_filters():
    print("\n=== 导出任务测试14: 不同筛选条件不冲突 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    snapshot1 = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task1 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters={"status": "returned", "keyword": _unique_keyword()})
    task2 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot2)

    assert_true("不同条件任务1创建成功", task1 is not None)
    assert_true("不同条件任务2创建成功", task2 is not None)
    assert_true("两个任务ID不同", task1["id"] != task2["id"])


def test_download_availability():
    print("\n=== 导出任务测试15: 下载可用性检查 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    avail = check_download_availability(completed["id"])
    assert_true("已完成任务可下载", avail["available"])
    assert_true("有文件路径", "file_path" in avail)
    assert_true("有导出条数", "export_count" in avail)


def test_download_pending_task_unavailable():
    print("\n=== 导出任务测试16: 等待中的任务不可下载 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    avail = check_download_availability(task["id"])
    assert_false("等待中的任务不可下载", avail["available"])
    assert_true("有不可下载原因", len(avail["reason"]) > 0)


def test_download_expired_file():
    print("\n=== 导出任务测试17: 过期文件提示 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    from datetime import datetime, timedelta
    expired_at = (datetime.now() - timedelta(days=1)).isoformat()

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET expires_at = ? WHERE id = ?
        """, (expired_at, completed["id"]))

    avail = check_download_availability(completed["id"])
    assert_false("过期任务不可下载", avail["available"])
    assert_true("提示包含过期", "过期" in avail["reason"])


def test_download_deleted_file():
    print("\n=== 导出任务测试18: 文件被删除提示 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    file_path = completed.get("export_file_path")
    if file_path and os.path.exists(file_path):
        os.remove(file_path)

    avail = check_download_availability(completed["id"])
    assert_false("文件删除后不可下载", avail["available"])
    assert_true("有原因说明", len(avail["reason"]) > 0)


def test_export_consistency_verification():
    print("\n=== 导出任务测试19: 导出一致性校验 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    parts = get_all_parts()
    if parts and parts[0]["available_stock"] >= 1:
        submit_borrow(parts[0]["id"], supervisor["id"], 1, "一致性校验测试")

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(filters=filters)
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    result = verify_export_task_consistency(completed["id"])
    assert_true("一致性校验通过", result["consistent"])
    assert_eq("提交时条数与CSV一致", result.get("task_record_count"), result.get("csv_count"))


def test_data_changed_detection():
    print("\n=== 导出任务测试20: 源数据变化检测 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(filters=filters)
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    if completed["status"] == TASK_STATUS_SUCCESS:
        with get_connection() as conn:
            conn.execute("""
                UPDATE export_tasks SET data_fingerprint = 'tampered_fingerprint' WHERE id = ?
            """, (completed["id"],))

        result = verify_export_task_consistency(completed["id"])
        assert_false("指纹变化后一致性不通过", result["consistent"])
        assert_true("原因包含数据变化", "变化" in result["reason"] or "不一致" in result["reason"])


def test_cross_restart_recovery():
    print("\n=== 导出任务测试21: 跨重启恢复（running任务标记为失败） ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, started_at = ? WHERE id = ?
        """, (TASK_STATUS_RUNNING, "2025-01-01T00:00:00", task["id"]))

    recover_incomplete_tasks()

    recovered = get_export_task(task["id"])
    assert_eq("running任务恢复为failed", recovered["status"], TASK_STATUS_FAILED)
    assert_true("有错误信息", recovered.get("error_message") is not None and "重启" in recovered["error_message"])

    retried = retry_export_task(task["id"], supervisor["id"])
    assert_eq("可重试恢复的任务", retried["status"], TASK_STATUS_PENDING)


def test_cross_restart_view_task_status():
    print("\n=== 导出任务测试22: 跨重启后可查看任务状态 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    assert_eq("任务已完成", completed["status"], TASK_STATUS_SUCCESS)

    task_no = completed["task_no"]
    found = get_export_task_by_no(task_no)
    assert_true("通过编号可找到任务", found is not None)
    assert_eq("状态正确", found["status"], TASK_STATUS_SUCCESS)

    user_tasks = get_user_export_tasks(supervisor["id"])
    assert_true("用户任务列表非空", len(user_tasks) > 0)
    task_ids = [t["id"] for t in user_tasks]
    assert_true("已完成任务在列表中", task["id"] in task_ids)


def test_cross_restart_redownload():
    print("\n=== 导出任务测试23: 跨重启后可重新下载 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    file_path = completed.get("export_file_path")

    if file_path and os.path.exists(file_path):
        save_dir = tempfile.mkdtemp()
        save_path = os.path.join(save_dir, "redownload_test.csv")
        shutil.copy2(file_path, save_path)

        assert_true("重新下载文件存在", os.path.exists(save_path))
        with open(save_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert_eq("重新下载行数一致", len(rows), completed["export_count"])

        shutil.rmtree(save_dir, ignore_errors=True)


def test_permission_check():
    print("\n=== 导出任务测试24: 权限检查（目录无写入权限） ===")
    export_dir = _get_export_dir()
    ok, err = _check_write_permission(export_dir)
    assert_true("正常目录有写入权限", ok)


def test_disk_space_check():
    print("\n=== 导出任务测试25: 磁盘空间检查 ===")
    export_dir = _get_export_dir()
    ok, err = _check_disk_space(export_dir, 0)
    assert_true("有足够磁盘空间", ok)


def test_get_user_tasks_by_status():
    print("\n=== 导出任务测试26: 按状态筛选任务列表 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot1 = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task1 = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task2 = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot2)

    cancel_export_task(task1["id"], supervisor["id"])

    cancelled_tasks = get_user_export_tasks(supervisor["id"], status=TASK_STATUS_CANCELLED)
    cancelled_ids = [t["id"] for t in cancelled_tasks]
    assert_true("已取消任务在列表中", task1["id"] in cancelled_ids)

    pending_tasks = get_user_export_tasks(supervisor["id"], status=TASK_STATUS_PENDING)
    pending_ids = [t["id"] for t in pending_tasks]
    assert_true("等待中任务在列表中", task2["id"] in pending_ids)


def test_recent_export_tasks():
    print("\n=== 导出任务测试27: 最近导出历史 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"status": "approved", "keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    recent = get_recent_export_tasks(limit=10)
    assert_true("最近导出非空", len(recent) > 0)

    recent_ids = [t["id"] for t in recent]
    completed = get_export_task(task["id"])
    if completed["status"] == TASK_STATUS_SUCCESS:
        assert_true("已完成任务在最近导出中", task["id"] in recent_ids)


def test_data_fingerprint():
    print("\n=== 导出任务测试28: 数据指纹计算 ===")
    records1 = [{"id": 1, "record_no": "BR001"}, {"id": 2, "record_no": "BR002"}]
    records2 = [{"id": 1, "record_no": "BR001"}, {"id": 2, "record_no": "BR002"}]
    records3 = [{"id": 1, "record_no": "BR001"}, {"id": 3, "record_no": "BR003"}]

    fp1 = _compute_data_fingerprint(records1)
    fp2 = _compute_data_fingerprint(records2)
    fp3 = _compute_data_fingerprint(records3)

    assert_eq("相同数据指纹相同", fp1, fp2)
    assert_true("不同数据指纹不同", fp1 != fp3)

    empty_fp = _compute_data_fingerprint([])
    assert_true("空数据有指纹", len(empty_fp) > 0)


def test_export_task_no_not_unique():
    print("\n=== 导出任务测试29: 任务编号唯一 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot1 = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task1 = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task2 = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot2)

    assert_true("两个任务编号不同", task1["task_no"] != task2["task_no"])


def test_export_file_content_matches_list():
    print("\n=== 导出任务测试30: 导出文件内容与提交时列表一致 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    records_before = get_borrow_records(**filters)

    snapshot = ExportTaskSnapshot(filters=filters)
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)
    process_pending_tasks()

    completed = get_export_task(task["id"])
    if completed["status"] == TASK_STATUS_SUCCESS and os.path.exists(completed["export_file_path"]):
        with open(completed["export_file_path"], "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            csv_rows = list(reader)

        assert_eq("CSV行数与提交时查询一致", len(csv_rows), len(records_before))


def test_task_type_display():
    print("\n=== 导出任务测试31: 任务类型和状态显示 ===")
    assert_true("借还记录有显示名", TASK_TYPE_DISPLAY.get(TASK_TYPE_BORROW) is not None)
    assert_true("库存明细有显示名", TASK_TYPE_DISPLAY.get(TASK_TYPE_STOCK) is not None)
    assert_true("库存变动有显示名", TASK_TYPE_DISPLAY.get(TASK_TYPE_STOCK_LOG) is not None)

    for status in [TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS,
                   TASK_STATUS_FAILED, TASK_STATUS_CANCELLED]:
        assert_true(f"状态 {status} 有显示", EXPORT_TASK_DISPLAY.get(status) is not None)


def test_conflict_different_task_type():
    print("\n=== 导出任务测试32: 不同任务类型不冲突 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot1 = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task1 = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot1)

    snapshot2 = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task2 = submit_export_task(supervisor["id"], TASK_TYPE_STOCK, snapshot2)

    assert_true("不同类型任务1创建成功", task1 is not None)
    assert_true("不同类型任务2创建成功", task2 is not None)
    assert_true("任务ID不同", task1["id"] != task2["id"])


def test_conflict_after_task_completed():
    print("\n=== 导出任务测试33: 已完成任务不产生冲突 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot1)
    process_pending_tasks()

    completed = get_export_task(task1["id"])
    assert_eq("任务已完成", completed["status"], TASK_STATUS_SUCCESS)

    snapshot2 = ExportTaskSnapshot(filters=filters)
    task2 = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot2)
    assert_true("完成后可再次提交相同条件", task2 is not None)
    assert_eq("新任务状态为pending", task2["status"], TASK_STATUS_PENDING)


def test_cancel_other_user_task():
    print("\n=== 导出任务测试34: 不能重试他人的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]
    operator = [u for u in users if u["role"] == "operator"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, error_message = ? WHERE id = ?
        """, (TASK_STATUS_FAILED, "模拟失败", task["id"]))

    assert_raises("操作员不能重试主管的任务", BusinessException,
                  retry_export_task, task["id"], operator["id"])


def test_query_records_for_task():
    print("\n=== 导出任务测试35: 按任务类型查询记录 ===")
    borrow_records = _query_records_for_task(TASK_TYPE_BORROW, {"status": "approved"})
    assert_true("借还记录查询返回列表", isinstance(borrow_records, list))

    stock_records = _query_records_for_task(TASK_TYPE_STOCK, {})
    assert_true("库存记录查询返回列表", isinstance(stock_records, list))
    assert_true("库存记录非空", len(stock_records) > 0)

    log_records = _query_records_for_task(TASK_TYPE_STOCK_LOG, {})
    assert_true("库存变动记录查询返回列表", isinstance(log_records, list))


def test_data_changed_on_export():
    print("\n=== 导出任务测试36: 导出时源数据变化进入待确认 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot = ExportTaskSnapshot(filters=filters)
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET data_fingerprint = 'wrong_fingerprint' WHERE id = ?
        """, (task["id"],))

    process_pending_tasks()

    updated = get_export_task(task["id"])
    assert_eq("数据变化时进入待确认", updated["status"], TASK_STATUS_PENDING_CONFIRMATION)
    assert_true("错误信息包含变化提示",
                updated.get("error_message") is not None and "变化" in updated["error_message"])


def test_task_operation_logged():
    print("\n=== 导出任务测试37: 导出任务操作记录日志 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    before_logs = get_operation_logs(limit=500)
    before_count = len([l for l in before_logs if l["action"] == "submit_export_task"])

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    after_logs = get_operation_logs(limit=500)
    after_count = len([l for l in after_logs if l["action"] == "submit_export_task"])

    assert_true("提交任务有日志记录", after_count > before_count)


def test_export_dir_created():
    print("\n=== 导出任务测试38: 导出目录自动创建 ===")
    export_dir = _get_export_dir()
    assert_true("导出目录存在", os.path.exists(export_dir))
    assert_true("导出目录是目录", os.path.isdir(export_dir))


def test_snapshot_from_dict():
    print("\n=== 导出任务测试39: ExportTaskSnapshot 序列化/反序列化 ===")
    original = ExportTaskSnapshot(
        filters={"status": "approved", "keyword": "test"},
        sort_by="created_at",
        sort_order="desc",
        page=2,
        page_size=50,
        columns=["col1", "col2"],
    )
    d = original.to_dict()
    restored = ExportTaskSnapshot.from_dict(d)

    assert_eq("filters 恢复正确", restored.filters, original.filters)
    assert_eq("sort_by 恢复正确", restored.sort_by, original.sort_by)
    assert_eq("sort_order 恢复正确", restored.sort_order, original.sort_order)
    assert_eq("page 恢复正确", restored.page, original.page)
    assert_eq("page_size 恢复正确", restored.page_size, original.page_size)
    assert_eq("columns 恢复正确", restored.columns, original.columns)

    empty = ExportTaskSnapshot.from_dict(None)
    assert_eq("空数据恢复默认filters", empty.filters, {})
    assert_eq("空数据恢复默认page", empty.page, None)


def test_get_task_nonexistent():
    print("\n=== 导出任务测试40: 查询不存在的任务返回None ===")
    result = get_export_task(999999)
    assert_true("不存在任务返回None", result is None)

    result2 = get_export_task_by_no("ET_NONEXISTENT")
    assert_true("不存在编号返回None", result2 is None)


def test_conflict_after_cancel():
    print("\n=== 导出任务测试41: 已取消任务不产生冲突 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot1)

    cancel_export_task(task1["id"], operator["id"])

    snapshot2 = ExportTaskSnapshot(filters=filters)
    task2 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot2)
    assert_true("取消后可再次提交相同条件", task2 is not None)
    assert_eq("新任务状态为pending", task2["status"], TASK_STATUS_PENDING)


def test_multiple_status_filter():
    print("\n=== 导出任务测试42: 多状态筛选任务列表 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    active_tasks = get_user_export_tasks(supervisor["id"],
                                          status=[TASK_STATUS_PENDING, TASK_STATUS_RUNNING],
                                          limit=50)
    assert_true("多状态筛选返回列表", isinstance(active_tasks, list))

    for t in active_tasks:
        assert_true("任务状态在筛选范围内",
                    t["status"] in [TASK_STATUS_PENDING, TASK_STATUS_RUNNING])


def test_force_submit_cancels_conflicting_task():
    print("\n=== 导出任务测试43: 强制提交取消冲突任务 ===")
    users = get_all_users()
    operator = [u for u in users if u["role"] == "operator"][0]

    filters = {"status": "approved", "keyword": _unique_keyword()}
    snapshot1 = ExportTaskSnapshot(filters=filters)
    task1 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot1)
    task1_id = task1["id"]

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ? WHERE id = ?
        """, (TASK_STATUS_RUNNING, task1_id))

    snapshot2 = ExportTaskSnapshot(filters=filters)
    task2 = submit_export_task(operator["id"], TASK_TYPE_BORROW, snapshot2, force=True)

    old_task = get_export_task(task1_id)
    assert_eq("冲突的running任务被取消", old_task["status"], TASK_STATUS_CANCELLED)
    assert_eq("新任务状态为pending", task2["status"], TASK_STATUS_PENDING)
    assert_true("新任务有冲突任务ID记录", task2.get("conflict_task_id") is not None)


def test_cancel_running_task():
    print("\n=== 导出任务测试44: 取消运行中的任务 ===")
    users = get_all_users()
    supervisor = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _unique_keyword()})
    task = submit_export_task(supervisor["id"], TASK_TYPE_BORROW, snapshot)

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, started_at = ? WHERE id = ?
        """, (TASK_STATUS_RUNNING, "2025-01-01T00:00:00", task["id"]))

    cancelled = cancel_export_task(task["id"], supervisor["id"])
    assert_eq("运行中任务取消成功", cancelled["status"], TASK_STATUS_CANCELLED)


if __name__ == "__main__":
    try:
        init_db()
        seed_sample_data()

        test_submit_borrow_export_task()
        test_submit_stock_export_task()
        test_task_snapshot_persistence()
        test_process_pending_task()
        test_stock_task_export()
        test_stock_log_task_export()
        test_cancel_pending_task()
        test_cancel_other_user_task_blocked()
        test_cancel_completed_task_blocked()
        test_retry_failed_task()
        test_retry_non_failed_task_blocked()
        test_conflict_detection()
        test_conflict_force_submit()
        test_conflict_different_filters()
        test_download_availability()
        test_download_pending_task_unavailable()
        test_download_expired_file()
        test_download_deleted_file()
        test_export_consistency_verification()
        test_data_changed_detection()
        test_cross_restart_recovery()
        test_cross_restart_view_task_status()
        test_cross_restart_redownload()
        test_permission_check()
        test_disk_space_check()
        test_get_user_tasks_by_status()
        test_recent_export_tasks()
        test_data_fingerprint()
        test_export_task_no_not_unique()
        test_export_file_content_matches_list()
        test_task_type_display()
        test_conflict_different_task_type()
        test_conflict_after_task_completed()
        test_cancel_other_user_task()
        test_query_records_for_task()
        test_data_changed_on_export()
        test_task_operation_logged()
        test_export_dir_created()
        test_snapshot_from_dict()
        test_get_task_nonexistent()
        test_conflict_after_cancel()
        test_multiple_status_filter()
        test_force_submit_cancels_conflicting_task()
        test_cancel_running_task()

        print(f"\n{'='*60}")
        print(f"导出任务中心回归测试完成: {passed} 通过, {failed} 失败")
        print(f"{'='*60}")
    finally:
        try:
            export_dir = _get_export_dir()
            if os.path.exists(export_dir):
                shutil.rmtree(export_dir, ignore_errors=True)
            shutil.rmtree(TEST_DB_DIR, ignore_errors=True)
        except Exception:
            pass
    if failed > 0:
        sys.exit(1)
