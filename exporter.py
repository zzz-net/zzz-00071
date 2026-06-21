import csv
import os
from datetime import datetime
from services import get_all_parts, get_borrow_records, get_stock_logs, STATUS_DISPLAY, OPERATION_DISPLAY


def export_stock_details(file_path):
    parts = get_all_parts()
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


def export_borrow_records(file_path, status=None, borrower_id=None, keyword=None,
                          date_from=None, date_to=None):
    records = get_borrow_records(status=status, borrower_id=borrower_id, keyword=keyword,
                                 date_from=date_from, date_to=date_to)
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


def export_stock_logs(file_path, part_id=None):
    logs = get_stock_logs(part_id=part_id, limit=5000)
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


def generate_default_filename(prefix):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.csv"
