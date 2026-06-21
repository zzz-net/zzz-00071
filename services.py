import sqlite3
import os
import json
from datetime import datetime
from database import get_connection, DB_PATH


class BusinessException(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def log_operation(conn, operator_id, action, target_type=None, target_id=None,
                 detail=None, success=True, error_message=None):
    conn.execute("""
        INSERT INTO operation_logs (operator_id, action, target_type, target_id,
            detail, success, error_message, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (operator_id, action, target_type, target_id, detail,
          1 if success else 0, error_message, datetime.now().isoformat()))


def log_failure_operation(operator_id, action, target_type=None, target_id=None,
                          detail=None, error_message=None):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT INTO operation_logs (operator_id, action, target_type, target_id,
                detail, success, error_message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (operator_id, action, target_type, target_id, detail,
              0, error_message, datetime.now().isoformat()))
        conn.commit()
    finally:
        conn.close()


def generate_record_no():
    now = datetime.now()
    return f"BR{now.strftime('%Y%m%d%H%M%S')}{now.microsecond // 1000:03d}"


def get_all_users():
    with get_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM users ORDER BY id").fetchall()]


def get_user_by_id(user_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_username(username):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_all_parts(keyword=None, category=None):
    with get_connection() as conn:
        sql = """
            SELECT sp.*,
                (SELECT COALESCE(SUM(br.quantity), 0) FROM borrow_records br
                    WHERE br.part_id = sp.id AND br.status = 'pending_approval') AS pending_count,
                (SELECT COALESCE(SUM(br.quantity - br.return_quantity), 0) FROM borrow_records br
                    WHERE br.part_id = sp.id AND br.status IN ('approved', 'borrowed')) AS borrowed_count
            FROM spare_parts sp
            WHERE sp.status = 'active'
        """
        params = []
        if keyword:
            sql += " AND (sp.part_code LIKE ? OR sp.part_name LIKE ? OR sp.specification LIKE ?)"
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])
        if category:
            sql += " AND sp.category = ?"
            params.append(category)
        sql += " ORDER BY sp.part_code"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_all_categories():
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT category FROM spare_parts WHERE status = 'active' ORDER BY category"
        ).fetchall()
        return [row["category"] for row in rows]


def get_part_by_id(part_id):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT sp.*,
                (SELECT COALESCE(SUM(br.quantity), 0) FROM borrow_records br
                    WHERE br.part_id = sp.id AND br.status = 'pending_approval') AS pending_count,
                (SELECT COALESCE(SUM(br.quantity - br.return_quantity), 0) FROM borrow_records br
                    WHERE br.part_id = sp.id AND br.status IN ('approved', 'borrowed')) AS borrowed_count
            FROM spare_parts sp
            WHERE sp.id = ?
        """, (part_id,)).fetchone()
        return dict(row) if row else None


def create_part(data, operator_id):
    with get_connection() as conn:
        now = datetime.now().isoformat()
        cursor = conn.execute("""
            INSERT INTO spare_parts (part_code, part_name, category, specification, unit,
                unit_price, requires_approval, approval_threshold, total_stock, available_stock,
                status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """, (
            data["part_code"], data["part_name"], data["category"],
            data.get("specification", ""), data.get("unit", "个"),
            data.get("unit_price", 0), data.get("requires_approval", 0),
            data.get("approval_threshold", 0),
            data.get("total_stock", 0), data.get("total_stock", 0),
            now, now
        ))
        part_id = cursor.lastrowid
        if data.get("total_stock", 0) > 0:
            conn.execute("""
                INSERT INTO stock_logs (part_id, operation_type, quantity_change,
                    before_available, after_available, operator_id, remark, created_at)
                VALUES (?, 'init', ?, 0, ?, ?, '新建备件初始化库存', ?)
            """, (part_id, data["total_stock"], data["total_stock"], operator_id, now))
        log_operation(conn, operator_id, "create_part", "spare_part", part_id,
                      f"创建备件: {data['part_code']} {data['part_name']}")
        return part_id


def update_part(part_id, data, operator_id):
    with get_connection() as conn:
        now = datetime.now().isoformat()
        conn.execute("""
            UPDATE spare_parts SET part_code=?, part_name=?, category=?, specification=?,
                unit=?, unit_price=?, requires_approval=?, approval_threshold=?, updated_at=?
            WHERE id=?
        """, (
            data["part_code"], data["part_name"], data["category"],
            data.get("specification", ""), data.get("unit", "个"),
            data.get("unit_price", 0), data.get("requires_approval", 0),
            data.get("approval_threshold", 0), now, part_id
        ))
        log_operation(conn, operator_id, "update_part", "spare_part", part_id,
                      f"更新备件信息: {data['part_code']}")


def adjust_stock(part_id, quantity_change, operator_id, remark=""):
    if quantity_change == 0:
        raise BusinessException("调整数量不能为0")
    with get_connection() as conn:
        part = conn.execute("SELECT * FROM spare_parts WHERE id = ?", (part_id,)).fetchone()
        if not part:
            raise BusinessException("备件不存在")
        before = part["available_stock"]
        after = before + quantity_change
        if after < 0:
            raise BusinessException("可用库存不能为负数")
        now = datetime.now().isoformat()
        conn.execute("""
            UPDATE spare_parts SET available_stock=?, total_stock=total_stock+?, updated_at=?
            WHERE id=?
        """, (after, quantity_change, now, part_id))
        conn.execute("""
            INSERT INTO stock_logs (part_id, operation_type, quantity_change,
                before_available, after_available, operator_id, remark, created_at)
            VALUES (?, 'adjust', ?, ?, ?, ?, ?, ?)
        """, (part_id, quantity_change, before, after, operator_id, remark or "库存调整", now))
        log_operation(conn, operator_id, "adjust_stock", "spare_part", part_id,
                      f"{part['part_code']} 库存调整: {before:+d} -> {after} ({quantity_change:+d})")


def delete_part(part_id, operator_id):
    with get_connection() as conn:
        now = datetime.now().isoformat()
        part = conn.execute("SELECT * FROM spare_parts WHERE id = ?", (part_id,)).fetchone()
        if not part:
            raise BusinessException("备件不存在")
        borrowed = conn.execute(
            "SELECT COUNT(*) FROM borrow_records WHERE part_id=? AND status IN ('pending_approval','approved','borrowed')",
            (part_id,)
        ).fetchone()[0]
        if borrowed > 0:
            raise BusinessException("该备件存在未完成的借还记录，无法停用")
        conn.execute("UPDATE spare_parts SET status='inactive', updated_at=? WHERE id=?", (now, part_id))
        log_operation(conn, operator_id, "delete_part", "spare_part", part_id,
                      f"停用备件: {part['part_code']}")


def submit_borrow(part_id, borrower_id, quantity, purpose=""):
    if quantity <= 0:
        raise BusinessException("借用数量必须大于0")
    with get_connection() as conn:
        part = conn.execute("SELECT * FROM spare_parts WHERE id = ?", (part_id,)).fetchone()
        if not part:
            raise BusinessException("备件不存在")
        if part["status"] != "active":
            raise BusinessException("该备件已停用")
        borrower = conn.execute("SELECT * FROM users WHERE id = ?", (borrower_id,)).fetchone()
        if not borrower:
            raise BusinessException("借用人不存在")
        if quantity > part["available_stock"]:
            log_failure_operation(borrower_id, "submit_borrow", "spare_part", part_id,
                                  f"尝试借用 {part['part_code']} x{quantity}",
                                  error_message=f"库存不足: 可用{part['available_stock']}, 请求{quantity}")
            raise BusinessException(
                f"借出数量超过可用库存，当前可用库存: {part['available_stock']}{part['unit']}"
            )
        total_amount = part["unit_price"] * quantity
        needs_approval = bool(part["requires_approval"]) or (part["approval_threshold"] > 0 and total_amount >= part["approval_threshold"])
        if needs_approval and borrower["role"] != "supervisor":
            status = "pending_approval"
            borrow_at = None
        else:
            status = "approved"
            borrow_at = datetime.now().isoformat()
        now = datetime.now().isoformat()
        record_no = generate_record_no()
        cursor = conn.execute("""
            INSERT INTO borrow_records (record_no, part_id, borrower_id, quantity, purpose,
                status, approver_id, approval_at, borrow_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (record_no, part_id, borrower_id, quantity, purpose, status,
              None, None, borrow_at, now, now))
        record_id = cursor.lastrowid
        if status == "approved":
            before = part["available_stock"]
            after = before - quantity
            conn.execute("""
                UPDATE spare_parts SET available_stock=?, updated_at=? WHERE id=?
            """, (after, now, part_id))
            conn.execute("""
                INSERT INTO stock_logs (part_id, record_id, operation_type, quantity_change,
                    before_available, after_available, operator_id, remark, created_at)
                VALUES (?, ?, 'borrow_approve', -?, ?, ?, ?, '自动审批通过借出', ?)
            """, (part_id, record_id, quantity, before, after, borrower_id, now))
        log_operation(conn, borrower_id, "submit_borrow", "borrow_record", record_id,
                      f"提交借用 {part['part_code']} x{quantity}, 状态: {status}")
        return record_id


def approve_borrow(record_id, approver_id, remark=""):
    with get_connection() as conn:
        record = conn.execute("SELECT * FROM borrow_records WHERE id = ?", (record_id,)).fetchone()
        if not record:
            raise BusinessException("借用记录不存在")
        if record["status"] != "pending_approval":
            log_failure_operation(approver_id, "approve_borrow", "borrow_record", record_id,
                                  f"审批 {record['record_no']}",
                                  error_message=f"记录状态为{record['status']}, 非待审批")
            raise BusinessException(f"该记录状态为 {record['status']}，无法审批")
        approver = conn.execute("SELECT * FROM users WHERE id = ?", (approver_id,)).fetchone()
        if approver["role"] != "supervisor":
            log_failure_operation(approver_id, "approve_borrow", "borrow_record", record_id,
                                  f"审批 {record['record_no']}",
                                  error_message="非主管用户无权审批")
            raise BusinessException("只有主管用户可以审批")
        part = conn.execute("SELECT * FROM spare_parts WHERE id = ?", (record["part_id"],)).fetchone()
        if record["quantity"] > part["available_stock"]:
            log_failure_operation(approver_id, "approve_borrow", "borrow_record", record_id,
                                  f"审批 {record['record_no']}",
                                  error_message=f"库存不足: 可用{part['available_stock']}, 请求{record['quantity']}")
            raise BusinessException(
                f"可用库存不足，当前可用: {part['available_stock']}{part['unit']}"
            )
        now = datetime.now().isoformat()
        before = part["available_stock"]
        after = before - record["quantity"]
        conn.execute("""
            UPDATE spare_parts SET available_stock=?, updated_at=? WHERE id=?
        """, (after, now, record["part_id"]))
        conn.execute("""
            UPDATE borrow_records SET status='approved', approver_id=?, approval_remark=?,
                approval_at=?, borrow_at=?, updated_at=? WHERE id=?
        """, (approver_id, remark, now, now, now, record_id))
        conn.execute("""
            INSERT INTO stock_logs (part_id, record_id, operation_type, quantity_change,
                before_available, after_available, operator_id, remark, created_at)
            VALUES (?, ?, 'borrow_approve', -?, ?, ?, ?, ?, ?)
        """, (record["part_id"], record_id, record["quantity"], before, after,
              approver_id, remark or "审批通过借出", now))
        log_operation(conn, approver_id, "approve_borrow", "borrow_record", record_id,
                      f"审批通过 {record['record_no']} x{record['quantity']}")


def reject_borrow(record_id, approver_id, remark=""):
    with get_connection() as conn:
        record = conn.execute("SELECT * FROM borrow_records WHERE id = ?", (record_id,)).fetchone()
        if not record:
            raise BusinessException("借用记录不存在")
        if record["status"] != "pending_approval":
            log_failure_operation(approver_id, "reject_borrow", "borrow_record", record_id,
                                  f"驳回 {record['record_no']}",
                                  error_message=f"记录状态为{record['status']}, 非待审批")
            raise BusinessException(f"该记录状态为 {record['status']}，无法驳回")
        approver = conn.execute("SELECT * FROM users WHERE id = ?", (approver_id,)).fetchone()
        if approver["role"] != "supervisor":
            log_failure_operation(approver_id, "reject_borrow", "borrow_record", record_id,
                                  f"驳回 {record['record_no']}",
                                  error_message="非主管用户无权驳回")
            raise BusinessException("只有主管用户可以驳回")
        now = datetime.now().isoformat()
        conn.execute("""
            UPDATE borrow_records SET status='rejected', approver_id=?, approval_remark=?,
                approval_at=?, updated_at=? WHERE id=?
        """, (approver_id, remark, now, now, record_id))
        log_operation(conn, approver_id, "reject_borrow", "borrow_record", record_id,
                      f"审批驳回 {record['record_no']}")


def return_part(record_id, operator_id, return_quantity=None, remark=""):
    with get_connection() as conn:
        record = conn.execute("SELECT * FROM borrow_records WHERE id = ?", (record_id,)).fetchone()
        if not record:
            raise BusinessException("借用记录不存在")
        if record["status"] not in ("approved", "borrowed"):
            log_failure_operation(operator_id, "return_part", "borrow_record", record_id,
                                  f"归还 {record['record_no']}",
                                  error_message=f"记录状态为{record['status']}, 非已借出状态")
            raise BusinessException(
                f"该记录状态为 {record['status']}，无法执行归还操作"
            )
        part = conn.execute("SELECT * FROM spare_parts WHERE id = ?", (record["part_id"],)).fetchone()
        if return_quantity is None:
            return_quantity = record["quantity"] - record["return_quantity"]
        if return_quantity <= 0:
            log_failure_operation(operator_id, "return_part", "borrow_record", record_id,
                                  f"归还 {record['record_no']}",
                                  error_message=f"归还数量无效: {return_quantity}")
            raise BusinessException("归还数量必须大于0")
        remaining = record["quantity"] - record["return_quantity"]
        if return_quantity > remaining:
            log_failure_operation(operator_id, "return_part", "borrow_record", record_id,
                                  f"归还 {record['record_no']}",
                                  error_message=f"归还数量超过未归还数量: 未归还{remaining}, 归还{return_quantity}")
            raise BusinessException(
                f"归还数量超过未归还数量，当前未归还: {remaining}{part['unit']}"
            )
        now = datetime.now().isoformat()
        before = part["available_stock"]
        after = before + return_quantity
        new_return_quantity = record["return_quantity"] + return_quantity
        new_status = "returned" if new_return_quantity >= record["quantity"] else "borrowed"
        conn.execute("""
            UPDATE spare_parts SET available_stock=?, updated_at=? WHERE id=?
        """, (after, now, record["part_id"]))
        conn.execute("""
            UPDATE borrow_records SET return_quantity=?, return_at=?, return_remark=?,
                status=?, updated_at=? WHERE id=?
        """, (new_return_quantity, now, remark, new_status, now, record_id))
        conn.execute("""
            INSERT INTO stock_logs (part_id, record_id, operation_type, quantity_change,
                before_available, after_available, operator_id, remark, created_at)
            VALUES (?, ?, 'return', ?, ?, ?, ?, ?, ?)
        """, (record["part_id"], record_id, return_quantity, before, after,
              operator_id, remark or "归还入库", now))
        log_operation(conn, operator_id, "return_part", "borrow_record", record_id,
                      f"归还 {record['record_no']} x{return_quantity}, 剩余未还: {record['quantity'] - new_return_quantity}")


def undo_return(record_id, operator_id, remark=""):
    with get_connection() as conn:
        record = conn.execute("SELECT * FROM borrow_records WHERE id = ?", (record_id,)).fetchone()
        if not record:
            raise BusinessException("借用记录不存在")
        if record["status"] != "returned":
            log_failure_operation(operator_id, "undo_return", "borrow_record", record_id,
                                  f"撤销归还 {record['record_no']}",
                                  error_message=f"记录状态为{record['status']}, 非已归还状态")
            raise BusinessException(
                f"该记录状态为 {record['status']}，无法执行撤销归还操作"
            )
        part = conn.execute("SELECT * FROM spare_parts WHERE id = ?", (record["part_id"],)).fetchone()
        undo_quantity = record["return_quantity"]
        if undo_quantity <= 0:
            log_failure_operation(operator_id, "undo_return", "borrow_record", record_id,
                                  f"撤销归还 {record['record_no']}",
                                  error_message=f"可撤销数量无效: {undo_quantity}")
            raise BusinessException("无可撤销的归还数量")
        if undo_quantity > part["available_stock"]:
            log_failure_operation(operator_id, "undo_return", "borrow_record", record_id,
                                  f"撤销归还 {record['record_no']}",
                                  error_message=f"库存不足: 可用{part['available_stock']}, 需撤销{undo_quantity}")
            raise BusinessException(
                f"可用库存不足，无法撤销归还，当前可用: {part['available_stock']}{part['unit']}"
            )
        now = datetime.now().isoformat()
        before = part["available_stock"]
        after = before - undo_quantity
        conn.execute("""
            UPDATE spare_parts SET available_stock=?, updated_at=? WHERE id=?
        """, (after, now, record["part_id"]))
        conn.execute("""
            UPDATE borrow_records SET return_quantity=0, return_at=NULL, return_remark=NULL,
                status='borrowed', updated_at=? WHERE id=?
        """, (now, record_id))
        conn.execute("""
            INSERT INTO stock_logs (part_id, record_id, operation_type, quantity_change,
                before_available, after_available, operator_id, remark, created_at)
            VALUES (?, ?, 'return', ?, ?, ?, ?, ?, ?)
        """, (record["part_id"], record_id, -undo_quantity, before, after,
              operator_id, remark or "撤销归还", now))
        log_operation(conn, operator_id, "undo_return", "borrow_record", record_id,
                      f"撤销归还 {record['record_no']} x{undo_quantity}, 恢复为已借出状态")


def rollback_borrow(record_id, operator_id, remark=""):
    with get_connection() as conn:
        record = conn.execute("SELECT * FROM borrow_records WHERE id = ?", (record_id,)).fetchone()
        if not record:
            raise BusinessException("借用记录不存在")
        if record["status"] != "approved" and record["status"] != "borrowed":
            log_failure_operation(operator_id, "rollback_borrow", "borrow_record", record_id,
                                  f"回滚 {record['record_no']}",
                                  error_message=f"记录状态为{record['status']}, 无法回滚")
            raise BusinessException(f"该记录状态为 {record['status']}，无法回滚")
        if record["return_quantity"] > 0:
            log_failure_operation(operator_id, "rollback_borrow", "borrow_record", record_id,
                                  f"回滚 {record['record_no']}",
                                  error_message="存在部分归还记录, 无法回滚")
            raise BusinessException("该记录存在部分归还，无法整体回滚")
        part = conn.execute("SELECT * FROM spare_parts WHERE id = ?", (record["part_id"],)).fetchone()
        now = datetime.now().isoformat()
        before = part["available_stock"]
        after = before + record["quantity"]
        conn.execute("""
            UPDATE spare_parts SET available_stock=?, updated_at=? WHERE id=?
        """, (after, now, record["part_id"]))
        conn.execute("""
            UPDATE borrow_records SET status='rollback', updated_at=? WHERE id=?
        """, (now, record_id))
        conn.execute("""
            INSERT INTO stock_logs (part_id, record_id, operation_type, quantity_change,
                before_available, after_available, operator_id, remark, created_at)
            VALUES (?, ?, 'rollback', ?, ?, ?, ?, ?, ?)
        """, (record["part_id"], record_id, record["quantity"], before, after,
              operator_id, remark or "异常回滚", now))
        log_operation(conn, operator_id, "rollback_borrow", "borrow_record", record_id,
                      f"异常回滚 {record['record_no']} x{record['quantity']}")


def cancel_borrow(record_id, operator_id):
    with get_connection() as conn:
        record = conn.execute("SELECT * FROM borrow_records WHERE id = ?", (record_id,)).fetchone()
        if not record:
            raise BusinessException("借用记录不存在")
        if record["status"] != "pending_approval":
            log_failure_operation(operator_id, "cancel_borrow", "borrow_record", record_id,
                                  f"撤销 {record['record_no']}",
                                  error_message=f"记录状态为{record['status']}, 无法撤销")
            raise BusinessException(f"该记录状态为 {record['status']}，无法撤销")
        now = datetime.now().isoformat()
        conn.execute("""
            UPDATE borrow_records SET status='cancelled', updated_at=? WHERE id=?
        """, (now, record_id))
        log_operation(conn, operator_id, "cancel_borrow", "borrow_record", record_id,
                      f"撤销申请 {record['record_no']}")


def get_borrow_records(status=None, part_id=None, borrower_id=None, keyword=None,
                       date_from=None, date_to=None):
    with get_connection() as conn:
        sql = """
            SELECT br.*, sp.part_code, sp.part_name, sp.unit, sp.unit_price,
                ub.display_name AS borrower_name, ua.display_name AS approver_name
            FROM borrow_records br
            JOIN spare_parts sp ON br.part_id = sp.id
            JOIN users ub ON br.borrower_id = ub.id
            LEFT JOIN users ua ON br.approver_id = ua.id
            WHERE 1=1
        """
        params = []
        if status:
            if isinstance(status, (list, tuple)):
                placeholders = ",".join(["?"] * len(status))
                sql += f" AND br.status IN ({placeholders})"
                params.extend(status)
            else:
                sql += " AND br.status = ?"
                params.append(status)
        if part_id:
            sql += " AND br.part_id = ?"
            params.append(part_id)
        if borrower_id:
            sql += " AND br.borrower_id = ?"
            params.append(borrower_id)
        if keyword:
            sql += " AND (br.record_no LIKE ? OR sp.part_code LIKE ? OR sp.part_name LIKE ?)"
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw])
        if date_from:
            sql += " AND br.created_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND br.created_at <= ?"
            params.append(date_to)
        sql += " ORDER BY br.created_at DESC"
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_borrow_record(record_id):
    with get_connection() as conn:
        row = conn.execute("""
            SELECT br.*, sp.part_code, sp.part_name, sp.unit, sp.unit_price,
                ub.display_name AS borrower_name, ua.display_name AS approver_name
            FROM borrow_records br
            JOIN spare_parts sp ON br.part_id = sp.id
            JOIN users ub ON br.borrower_id = ub.id
            LEFT JOIN users ua ON br.approver_id = ua.id
            WHERE br.id = ?
        """, (record_id,)).fetchone()
        return dict(row) if row else None


def get_stock_logs(part_id=None, record_id=None, limit=100):
    with get_connection() as conn:
        sql = """
            SELECT sl.*, sp.part_code, sp.part_name, u.display_name AS operator_name
            FROM stock_logs sl
            JOIN spare_parts sp ON sl.part_id = sp.id
            JOIN users u ON sl.operator_id = u.id
            WHERE 1=1
        """
        params = []
        if part_id:
            sql += " AND sl.part_id = ?"
            params.append(part_id)
        if record_id:
            sql += " AND sl.record_id = ?"
            params.append(record_id)
        sql += " ORDER BY sl.created_at DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def get_operation_logs(limit=200):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ol.*, u.display_name AS operator_name
            FROM operation_logs ol
            JOIN users u ON ol.operator_id = u.id
            ORDER BY ol.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]


STATUS_DISPLAY = {
    "pending_approval": ("待审批", "#E6A23C"),
    "approved": ("已借出", "#409EFF"),
    "rejected": ("已驳回", "#F56C6C"),
    "borrowed": ("已借出", "#409EFF"),
    "returned": ("已归还", "#67C23A"),
    "rollback": ("已回滚", "#909399"),
    "cancelled": ("已撤销", "#909399"),
}

OPERATION_DISPLAY = {
    "init": ("初始化", "#409EFF"),
    "stock_in": ("入库", "#67C23A"),
    "borrow_approve": ("借出", "#E6A23C"),
    "borrow_reject": ("驳回", "#F56C6C"),
    "return": ("归还", "#67C23A"),
    "rollback": ("回滚", "#909399"),
    "cancel": ("撤销", "#909399"),
    "adjust": ("调整", "#F56C6C"),
}


def _serialize_filters(filters):
    cleaned = {}
    for k, v in filters.items():
        if v is not None and v != "" and v != [] and v != ():
            cleaned[k] = v
    return json.dumps(cleaned, ensure_ascii=False)


def _deserialize_filters(filters_json):
    if not filters_json:
        return {}
    try:
        return json.loads(filters_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _is_filter_empty(filters):
    for v in filters.values():
        if v is not None and v != "" and v != [] and v != ():
            return False
    return True


def save_filter_scheme(name, owner_id, filters, scope="personal", scheme_id=None):
    if not name or not name.strip():
        raise BusinessException("方案名称不能为空")
    name = name.strip()
    if _is_filter_empty(filters):
        raise BusinessException("筛选条件不能全部为空")
    filters_json = _serialize_filters(filters)
    now = datetime.now().isoformat()
    with get_connection() as conn:
        if scheme_id:
            existing = conn.execute("SELECT id, owner_id FROM filter_schemes WHERE id = ?",
                                    (scheme_id,)).fetchone()
            if not existing:
                raise BusinessException("方案不存在")
            conn.execute("""
                UPDATE filter_schemes SET name=?, scope=?, filters=?, updated_at=? WHERE id=?
            """, (name, scope, filters_json, now, scheme_id))
            log_operation(conn, owner_id, "update_filter_scheme", "filter_scheme", scheme_id,
                          f"更新筛选方案: {name}")
            return scheme_id
        existing = conn.execute(
            "SELECT id FROM filter_schemes WHERE name = ? AND (owner_id = ? OR scope = 'shared')",
            (name, owner_id)
        ).fetchone()
        if existing:
            raise BusinessException(f"同名方案已存在: {name}")
        cursor = conn.execute("""
            INSERT INTO filter_schemes (name, owner_id, scope, filters, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, owner_id, scope, filters_json, now, now))
        scheme_id = cursor.lastrowid
        log_operation(conn, owner_id, "save_filter_scheme", "filter_scheme", scheme_id,
                      f"保存筛选方案: {name}")
        return scheme_id


def get_filter_schemes(user_id, role):
    with get_connection() as conn:
        if role == "supervisor":
            rows = conn.execute("""
                SELECT * FROM filter_schemes
                WHERE owner_id = ? OR scope = 'shared'
                ORDER BY scope DESC, name ASC
            """, (user_id,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM filter_schemes
                WHERE owner_id = ? OR scope = 'shared'
                ORDER BY scope DESC, name ASC
            """, (user_id,)).fetchall()
        return [dict(row) for row in rows]


def delete_filter_scheme(scheme_id, user_id, role):
    with get_connection() as conn:
        scheme = conn.execute("SELECT * FROM filter_schemes WHERE id = ?", (scheme_id,)).fetchone()
        if not scheme:
            raise BusinessException("方案不存在")
        if scheme["owner_id"] != user_id and role != "supervisor":
            raise BusinessException("只能删除自己创建的方案")
        conn.execute("DELETE FROM filter_schemes WHERE id = ?", (scheme_id,))
        log_operation(conn, user_id, "delete_filter_scheme", "filter_scheme", scheme_id,
                      f"删除筛选方案: {scheme['name']}")


def get_filter_scheme_by_id(scheme_id):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM filter_schemes WHERE id = ?", (scheme_id,)).fetchone()
        if row:
            result = dict(row)
            result["filters"] = _deserialize_filters(result["filters"])
            return result
        return None
