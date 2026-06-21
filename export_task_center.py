import csv
import hashlib
import json
import logging
import os
import shutil
import threading
from datetime import datetime, timedelta
from database import get_connection, DB_PATH
from services import (
    get_borrow_records, get_all_parts, get_stock_logs,
    BusinessException, _serialize_filters, _deserialize_filters,
    STATUS_DISPLAY, OPERATION_DISPLAY
)

logger = logging.getLogger(__name__)

EXPORT_DIR_NAME = "exports"
FILE_EXPIRE_DAYS = 7
MAX_CONCURRENT_TASKS = 1

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

TASK_TYPE_BORROW = "borrow_records"
TASK_TYPE_STOCK = "stock_details"
TASK_TYPE_STOCK_LOG = "stock_logs"

EXPORT_TASK_DISPLAY = {
    TASK_STATUS_PENDING: ("等待中", "#E6A23C"),
    TASK_STATUS_RUNNING: ("导出中", "#409EFF"),
    TASK_STATUS_SUCCESS: ("已完成", "#67C23A"),
    TASK_STATUS_FAILED: ("失败", "#F56C6C"),
    TASK_STATUS_CANCELLED: ("已取消", "#909399"),
}

TASK_TYPE_DISPLAY = {
    TASK_TYPE_BORROW: "借还记录",
    TASK_TYPE_STOCK: "库存明细",
    TASK_TYPE_STOCK_LOG: "库存变动",
}


def _get_export_dir():
    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), EXPORT_DIR_NAME)
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


def _generate_task_no():
    now = datetime.now()
    return f"ET{now.strftime('%Y%m%d%H%M%S')}{now.microsecond // 1000:03d}"


def _compute_data_fingerprint(records):
    if not records:
        return hashlib.md5(b"empty").hexdigest()
    id_str = ",".join(str(r.get("id", r.get("record_no", ""))) for r in records)
    return hashlib.md5(id_str.encode("utf-8")).hexdigest()


def _check_disk_space(export_dir, estimated_bytes=0):
    try:
        usage = shutil.disk_usage(export_dir)
        if usage.free < max(estimated_bytes, 10 * 1024 * 1024):
            return False, f"磁盘空间不足，剩余 {usage.free // (1024*1024)} MB"
        return True, ""
    except Exception as e:
        return False, f"磁盘空间检查失败: {e}"


def _check_write_permission(export_dir):
    test_file = os.path.join(export_dir, ".permission_test")
    try:
        with open(test_file, "w") as f:
            f.write("test")
        os.remove(test_file)
        return True, ""
    except PermissionError:
        return False, f"导出目录无写入权限: {export_dir}"
    except Exception as e:
        return False, f"导出目录权限检查失败: {e}"


class ExportTaskSnapshot:
    def __init__(self, filters=None, sort_by=None, sort_order=None,
                 page=None, page_size=None, columns=None):
        self.filters = filters or {}
        self.sort_by = sort_by
        self.sort_order = sort_order
        self.page = page
        self.page_size = page_size
        self.columns = columns or []

    def to_dict(self):
        d = {}
        if self.filters:
            d["filters"] = self.filters
        if self.sort_by:
            d["sort_by"] = self.sort_by
        if self.sort_order:
            d["sort_order"] = self.sort_order
        if self.page is not None:
            d["page"] = self.page
        if self.page_size is not None:
            d["page_size"] = self.page_size
        if self.columns:
            d["columns"] = self.columns
        return d

    @classmethod
    def from_dict(cls, data):
        if not data:
            return cls()
        return cls(
            filters=data.get("filters", {}),
            sort_by=data.get("sort_by"),
            sort_order=data.get("sort_order"),
            page=data.get("page"),
            page_size=data.get("page_size"),
            columns=data.get("columns", []),
        )


class ConflictInfo:
    def __init__(self, conflict_task_id, conflict_task_no, conflict_status,
                 conflict_created_at, message):
        self.conflict_task_id = conflict_task_id
        self.conflict_task_no = conflict_task_no
        self.conflict_status = conflict_status
        self.conflict_created_at = conflict_created_at
        self.message = message

    def to_dict(self):
        return {
            "conflict_task_id": self.conflict_task_id,
            "conflict_task_no": self.conflict_task_no,
            "conflict_status": self.conflict_status,
            "conflict_created_at": self.conflict_created_at,
            "message": self.message,
        }


def check_conflict(user_id, task_type, filters_snapshot):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, task_no, status, created_at, filters_snapshot
            FROM export_tasks
            WHERE user_id = ? AND task_type = ? AND status IN ('pending', 'running')
            ORDER BY created_at DESC
        """, (user_id, task_type)).fetchall()

        if not rows:
            return None

        snapshot_dict = filters_snapshot if isinstance(filters_snapshot, dict) else json.loads(filters_snapshot)
        snapshot_key = json.dumps(snapshot_dict, sort_keys=True, ensure_ascii=False)

        for row in rows:
            row_dict = dict(row)
            try:
                existing = json.loads(row_dict["filters_snapshot"]) if row_dict["filters_snapshot"] else {}
                existing_key = json.dumps(existing, sort_keys=True, ensure_ascii=False)
                if existing_key == snapshot_key:
                    status_text = EXPORT_TASK_DISPLAY.get(row_dict["status"], (row_dict["status"], ""))[0]
                    return ConflictInfo(
                        conflict_task_id=row_dict["id"],
                        conflict_task_no=row_dict["task_no"],
                        conflict_status=row_dict["status"],
                        conflict_created_at=row_dict["created_at"],
                        message=f"存在相同条件的{status_text}任务 {row_dict['task_no']}（提交于 {row_dict['created_at']}），请先处理冲突"
                    )
            except (json.JSONDecodeError, TypeError):
                continue

        return None


def submit_export_task(user_id, task_type, snapshot, force=False):
    if not snapshot or not isinstance(snapshot, ExportTaskSnapshot):
        raise BusinessException("任务快照不能为空")

    filters_json = json.dumps(snapshot.filters, ensure_ascii=False)
    sort_json = json.dumps(snapshot.to_dict().get("sort_by") and {
        "sort_by": snapshot.sort_by, "sort_order": snapshot.sort_order
    }, ensure_ascii=False) if snapshot.sort_by else ""
    page_json = json.dumps({"page": snapshot.page, "page_size": snapshot.page_size},
                           ensure_ascii=False) if snapshot.page is not None else ""
    columns_json = json.dumps(snapshot.columns, ensure_ascii=False) if snapshot.columns else ""

    conflict = check_conflict(user_id, task_type, filters_json)
    if conflict and not force:
        raise BusinessException(conflict.message)

    records = _query_records_for_task(task_type, snapshot.filters)
    record_count = len(records)
    data_fingerprint = _compute_data_fingerprint(records)

    export_dir = _get_export_dir()
    ok, err = _check_write_permission(export_dir)
    if not ok:
        raise BusinessException(err)

    ok, err = _check_disk_space(export_dir, record_count * 500)
    if not ok:
        raise BusinessException(err)

    now = datetime.now().isoformat()
    expires_at = (datetime.now() + timedelta(days=FILE_EXPIRE_DAYS)).isoformat()
    task_no = _generate_task_no()

    conflict_task_id = conflict.conflict_task_id if conflict else None

    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT INTO export_tasks (
                task_no, user_id, task_type, status,
                filters_snapshot, sort_snapshot, page_snapshot, columns_snapshot,
                record_count, data_fingerprint, conflict_task_id,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_no, user_id, task_type, TASK_STATUS_PENDING,
            filters_json, sort_json, page_json, columns_json,
            record_count, data_fingerprint, conflict_task_id,
            now, expires_at
        ))
        task_id = cursor.lastrowid

        if conflict and conflict.conflict_task_id:
            conn.execute("""
                UPDATE export_tasks SET status = ?, completed_at = ? WHERE id = ? AND status IN ('pending', 'running')
            """, (TASK_STATUS_CANCELLED, now, conflict.conflict_task_id))

        _log_task_operation(conn, user_id, "submit_export_task", task_id,
                            f"提交导出任务 {task_no}, 类型={TASK_TYPE_DISPLAY.get(task_type, task_type)}, 预计{record_count}条")

    return get_export_task(task_id)


def _query_records_for_task(task_type, filters):
    if task_type == TASK_TYPE_BORROW:
        return get_borrow_records(**filters)
    elif task_type == TASK_TYPE_STOCK:
        keyword = filters.get("keyword")
        category = filters.get("category")
        return get_all_parts(keyword=keyword, category=category)
    elif task_type == TASK_TYPE_STOCK_LOG:
        part_id = filters.get("part_id")
        return get_stock_logs(part_id=part_id, limit=5000)
    return []


def _execute_export(task_id):
    task = get_export_task(task_id)
    if not task:
        return

    if task["status"] not in (TASK_STATUS_PENDING,):
        return

    with get_connection() as conn:
        now = datetime.now().isoformat()
        conn.execute("""
            UPDATE export_tasks SET status = ?, started_at = ? WHERE id = ? AND status = ?
        """, (TASK_STATUS_RUNNING, now, task_id, TASK_STATUS_PENDING))

    task = get_export_task(task_id)
    try:
        export_dir = _get_export_dir()
        ok, err = _check_write_permission(export_dir)
        if not ok:
            _mark_task_failed(task_id, task["user_id"], err)
            return

        ok, err = _check_disk_space(export_dir, task["record_count"] * 500)
        if not ok:
            _mark_task_failed(task_id, task["user_id"], err)
            return

        filters = _deserialize_filters(task["filters_snapshot"])
        current_records = _query_records_for_task(task["task_type"], filters)
        current_fingerprint = _compute_data_fingerprint(current_records)

        if current_fingerprint != (task["data_fingerprint"] or ""):
            _mark_task_failed(task_id, task["user_id"],
                              f"源数据已变化（提交时 {task['record_count']} 条，当前 {len(current_records)} 条），请重新提交任务")
            return

        task_type = task["task_type"]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{TASK_TYPE_DISPLAY.get(task_type, task_type)}_{timestamp}.csv"
        file_path = os.path.join(export_dir, filename)

        if task_type == TASK_TYPE_BORROW:
            export_count = _write_borrow_csv(file_path, current_records)
        elif task_type == TASK_TYPE_STOCK:
            export_count = _write_stock_csv(file_path, current_records)
        elif task_type == TASK_TYPE_STOCK_LOG:
            export_count = _write_stock_log_csv(file_path, current_records)
        else:
            _mark_task_failed(task_id, task["user_id"], f"不支持的任务类型: {task_type}")
            return

        now = datetime.now().isoformat()
        with get_connection() as conn:
            conn.execute("""
                UPDATE export_tasks SET status = ?, export_file_path = ?,
                    export_count = ?, completed_at = ? WHERE id = ?
            """, (TASK_STATUS_SUCCESS, file_path, export_count, now, task_id))
            _log_task_operation(conn, task["user_id"], "export_task_success", task_id,
                                f"导出完成 {filename}, {export_count}条")

    except PermissionError:
        _mark_task_failed(task_id, task["user_id"], "导出目录无写入权限")
    except OSError as e:
        if "No space left" in str(e) or "磁盘空间不足" in str(e):
            _mark_task_failed(task_id, task["user_id"], "磁盘空间不足，无法写入导出文件")
        else:
            _mark_task_failed(task_id, task["user_id"], f"文件写入失败: {e}")
    except Exception as e:
        _mark_task_failed(task_id, task["user_id"], f"导出失败: {e}")


def _mark_task_failed(task_id, user_id, error_message):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, error_message = ?, completed_at = ? WHERE id = ?
        """, (TASK_STATUS_FAILED, error_message, now, task_id))
        _log_task_operation(conn, user_id, "export_task_failed", task_id,
                            f"导出失败: {error_message}", success=False, error_message=error_message)


def _write_borrow_csv(file_path, records):
    fieldnames = [
        "记录编号", "备件编码", "备件名称", "借用数量", "单位", "单价(元)", "总金额(元)",
        "借用人", "用途", "状态", "审批人", "审批备注", "审批时间",
        "借出时间", "已归还数量", "归还时间", "归还备注", "创建时间"
    ]
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            status_text = STATUS_DISPLAY.get(r["status"], (r["status"], ""))[0]
            total_amount = r["quantity"] * r["unit_price"]
            writer.writerow({
                "记录编号": r["record_no"],
                "备件编码": r["part_code"],
                "备件名称": r["part_name"],
                "借用数量": r["quantity"],
                "单位": r["unit"],
                "单价(元)": r["unit_price"],
                "总金额(元)": total_amount,
                "借用人": r["borrower_name"],
                "用途": r.get("purpose", ""),
                "状态": status_text,
                "审批人": r.get("approver_name", "") or "",
                "审批备注": r.get("approval_remark", "") or "",
                "审批时间": r.get("approval_at", "") or "",
                "借出时间": r.get("borrow_at", "") or "",
                "已归还数量": r["return_quantity"],
                "归还时间": r.get("return_at", "") or "",
                "归还备注": r.get("return_remark", "") or "",
                "创建时间": r["created_at"],
            })
    return len(records)


def _write_stock_csv(file_path, parts):
    fieldnames = [
        "备件编码", "备件名称", "分类", "规格型号", "单位", "单价(元)",
        "是否需审批", "审批阈值(元)", "总库存", "可用库存",
        "待审批数量", "已借出数量", "库存状态"
    ]
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in parts:
            approval_flag = "是" if p["requires_approval"] else "否"
            if p["available_stock"] > 0:
                stock_status = f"可借 ({p['available_stock']})"
            elif p["pending_count"] > 0:
                stock_status = "待审批"
            elif p["borrowed_count"] > 0:
                stock_status = "已借空"
            else:
                stock_status = "无库存"
            writer.writerow({
                "备件编码": p["part_code"],
                "备件名称": p["part_name"],
                "分类": p["category"],
                "规格型号": p.get("specification", ""),
                "单位": p["unit"],
                "单价(元)": p["unit_price"],
                "是否需审批": approval_flag,
                "审批阈值(元)": p["approval_threshold"],
                "总库存": p["total_stock"],
                "可用库存": p["available_stock"],
                "待审批数量": p["pending_count"],
                "已借出数量": p["borrowed_count"],
                "库存状态": stock_status,
            })
    return len(parts)


def _write_stock_log_csv(file_path, logs):
    fieldnames = [
        "日志ID", "备件编码", "备件名称", "操作类型", "库存变动",
        "变动前可用", "变动后可用", "操作人", "备注", "操作时间"
    ]
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for log in logs:
            op_text = OPERATION_DISPLAY.get(log["operation_type"], (log["operation_type"], ""))[0]
            change = f"{log['quantity_change']:+d}"
            writer.writerow({
                "日志ID": log["id"],
                "备件编码": log["part_code"],
                "备件名称": log["part_name"],
                "操作类型": op_text,
                "库存变动": change,
                "变动前可用": log["before_available"],
                "变动后可用": log["after_available"],
                "操作人": log["operator_name"],
                "备注": log.get("remark", "") or "",
                "操作时间": log["created_at"],
            })
    return len(logs)


def get_export_task(task_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM export_tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None


def get_export_task_by_no(task_no):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM export_tasks WHERE task_no = ?", (task_no,)).fetchone()
        return dict(row) if row else None


def get_user_export_tasks(user_id, status=None, limit=50):
    with get_connection() as conn:
        sql = """
            SELECT et.*, u.display_name AS user_name
            FROM export_tasks et
            JOIN users u ON et.user_id = u.id
            WHERE et.user_id = ?
        """
        params = [user_id]
        if status:
            if isinstance(status, (list, tuple)):
                placeholders = ",".join(["?"] * len(status))
                sql += f" AND et.status IN ({placeholders})"
                params.extend(status)
            else:
                sql += " AND et.status = ?"
                params.append(status)
        sql += " ORDER BY et.created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_recent_export_tasks(limit=20):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT et.*, u.display_name AS user_name
            FROM export_tasks et
            JOIN users u ON et.user_id = u.id
            WHERE et.status = 'success'
            ORDER BY et.completed_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]


def cancel_export_task(task_id, user_id):
    task = get_export_task(task_id)
    if not task:
        raise BusinessException("任务不存在")
    if task["user_id"] != user_id:
        raise BusinessException("只能取消自己提交的任务")
    if task["status"] not in (TASK_STATUS_PENDING, TASK_STATUS_RUNNING):
        raise BusinessException(f"任务状态为 {EXPORT_TASK_DISPLAY.get(task['status'], (task['status'], ''))[0]}，无法取消")

    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, completed_at = ? WHERE id = ?
        """, (TASK_STATUS_CANCELLED, now, task_id))
        _log_task_operation(conn, user_id, "cancel_export_task", task_id,
                            f"取消导出任务 {task['task_no']}")

    return get_export_task(task_id)


def retry_export_task(task_id, user_id):
    task = get_export_task(task_id)
    if not task:
        raise BusinessException("任务不存在")
    if task["user_id"] != user_id:
        raise BusinessException("只能重试自己提交的任务")
    if task["status"] != TASK_STATUS_FAILED:
        raise BusinessException("只有失败的任务可以重试")

    filters = _deserialize_filters(task["filters_snapshot"])
    records = _query_records_for_task(task["task_type"], filters)
    record_count = len(records)
    new_fingerprint = _compute_data_fingerprint(records)

    now = datetime.now().isoformat()
    new_expires = (datetime.now() + timedelta(days=FILE_EXPIRE_DAYS)).isoformat()

    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, record_count = ?, data_fingerprint = ?,
                error_message = NULL, export_file_path = NULL, export_count = 0,
                started_at = NULL, completed_at = NULL, expires_at = ?,
                conflict_task_id = NULL
            WHERE id = ?
        """, (TASK_STATUS_PENDING, record_count, new_fingerprint, new_expires, task_id))
        _log_task_operation(conn, user_id, "retry_export_task", task_id,
                            f"重试导出任务 {task['task_no']}")

    return get_export_task(task_id)


def check_download_availability(task_id):
    task = get_export_task(task_id)
    if not task:
        return {"available": False, "reason": "任务不存在"}

    if task["status"] != TASK_STATUS_SUCCESS:
        status_text = EXPORT_TASK_DISPLAY.get(task["status"], (task["status"], ""))[0]
        return {"available": False, "reason": f"任务状态为 {status_text}，无法下载"}

    file_path = task.get("export_file_path")
    if not file_path:
        return {"available": False, "reason": "导出文件路径为空"}

    if not os.path.exists(file_path):
        expires_at = task.get("expires_at")
        if expires_at:
            try:
                expire_dt = datetime.fromisoformat(expires_at)
                if datetime.now() > expire_dt:
                    return {"available": False, "reason": "导出文件已过期，请重新提交任务"}
            except (ValueError, TypeError):
                pass
        return {"available": False, "reason": "导出文件已被删除或移动，请重新提交任务"}

    if not os.access(file_path, os.R_OK):
        return {"available": False, "reason": "导出文件无读取权限"}

    if task.get("expires_at"):
        try:
            expire_dt = datetime.fromisoformat(task["expires_at"])
            if datetime.now() > expire_dt:
                return {"available": False, "reason": "导出文件已过期，请重新提交任务"}
        except (ValueError, TypeError):
            pass

    return {"available": True, "file_path": file_path, "export_count": task.get("export_count", 0)}


def verify_export_task_consistency(task_id):
    task = get_export_task(task_id)
    if not task:
        return {"consistent": False, "reason": "任务不存在"}

    if task["status"] != TASK_STATUS_SUCCESS:
        return {"consistent": False, "reason": "任务未成功完成，无法校验"}

    file_path = task.get("export_file_path")
    if not file_path or not os.path.exists(file_path):
        return {"consistent": False, "reason": "导出文件不存在，无法校验"}

    filters = _deserialize_filters(task["filters_snapshot"])
    current_records = _query_records_for_task(task["task_type"], filters)

    csv_count = 0
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for _ in reader:
                csv_count += 1
    except Exception as e:
        return {"consistent": False, "reason": f"读取CSV失败: {e}"}

    if csv_count != len(current_records):
        return {
            "consistent": False,
            "reason": f"数量不一致: 提交时 {task['record_count']} 条, CSV {csv_count} 条, 当前查询 {len(current_records)} 条",
            "task_record_count": task["record_count"],
            "csv_count": csv_count,
            "current_count": len(current_records),
        }

    current_fingerprint = _compute_data_fingerprint(current_records)
    if current_fingerprint != (task.get("data_fingerprint") or ""):
        return {
            "consistent": False,
            "reason": f"源数据已变化: 提交时 {task['record_count']} 条, 当前 {len(current_records)} 条",
            "task_record_count": task["record_count"],
            "current_count": len(current_records),
        }

    return {
        "consistent": True,
        "reason": "数据完全一致",
        "task_record_count": task["record_count"],
        "csv_count": csv_count,
        "current_count": len(current_records),
    }


def process_pending_tasks():
    with get_connection() as conn:
        pending = conn.execute("""
            SELECT id FROM export_tasks WHERE status = 'pending' ORDER BY created_at ASC
        """).fetchall()

        running = conn.execute("""
            SELECT COUNT(*) as cnt FROM export_tasks WHERE status = 'running'
        """).fetchone()

        if running["cnt"] >= MAX_CONCURRENT_TASKS:
            return

    for row in pending:
        _execute_export(row["id"])


def cleanup_expired_files():
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


def recover_incomplete_tasks():
    with get_connection() as conn:
        running = conn.execute("""
            SELECT id, user_id, task_no FROM export_tasks WHERE status = 'running'
        """).fetchall()

        now = datetime.now().isoformat()
        for row in running:
            conn.execute("""
                UPDATE export_tasks SET status = ?, error_message = ?, completed_at = ?
                WHERE id = ?
            """, (TASK_STATUS_FAILED, "程序重启，任务中断，请重试", now, row["id"]))
            _log_task_operation(conn, row["user_id"], "recover_incomplete_task", row["id"],
                                f"程序重启恢复: 任务 {row['task_no']} 标记为失败")


def start_export_worker():
    def _worker():
        try:
            recover_incomplete_tasks()
        except Exception as e:
            logger.warning(f"恢复不完整任务失败: {e}")

        while True:
            try:
                process_pending_tasks()
                cleanup_expired_files()
            except Exception as e:
                logger.warning(f"导出工作线程异常: {e}")
            threading.Event().wait(2)

    t = threading.Thread(target=_worker, daemon=True, name="export-worker")
    t.start()
    return t


def _log_task_operation(conn, operator_id, action, target_id, detail,
                        success=True, error_message=None):
    try:
        conn.execute("""
            INSERT INTO operation_logs (operator_id, action, target_type, target_id,
                detail, success, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            operator_id, action, "export_task", target_id, detail,
            1 if success else 0, error_message, datetime.now().isoformat()
        ))
    except Exception as e:
        logger.warning(f"记录导出任务日志失败: {e}")
