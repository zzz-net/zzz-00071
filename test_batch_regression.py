import os
import sys
import csv
import json
import shutil
import tempfile
import sqlite3

REGRESSION_TEST_DIR = tempfile.mkdtemp(prefix="batch_regression_test_")
REGRESSION_DB_PATH = os.path.join(REGRESSION_TEST_DIR, "test_batch_regression.db")

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
    cancel_export_task, retry_export_task, confirm_pending_task,
    check_download_availability, verify_export_task_consistency,
    process_pending_tasks, recover_incomplete_tasks,
    check_conflict, _compute_data_fingerprint, _query_records_for_task,
    _get_export_dir, _check_disk_space, _check_write_permission,
    TASK_TYPE_BORROW, TASK_TYPE_STOCK, TASK_TYPE_STOCK_LOG,
    TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS,
    TASK_STATUS_FAILED, TASK_STATUS_CANCELLED, TASK_STATUS_PENDING_CONFIRMATION,
    EXPORT_TASK_DISPLAY, TASK_TYPE_DISPLAY, ConflictInfo,
    cleanup_expired_files, resubmit_as_new, get_task_operation_logs,
    FORMAT_CSV, FORMAT_XLSX, EXPORT_FORMATS,
    get_batch_snapshot, get_batch_tasks, get_user_batches,
    get_batch_aggregate_status,
)
from export_batch_archive import (
    create_export_batch, get_batch_detail, cancel_batch, retry_batch,
    confirm_batch, verify_batch_consistency, check_batch_downloads,
    get_batch_operation_logs, get_user_batch_list_by_status,
    BATCH_STATUS_DISPLAY,
)

passed = 0
failed = 0
_tc = 0


def _ukw():
    global _tc
    _tc += 1
    return f"batch_reg_{_tc}_unique"


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


def test_batch_csv_and_xlsx_coexist():
    print("\n=== 批次回归: CSV和Excel同批次并存 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")
    assert_true("批次编号非空", batch_no != "")
    assert_true("批次编号以EB开头", batch_no.startswith("EB"))

    batch = get_batch_snapshot(batch_no)
    assert_true("批次快照存在", batch is not None)
    assert_eq("批次任务类型", batch["task_type"], TASK_TYPE_STOCK)

    tasks = get_batch_tasks(batch_no)
    assert_eq("批次内任务数量", len(tasks), 2)

    fmt_set = {t.get("export_format") for t in tasks}
    assert_true("CSV和XLSX并存", fmt_set == {FORMAT_CSV, FORMAT_XLSX})

    process_pending_tasks()

    tasks_after = get_batch_tasks(batch_no)
    for t in tasks_after:
        assert_eq(f"任务 {t['task_no']} 成功", t["status"], TASK_STATUS_SUCCESS)

    agg = get_batch_aggregate_status(batch_no)
    assert_eq("批次聚合状态为成功", agg, TASK_STATUS_SUCCESS)

    csv_task = [t for t in tasks_after if t.get("export_format") == FORMAT_CSV][0]
    xlsx_task = [t for t in tasks_after if t.get("export_format") == FORMAT_XLSX][0]
    assert_true("CSV文件存在", os.path.exists(csv_task["export_file_path"]))
    assert_true("XLSX文件存在", os.path.exists(xlsx_task["export_file_path"]))
    assert_true("CSV后缀正确", csv_task["export_file_path"].endswith(".csv"))
    assert_true("XLSX后缀正确", xlsx_task["export_file_path"].endswith(".xlsx"))


def test_batch_data_change_interception():
    print("\n=== 批次回归: 数据变更拦截与待确认状态 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    with get_connection() as conn:
        conn.execute(
            "UPDATE export_tasks SET data_fingerprint = 'tampered_fp' WHERE batch_no = ?",
            (batch_no,)
        )

    process_pending_tasks()

    tasks = get_batch_tasks(batch_no)
    for t in tasks:
        assert_eq(f"任务 {t['task_no']} 数据变化进入待确认", t["status"], TASK_STATUS_PENDING_CONFIRMATION)
        assert_true(f"任务 {t['task_no']} 错误信息包含变化", "变化" in (t.get("error_message") or ""))

    agg = get_batch_aggregate_status(batch_no)
    assert_eq("批次聚合状态为待确认", agg, TASK_STATUS_PENDING_CONFIRMATION)

    detail = get_batch_detail(batch_no)
    assert_eq("详情状态文本为待确认", detail["status_text"], "待确认")
    assert_eq("详情待确认数量", detail["confirm_count"], 2)


def test_batch_confirm_after_data_change():
    print("\n=== 批次回归: 确认待确认任务后重新导出 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    with get_connection() as conn:
        conn.execute(
            "UPDATE export_tasks SET data_fingerprint = 'tampered_fp' WHERE batch_no = ?",
            (batch_no,)
        )

    process_pending_tasks()

    confirm_result = confirm_batch(batch_no, sup["id"])
    assert_eq("确认数量", confirm_result["confirmed_count"], 2)

    tasks = get_batch_tasks(batch_no)
    for t in tasks:
        assert_eq(f"确认后任务状态为pending", t["status"], TASK_STATUS_PENDING)

    process_pending_tasks()

    tasks_final = get_batch_tasks(batch_no)
    for t in tasks_final:
        assert_eq(f"最终任务状态为success", t["status"], TASK_STATUS_SUCCESS)


def test_batch_retry_after_failure():
    print("\n=== 批次回归: 批次级重试失败任务 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    tasks = get_batch_tasks(batch_no)
    task_ids = [t["id"] for t in tasks]

    with get_connection() as conn:
        for tid in task_ids:
            conn.execute(
                "UPDATE export_tasks SET status = ?, error_message = ? WHERE id = ?",
                (TASK_STATUS_FAILED, "模拟失败", tid)
            )

    retry_result = retry_batch(batch_no, sup["id"])
    assert_eq("重试数量", retry_result["retried_count"], 2)

    tasks_after = get_batch_tasks(batch_no)
    for t in tasks_after:
        assert_eq(f"重试后任务为pending", t["status"], TASK_STATUS_PENDING)

    process_pending_tasks()

    tasks_final = get_batch_tasks(batch_no)
    for t in tasks_final:
        assert_eq(f"重试后导出成功", t["status"], TASK_STATUS_SUCCESS)


def test_batch_cross_restart_recovery():
    print("\n=== 批次回归: 跨重启恢复批次任务 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    tasks = get_batch_tasks(batch_no)
    task_ids = [t["id"] for t in tasks]

    with get_connection() as conn:
        for tid in task_ids:
            conn.execute(
                "UPDATE export_tasks SET status = ?, started_at = ? WHERE id = ?",
                (TASK_STATUS_RUNNING, "2025-01-01T00:00:00", tid)
            )

    recover_incomplete_tasks()

    tasks_after = get_batch_tasks(batch_no)
    for t in tasks_after:
        assert_eq(f"重启后running任务标记为failed", t["status"], TASK_STATUS_FAILED)
        assert_true(f"错误信息含重启", "重启" in (t.get("error_message") or ""))

    agg = get_batch_aggregate_status(batch_no)
    assert_eq("批次聚合状态为失败", agg, TASK_STATUS_FAILED)

    retry_result = retry_batch(batch_no, sup["id"])
    assert_eq("重试数量", retry_result["retried_count"], 2)

    process_pending_tasks()

    tasks_final = get_batch_tasks(batch_no)
    for t in tasks_final:
        assert_eq(f"重启恢复重试后成功", t["status"], TASK_STATUS_SUCCESS)


def test_batch_cross_restart_pending_confirmation_preserved():
    print("\n=== 批次回归: 跨重启后待确认状态保持 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV])
    batch_no = result["task"].get("batch_no", "")

    with get_connection() as conn:
        conn.execute(
            "UPDATE export_tasks SET status = ?, error_message = ? WHERE batch_no = ?",
            (TASK_STATUS_PENDING_CONFIRMATION, "数据已变化", batch_no)
        )

    recover_incomplete_tasks()

    tasks = get_batch_tasks(batch_no)
    for t in tasks:
        assert_eq("待确认状态保持不变", t["status"], TASK_STATUS_PENDING_CONFIRMATION)

    confirm_result = confirm_batch(batch_no, sup["id"])
    assert_eq("确认数量", confirm_result["confirmed_count"], 1)


def test_batch_duplicate_detection_with_different_format():
    print("\n=== 批次回归: 不同格式不算重复 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"keyword": _ukw()}

    snapshot1 = ExportTaskSnapshot(filters=filters, export_format=FORMAT_CSV)
    result1 = create_export_batch(op["id"], TASK_TYPE_STOCK, snapshot1, formats=[FORMAT_CSV])
    assert_true("CSV批次创建成功", result1["task"] is not None)

    snapshot2 = ExportTaskSnapshot(filters=filters, export_format=FORMAT_XLSX)
    result2 = create_export_batch(op["id"], TASK_TYPE_STOCK, snapshot2, formats=[FORMAT_XLSX])
    assert_true("XLSX批次创建成功（同条件不同格式不冲突）", result2["task"] is not None)


def test_batch_duplicate_detection_same_format_same_columns():
    print("\n=== 批次回归: 相同条件+列+格式算重复 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"keyword": _ukw()}
    columns = ["part_code", "part_name"]

    snapshot1 = ExportTaskSnapshot(filters=filters, columns=columns, export_format=FORMAT_CSV)
    result1 = create_export_batch(op["id"], TASK_TYPE_STOCK, snapshot1, formats=[FORMAT_CSV])
    assert_true("第一个CSV批次创建成功", result1["task"] is not None)

    snapshot2 = ExportTaskSnapshot(filters=filters, columns=columns, export_format=FORMAT_CSV)
    assert_raises("相同条件+列+格式重复提交被拒绝", BusinessException,
                  create_export_batch, op["id"], TASK_TYPE_STOCK, snapshot2, [FORMAT_CSV])


def test_batch_duplicate_detection_different_columns_not_duplicate():
    print("\n=== 批次回归: 相同条件不同列不算重复 ===")
    users = get_all_users()
    op = [u for u in users if u["role"] == "operator"][0]

    filters = {"keyword": _ukw()}

    snapshot1 = ExportTaskSnapshot(filters=filters, columns=["part_code", "part_name"], export_format=FORMAT_CSV)
    result1 = create_export_batch(op["id"], TASK_TYPE_STOCK, snapshot1, formats=[FORMAT_CSV])
    assert_true("列配置A创建成功", result1["task"] is not None)

    snapshot2 = ExportTaskSnapshot(filters=filters, columns=["part_code", "part_name", "category"], export_format=FORMAT_CSV)
    result2 = create_export_batch(op["id"], TASK_TYPE_STOCK, snapshot2, formats=[FORMAT_CSV])
    assert_true("不同列配置创建成功", result2["task"] is not None)


def test_batch_consistency_verification():
    print("\n=== 批次回归: 批次一致性校验 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    process_pending_tasks()

    verify_result = verify_batch_consistency(batch_no)
    assert_true("批次一致性通过", verify_result.get("consistent"))
    assert_eq("校验结果数量", len(verify_result.get("results", [])), 2)

    for r in verify_result.get("results", []):
        assert_true(f"任务 {r.get('task_no', '')} 一致", r.get("consistent") is True)


def test_batch_download_availability():
    print("\n=== 批次回归: 批次下载可用性 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    process_pending_tasks()

    downloads = check_batch_downloads(batch_no)
    assert_eq("可下载数量", len(downloads["available"]), 2)
    assert_eq("不可下载数量", len(downloads["unavailable"]), 0)

    for d in downloads["available"]:
        assert_true(f"文件存在 {d['format']}", os.path.exists(d["file_path"]))


def test_batch_cancel():
    print("\n=== 批次回归: 批次取消 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV, FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    cancel_result = cancel_batch(batch_no, sup["id"])
    assert_eq("取消数量", cancel_result["cancelled_count"], 2)

    tasks = get_batch_tasks(batch_no)
    for t in tasks:
        assert_eq(f"任务已取消", t["status"], TASK_STATUS_CANCELLED)

    agg = get_batch_aggregate_status(batch_no)
    assert_eq("批次聚合状态为已取消", agg, TASK_STATUS_CANCELLED)


def test_batch_operation_logs():
    print("\n=== 批次回归: 批次操作日志 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(
        filters={"keyword": _ukw()},
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot,
                                 formats=[FORMAT_CSV])
    batch_no = result["task"].get("batch_no", "")

    process_pending_tasks()

    logs = get_batch_operation_logs(batch_no)
    assert_true("批次有操作日志", len(logs) > 0)

    actions = {l.get("action") for l in logs}
    assert_true("日志包含提交操作", "submit_export_task" in actions)


def test_batch_frozen_snapshot_preserved():
    print("\n=== 批次回归: 批次冻结快照保留完整 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    filters = {"keyword": _ukw()}
    columns = ["part_code", "part_name", "category"]

    snapshot = ExportTaskSnapshot(
        filters=filters,
        sort_by="part_code",
        sort_order="asc",
        page=1,
        page_size=50,
        columns=columns,
        export_format=FORMAT_CSV,
    )
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot, formats=[FORMAT_CSV])
    batch_no = result["task"].get("batch_no", "")

    batch = get_batch_snapshot(batch_no)
    assert_true("批次快照存在", batch is not None)

    saved_filters = json.loads(batch["filters_snapshot"])
    assert_eq("筛选条件保存正确", saved_filters.get("keyword"), filters["keyword"])

    if batch.get("sort_snapshot"):
        sort_data = json.loads(batch["sort_snapshot"])
        assert_eq("排序字段保存正确", sort_data.get("sort_by"), "part_code")
        assert_eq("排序方向保存正确", sort_data.get("sort_order"), "asc")

    if batch.get("columns_snapshot"):
        cols = json.loads(batch["columns_snapshot"])
        assert_eq("列配置保存正确", cols, columns)

    assert_true("冻结数据非空", batch.get("frozen_data_json") is not None and len(batch["frozen_data_json"]) > 0)


def test_user_batch_list_filter():
    print("\n=== 批次回归: 用户批次列表按状态筛选 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot1 = ExportTaskSnapshot(filters={"keyword": _ukw()}, export_format=FORMAT_CSV)
    create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot1, formats=[FORMAT_CSV])
    process_pending_tasks()

    snapshot2 = ExportTaskSnapshot(filters={"keyword": _ukw()}, export_format=FORMAT_CSV)
    create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot2, formats=[FORMAT_CSV])

    success_batches = get_user_batch_list_by_status(sup["id"], status_filter="success", limit=50)
    assert_true("成功批次非空", len(success_batches) > 0)
    for b in success_batches:
        agg = get_batch_aggregate_status(b["batch_no"])
        assert_eq("筛选结果为成功", agg, TASK_STATUS_SUCCESS)

    active_batches = get_user_batch_list_by_status(sup["id"], status_filter="active", limit=50)
    assert_true("进行中批次非空", len(active_batches) > 0)


def test_single_format_batch():
    print("\n=== 批次回归: 单格式批次正常工作 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    snapshot = ExportTaskSnapshot(filters={"keyword": _ukw()}, export_format=FORMAT_XLSX)
    result = create_export_batch(sup["id"], TASK_TYPE_STOCK, snapshot, formats=[FORMAT_XLSX])
    batch_no = result["task"].get("batch_no", "")

    tasks = get_batch_tasks(batch_no)
    assert_eq("单格式批次任务数", len(tasks), 1)
    assert_eq("格式为XLSX", tasks[0].get("export_format"), FORMAT_XLSX)

    process_pending_tasks()

    tasks_after = get_batch_tasks(batch_no)
    assert_eq("单格式导出成功", tasks_after[0]["status"], TASK_STATUS_SUCCESS)
    assert_true("XLSX文件存在", os.path.exists(tasks_after[0]["export_file_path"]))


def test_batch_three_types():
    print("\n=== 批次回归: 三种任务类型批次 ===")
    users = get_all_users()
    sup = [u for u in users if u["role"] == "supervisor"][0]

    for task_type in [TASK_TYPE_BORROW, TASK_TYPE_STOCK, TASK_TYPE_STOCK_LOG]:
        snapshot = ExportTaskSnapshot(filters={"keyword": _ukw()}, export_format=FORMAT_CSV)
        result = create_export_batch(sup["id"], task_type, snapshot, formats=[FORMAT_CSV])
        batch_no = result["task"].get("batch_no", "")
        process_pending_tasks()

        tasks = get_batch_tasks(batch_no)
        assert_eq(f"{task_type} 批次导出成功", tasks[0]["status"], TASK_STATUS_SUCCESS)


if __name__ == "__main__":
    try:
        init_db()
        seed_sample_data()

        test_batch_csv_and_xlsx_coexist()
        test_batch_data_change_interception()
        test_batch_confirm_after_data_change()
        test_batch_retry_after_failure()
        test_batch_cross_restart_recovery()
        test_batch_cross_restart_pending_confirmation_preserved()
        test_batch_duplicate_detection_with_different_format()
        test_batch_duplicate_detection_same_format_same_columns()
        test_batch_duplicate_detection_different_columns_not_duplicate()
        test_batch_consistency_verification()
        test_batch_download_availability()
        test_batch_cancel()
        test_batch_operation_logs()
        test_batch_frozen_snapshot_preserved()
        test_user_batch_list_filter()
        test_single_format_batch()
        test_batch_three_types()

        print(f"\n{'='*60}")
        print(f"批次归档回归测试完成: {passed} 通过, {failed} 失败")
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
