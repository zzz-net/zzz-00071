import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from database import get_connection
from export_task_center import (
    ExportTaskSnapshot, submit_export_task, get_export_task,
    get_batch_snapshot, get_batch_tasks, get_user_batches,
    get_batch_aggregate_status, cancel_export_task, retry_export_task,
    confirm_pending_task, check_download_availability,
    verify_export_task_consistency, resubmit_as_new,
    _query_records_for_task, _compute_data_fingerprint,
    _get_export_dir, _check_disk_space, _check_write_permission,
    _log_task_operation,
    TASK_TYPE_BORROW, TASK_TYPE_STOCK, TASK_TYPE_STOCK_LOG,
    TASK_STATUS_PENDING, TASK_STATUS_RUNNING, TASK_STATUS_SUCCESS,
    TASK_STATUS_FAILED, TASK_STATUS_CANCELLED, TASK_STATUS_PENDING_CONFIRMATION,
    EXPORT_TASK_DISPLAY, TASK_TYPE_DISPLAY, FORMAT_DISPLAY,
    FORMAT_CSV, FORMAT_XLSX, EXPORT_FORMATS,
    FILE_EXPIRE_DAYS,
)
from services import BusinessException, _deserialize_filters

logger = logging.getLogger(__name__)

BATCH_STATUS_DISPLAY = {
    TASK_STATUS_PENDING: ("等待中", "#E6A23C"),
    TASK_STATUS_RUNNING: ("进行中", "#409EFF"),
    TASK_STATUS_SUCCESS: ("成功", "#67C23A"),
    TASK_STATUS_FAILED: ("失败", "#F56C6C"),
    TASK_STATUS_CANCELLED: ("已取消", "#909399"),
    TASK_STATUS_PENDING_CONFIRMATION: ("待确认", "#E6A23C"),
    "empty": ("空", "#909399"),
    "mixed": ("混合", "#909399"),
}


def create_export_batch(user_id, task_type, snapshot, formats=None, force=False):
    if not formats:
        formats = [snapshot.export_format]
    task = submit_export_task(user_id, task_type, snapshot, force=force, formats=formats)
    batch_no = task.get("batch_no")
    if not batch_no:
        return {"task": task, "batch": None, "batch_tasks": [task]}
    batch = get_batch_snapshot(batch_no)
    batch_task_list = get_batch_tasks(batch_no)
    return {"task": task, "batch": batch, "batch_tasks": batch_task_list}


def get_batch_detail(batch_no):
    batch = get_batch_snapshot(batch_no)
    if not batch:
        return None
    tasks = get_batch_tasks(batch_no)
    agg_status = get_batch_aggregate_status(batch_no)
    status_text, status_color = BATCH_STATUS_DISPLAY.get(agg_status, (agg_status, "#909399"))
    return {
        "batch": batch,
        "tasks": tasks,
        "aggregate_status": agg_status,
        "status_text": status_text,
        "status_color": status_color,
        "task_count": len(tasks),
        "success_count": sum(1 for t in tasks if t["status"] == TASK_STATUS_SUCCESS),
        "failed_count": sum(1 for t in tasks if t["status"] == TASK_STATUS_FAILED),
        "active_count": sum(1 for t in tasks if t["status"] in (TASK_STATUS_PENDING, TASK_STATUS_RUNNING)),
        "confirm_count": sum(1 for t in tasks if t["status"] == TASK_STATUS_PENDING_CONFIRMATION),
        "cancelled_count": sum(1 for t in tasks if t["status"] == TASK_STATUS_CANCELLED),
    }


def cancel_batch(batch_no, user_id):
    tasks = get_batch_tasks(batch_no)
    if not tasks:
        raise BusinessException("批次不存在或没有任务")
    cancelled = []
    for t in tasks:
        if t["status"] in (TASK_STATUS_PENDING, TASK_STATUS_RUNNING):
            try:
                cancel_export_task(t["id"], user_id)
                cancelled.append(t["id"])
            except BusinessException:
                pass
    return {"cancelled_count": len(cancelled), "cancelled_ids": cancelled}


def retry_batch(batch_no, user_id):
    tasks = get_batch_tasks(batch_no)
    if not tasks:
        raise BusinessException("批次不存在或没有任务")
    retried = []
    for t in tasks:
        if t["status"] in (TASK_STATUS_FAILED, TASK_STATUS_PENDING_CONFIRMATION):
            try:
                retry_export_task(t["id"], user_id)
                retried.append(t["id"])
            except BusinessException:
                pass
    return {"retried_count": len(retried), "retried_ids": retried}


def confirm_batch(batch_no, user_id):
    tasks = get_batch_tasks(batch_no)
    if not tasks:
        raise BusinessException("批次不存在或没有任务")
    confirmed = []
    for t in tasks:
        if t["status"] == TASK_STATUS_PENDING_CONFIRMATION:
            try:
                confirm_pending_task(t["id"], user_id)
                confirmed.append(t["id"])
            except BusinessException:
                pass
    return {"confirmed_count": len(confirmed), "confirmed_ids": confirmed}


def verify_batch_consistency(batch_no):
    tasks = get_batch_tasks(batch_no)
    if not tasks:
        return {"consistent": False, "reason": "批次不存在"}
    results = []
    all_consistent = True
    for t in tasks:
        if t["status"] == TASK_STATUS_SUCCESS:
            result = verify_export_task_consistency(t["id"])
            results.append({"task_id": t["id"], "task_no": t["task_no"],
                            "format": t.get("export_format", FORMAT_CSV), **result})
            if not result.get("consistent"):
                all_consistent = False
        else:
            results.append({"task_id": t["id"], "task_no": t["task_no"],
                            "format": t.get("export_format", FORMAT_CSV),
                            "consistent": None, "reason": f"任务状态为 {EXPORT_TASK_DISPLAY.get(t['status'], (t['status'], ''))[0]}，跳过校验"})
    return {"consistent": all_consistent, "results": results}


def check_batch_downloads(batch_no):
    tasks = get_batch_tasks(batch_no)
    if not tasks:
        return {"available": [], "unavailable": []}
    available = []
    unavailable = []
    for t in tasks:
        if t["status"] == TASK_STATUS_SUCCESS:
            avail = check_download_availability(t["id"])
            if avail.get("available"):
                available.append({"task_id": t["id"], "task_no": t["task_no"],
                                  "format": t.get("export_format", FORMAT_CSV),
                                  "file_path": avail["file_path"],
                                  "export_count": avail.get("export_count", 0)})
            else:
                unavailable.append({"task_id": t["id"], "task_no": t["task_no"],
                                    "format": t.get("export_format", FORMAT_CSV),
                                    "reason": avail.get("reason", "")})
        else:
            status_text = EXPORT_TASK_DISPLAY.get(t["status"], (t["status"], ""))[0]
            unavailable.append({"task_id": t["id"], "task_no": t["task_no"],
                                "format": t.get("export_format", FORMAT_CSV),
                                "reason": f"任务状态为 {status_text}"})
    return {"available": available, "unavailable": unavailable}


def cleanup_expired_batch_files():
    now = datetime.now()
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, export_file_path, expires_at FROM export_tasks
            WHERE status = 'success' AND expires_at IS NOT NULL
        """).fetchall()
    cleaned = 0
    for row in rows:
        try:
            expire_dt = datetime.fromisoformat(row["expires_at"])
            if now > expire_dt + timedelta(days=1):
                file_path = row["export_file_path"]
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        cleaned += 1
                    except Exception as e:
                        logger.warning(f"清理过期文件失败 {file_path}: {e}")
        except (ValueError, TypeError):
            continue
    if cleaned > 0:
        logger.info(f"已清理 {cleaned} 个过期导出文件")


def get_batch_operation_logs(batch_no, limit=200):
    tasks = get_batch_tasks(batch_no)
    if not tasks:
        return []
    task_ids = [str(t["id"]) for t in tasks]
    if not task_ids:
        return []
    with get_connection() as conn:
        placeholders = ",".join(["?"] * len(task_ids))
        rows = conn.execute(f"""
            SELECT ol.*, u.display_name AS operator_name
            FROM operation_logs ol
            JOIN users u ON ol.operator_id = u.id
            WHERE ol.target_type = 'export_task' AND ol.target_id IN ({placeholders})
            ORDER BY ol.created_at DESC LIMIT ?
        """, [int(tid) for tid in task_ids] + [limit]).fetchall()
        return [dict(row) for row in rows]


def get_user_batch_list_by_status(user_id, status_filter="all", limit=50):
    batches = get_user_batches(user_id, limit=limit)
    if status_filter == "all":
        return batches
    result = []
    for b in batches:
        agg = get_batch_aggregate_status(b["batch_no"])
        if status_filter == "active" and agg in (TASK_STATUS_PENDING, TASK_STATUS_RUNNING):
            result.append(b)
        elif status_filter == "success" and agg == TASK_STATUS_SUCCESS:
            result.append(b)
        elif status_filter == "failed" and agg == TASK_STATUS_FAILED:
            result.append(b)
        elif status_filter == "confirm" and agg == TASK_STATUS_PENDING_CONFIRMATION:
            result.append(b)
        elif status_filter == "cancelled" and agg == TASK_STATUS_CANCELLED:
            result.append(b)
        elif status_filter == "history" and agg in (TASK_STATUS_SUCCESS, TASK_STATUS_CANCELLED):
            result.append(b)
    return result
