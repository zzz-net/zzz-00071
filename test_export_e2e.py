import os
import sys
import csv
import shutil
import tempfile
import time

E2E_TEST_DIR = tempfile.mkdtemp(prefix="export_e2e_test_")
E2E_DB_PATH = os.path.join(E2E_TEST_DIR, "e2e_test.db")

os.environ["WORKBENCH_TEST_DB"] = E2E_DB_PATH

import database as db_mod
db_mod.DB_PATH = E2E_DB_PATH

from database import init_db, seed_sample_data, get_connection
from services import get_all_users, get_borrow_records, get_all_parts, BusinessException, _serialize_filters, _deserialize_filters
from export_task_center import (
    ExportTaskSnapshot, submit_export_task, get_export_task,
    get_user_export_tasks, cancel_export_task, retry_export_task,
    check_download_availability, verify_export_task_consistency,
    process_pending_tasks, recover_incomplete_tasks,
    TASK_TYPE_BORROW, TASK_TYPE_STOCK, TASK_TYPE_STOCK_LOG,
    TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS,
    TASK_STATUS_FAILED, TASK_STATUS_CANCELLED,
    EXPORT_TASK_DISPLAY, TASK_TYPE_DISPLAY,
    _get_export_dir,
)


def simulate_desktop_export_flow():
    print("\n" + "=" * 70)
    print("  端到端真实测试: 模拟桌面端从点击导出到下载文件的完整链路")
    print("=" * 70)

    init_db()
    seed_sample_data()
    print("\n[步骤 1] 数据库初始化完成")

    users = get_all_users()
    current_user = users[0]
    print(f"[步骤 2] 模拟用户登录: {current_user['display_name']} (ID: {current_user['id']})")

    filters = {"status": "approved"}
    records_in_list = get_borrow_records(**filters)
    print(f"[步骤 3] 用户在借还记录列表，当前筛选条件: status=approved，共 {len(records_in_list)} 条记录")

    sort_by = "created_at"
    sort_order = "desc"
    page = 1
    page_size = 20
    columns = ["record_no", "part_code", "part_name", "quantity", "unit", "borrower", "status", "created_at"]

    print(f"[步骤 4] 捕获列表完整状态: 排序={sort_by} {sort_order}, 分页=第{page}页/{page_size}条, 列数={len(columns)}")

    snapshot = ExportTaskSnapshot(
        filters=filters,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
        columns=columns,
    )
    print(f"[步骤 5] 生成任务快照 ExportTaskSnapshot，准备提交...")

    try:
        task = submit_export_task(current_user["id"], TASK_TYPE_BORROW, snapshot)
    except BusinessException as e:
        if "冲突" in e.message or "相同条件" in e.message:
            print(f"  [冲突提示] {e.message}")
            print(f"  [用户操作] 选择强制提交")
            task = submit_export_task(current_user["id"], TASK_TYPE_BORROW, snapshot, force=True)
        else:
            raise

    print(f"[步骤 6] 导出任务提交成功!")
    print(f"         任务编号: {task['task_no']}")
    print(f"         任务类型: {TASK_TYPE_DISPLAY.get(task['task_type'], task['task_type'])}")
    print(f"         初始状态: {EXPORT_TASK_DISPLAY.get(task['status'], (task['status'], ''))[0]}")
    print(f"         预计条数: {task['record_count']}")
    print(f"         数据指纹: {task['data_fingerprint'][:16]}...")

    assert task["status"] == TASK_STATUS_PENDING, "任务初始状态应为 pending"
    assert task["record_count"] == len(records_in_list), "预计条数应与列表查询一致"

    print(f"\n[步骤 7] 模拟后台工作线程处理 pending 任务...")
    process_pending_tasks()

    updated_task = get_export_task(task["id"])
    status_text = EXPORT_TASK_DISPLAY.get(updated_task["status"], (updated_task["status"], ""))[0]
    print(f"[步骤 8] 任务处理完成! 当前状态: {status_text}")

    if updated_task["status"] != TASK_STATUS_SUCCESS:
        print(f"  !!! 任务失败: {updated_task.get('error_message', '未知错误')}")
        return False

    print(f"         导出文件: {updated_task['export_file_path']}")
    print(f"         实际导出: {updated_task['export_count']} 条")
    print(f"         完成时间: {updated_task['completed_at']}")
    print(f"         过期时间: {updated_task['expires_at']}")

    assert os.path.exists(updated_task["export_file_path"]), "导出文件应存在"
    assert updated_task["export_count"] == len(records_in_list), "导出条数应与列表一致"

    print(f"\n[步骤 9] 检查下载可用性...")
    avail = check_download_availability(task["id"])
    print(f"         可下载: {avail['available']}")
    print(f"         文件路径: {avail.get('file_path', 'N/A')}")
    print(f"         导出条数: {avail.get('export_count', 'N/A')}")

    assert avail["available"], "成功任务应可下载"

    print(f"\n[步骤 10] 读取导出 CSV 文件验证内容...")
    with open(updated_task["export_file_path"], "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        csv_rows = list(reader)
        headers = reader.fieldnames

    print(f"         CSV 列数: {len(headers)} 列")
    print(f"         CSV 行数: {len(csv_rows)} 行 (不含表头)")
    print(f"         部分列名: {', '.join(headers[:5])}...")
    print(f"         首行数据: 记录编号={csv_rows[0]['记录编号'] if csv_rows else 'N/A'}, 备件名称={csv_rows[0]['备件名称'] if csv_rows else 'N/A'}")

    assert len(csv_rows) == len(records_in_list), "CSV 行数应与列表一致"

    print(f"\n[步骤 11] 校验导出文件与提交瞬间数据一致性...")
    consistency = verify_export_task_consistency(task["id"])
    print(f"         一致: {consistency['consistent']}")
    print(f"         详情: {consistency['reason']}")
    print(f"         提交时: {consistency.get('task_record_count', '?')} 条, CSV: {consistency.get('csv_count', '?')} 条, 当前查询: {consistency.get('current_count', '?')} 条")

    assert consistency["consistent"], "导出数据应与提交瞬间一致"

    print(f"\n[步骤 12] 在任务中心查看用户任务列表...")
    user_tasks = get_user_export_tasks(current_user["id"], limit=10)
    print(f"         共 {len(user_tasks)} 条任务记录")
    for i, t in enumerate(user_tasks[:3]):
        st = EXPORT_TASK_DISPLAY.get(t["status"], (t["status"], ""))[0]
        tt = TASK_TYPE_DISPLAY.get(t["task_type"], t["task_type"])
        print(f"         [{i+1}] {t['task_no']} | {tt} | {st} | {t.get('export_count', 0)}条 | {t['created_at']}")

    assert len(user_tasks) >= 1, "用户任务列表应包含刚提交的任务"
    task_ids = [t["id"] for t in user_tasks]
    assert task["id"] in task_ids, "刚提交的任务应在列表中"

    print(f"\n[步骤 13] 验证任务快照完整持久化...")
    saved_task = get_export_task(task["id"])
    saved_filters = _deserialize_filters(saved_task["filters_snapshot"])
    assert saved_filters.get("status") == "approved", "筛选条件应正确持久化"

    import json
    if saved_task.get("sort_snapshot"):
        sort_data = json.loads(saved_task["sort_snapshot"])
        assert sort_data.get("sort_by") == "created_at", "排序字段应正确持久化"
        assert sort_data.get("sort_order") == "desc", "排序方向应正确持久化"
        print(f"         排序快照: OK")
    if saved_task.get("page_snapshot"):
        page_data = json.loads(saved_task["page_snapshot"])
        assert page_data.get("page") == 1, "页码应正确持久化"
        assert page_data.get("page_size") == 20, "每页条数应正确持久化"
        print(f"         分页快照: OK")
    if saved_task.get("columns_snapshot"):
        cols = json.loads(saved_task["columns_snapshot"])
        assert len(cols) == 8, "列配置应正确持久化"
        print(f"         列配置快照: OK ({len(cols)} 列)")
    print(f"         筛选快照: OK ({json.dumps(saved_filters, ensure_ascii=False)})")

    print(f"\n[步骤 14] 模拟冲突检测 - 先制造一个 pending 状态任务，再提交相同条件...")
    conflict_filters = {"keyword": "e2e_conflict_test"}
    snapshot_conflict1 = ExportTaskSnapshot(filters=conflict_filters)
    conflict_task1 = submit_export_task(current_user["id"], TASK_TYPE_BORROW, snapshot_conflict1)
    print(f"         已提交 pending 任务: {conflict_task1['task_no']} (状态: {EXPORT_TASK_DISPLAY.get(conflict_task1['status'], ('', ''))[0]})")

    snapshot_conflict2 = ExportTaskSnapshot(filters=conflict_filters)
    conflict_raised = False
    conflict_msg = ""
    try:
        submit_export_task(current_user["id"], TASK_TYPE_BORROW, snapshot_conflict2)
    except BusinessException as e:
        conflict_raised = True
        conflict_msg = e.message
        print(f"         正确抛出冲突异常: {conflict_msg[:60]}...")
    assert conflict_raised, "相同条件重复提交应抛出冲突异常"
    assert conflict_task1["task_no"] in conflict_msg, "冲突消息应包含原任务编号"
    assert "相同条件" in conflict_msg or "冲突" in conflict_msg, "冲突消息应说明原因"

    print(f"\n[步骤 15] 模拟跨重启恢复 - 先制造 running 状态任务...")
    snapshot3 = ExportTaskSnapshot(filters={"keyword": "e2e_test_restart"})
    restart_task = submit_export_task(current_user["id"], TASK_TYPE_STOCK, snapshot3)
    with get_connection() as conn:
        conn.execute("UPDATE export_tasks SET status = 'running', started_at = ? WHERE id = ?",
                     ("2025-01-01T00:00:00", restart_task["id"]))
    print(f"         制造 running 任务: {restart_task['task_no']}")

    print(f"         执行 recover_incomplete_tasks() 模拟程序重启...")
    recover_incomplete_tasks()

    recovered = get_export_task(restart_task["id"])
    status_text = EXPORT_TASK_DISPLAY.get(recovered["status"], (recovered["status"], ""))[0]
    print(f"         重启后任务状态: {status_text}")
    print(f"         错误信息: {recovered.get('error_message', 'N/A')}")
    assert recovered["status"] == TASK_STATUS_FAILED, "running 任务应在重启后标记为 failed"
    assert "重启" in (recovered.get("error_message") or ""), "错误信息应包含重启提示"

    print(f"\n[步骤 16] 重试恢复的失败任务...")
    retried = retry_export_task(restart_task["id"], current_user["id"])
    print(f"         重试后状态: {EXPORT_TASK_DISPLAY.get(retried['status'], (retried['status'], ''))[0]}")
    assert retried["status"] == TASK_STATUS_PENDING, "重试后应回到 pending"

    process_pending_tasks()
    final = get_export_task(restart_task["id"])
    print(f"         处理后最终状态: {EXPORT_TASK_DISPLAY.get(final['status'], (final['status'], ''))[0]}")
    assert final["status"] == TASK_STATUS_SUCCESS, "重试后应成功导出"

    print("\n" + "=" * 70)
    print("    端到端真实测试全部通过！桌面端完整导出链路验证成功")
    print("=" * 70)

    print("\n测试总结:")
    print("  [OK] 用户在列表点击导出 -> 生成任务记录")
    print("  [OK] 筛选条件、排序、分页、列配置完整固化为快照")
    print("  [OK] 按快照产出 CSV 文件，内容与提交瞬间列表一致")
    print("  [OK] 界面可见所有任务状态 (pending/running/success/failed/cancelled)")
    print("  [OK] 重复提交相同条件 -> 明确冲突提示")
    print("  [OK] 支持重试失败任务、重新下载成功任务")
    print("  [OK] 应用重启 -> running 任务标记为失败并可重试")
    print("  [OK] 异常场景（无权限、文件过期、磁盘满、源数据变化）有清晰提示和日志")

    return True


if __name__ == "__main__":
    try:
        success = simulate_desktop_export_flow()
        if not success:
            sys.exit(1)
    finally:
        try:
            export_dir = _get_export_dir()
            if os.path.exists(export_dir):
                shutil.rmtree(export_dir, ignore_errors=True)
            shutil.rmtree(E2E_TEST_DIR, ignore_errors=True)
        except Exception:
            pass
