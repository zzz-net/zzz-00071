import csv
import hashlib
import json
import logging
import os
import shutil
import threading
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape
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

FORMAT_CSV = "csv"
FORMAT_XLSX = "xlsx"
EXPORT_FORMATS = (FORMAT_CSV, FORMAT_XLSX)
FORMAT_DISPLAY = {FORMAT_CSV: "CSV", FORMAT_XLSX: "Excel"}

TASK_STATUS_PENDING = "pending"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCESS = "success"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"
TASK_STATUS_PENDING_CONFIRMATION = "pending_confirmation"

TASK_TYPE_BORROW = "borrow_records"
TASK_TYPE_STOCK = "stock_details"
TASK_TYPE_STOCK_LOG = "stock_logs"

EXPORT_TASK_DISPLAY = {
    TASK_STATUS_PENDING: ("等待中", "#E6A23C"),
    TASK_STATUS_RUNNING: ("导出中", "#409EFF"),
    TASK_STATUS_SUCCESS: ("已完成", "#67C23A"),
    TASK_STATUS_FAILED: ("失败", "#F56C6C"),
    TASK_STATUS_CANCELLED: ("已取消", "#909399"),
    TASK_STATUS_PENDING_CONFIRMATION: ("待确认", "#E6A23C"),
}

TASK_TYPE_DISPLAY = {
    TASK_TYPE_BORROW: "借还记录",
    TASK_TYPE_STOCK: "库存明细",
    TASK_TYPE_STOCK_LOG: "库存变动",
}

BORROW_COLUMN_MAP = {
    "record_no": ("记录编号", "record_no"),
    "part_code": ("备件编码", "part_code"),
    "part_name": ("备件名称", "part_name"),
    "quantity": ("借用数量", "quantity"),
    "unit": ("单位", "unit"),
    "unit_price": ("单价(元)", "unit_price"),
    "total_amount": ("总金额(元)", "_total"),
    "borrower": ("借用人", "borrower_name"),
    "borrower_name": ("借用人", "borrower_name"),
    "purpose": ("用途", "purpose"),
    "status": ("状态", "status"),
    "approver": ("审批人", "approver_name"),
    "approver_name": ("审批人", "approver_name"),
    "approval_remark": ("审批备注", "approval_remark"),
    "approval_at": ("审批时间", "approval_at"),
    "borrow_at": ("借出时间", "borrow_at"),
    "return_qty": ("已归还数量", "return_quantity"),
    "return_quantity": ("已归还数量", "return_quantity"),
    "return_at": ("归还时间", "return_at"),
    "return_remark": ("归还备注", "return_remark"),
    "created_at": ("创建时间", "created_at"),
}

STOCK_COLUMN_MAP = {
    "part_code": ("备件编码", "part_code"),
    "part_name": ("备件名称", "part_name"),
    "category": ("分类", "category"),
    "specification": ("规格型号", "specification"),
    "unit": ("单位", "unit"),
    "unit_price": ("单价(元)", "unit_price"),
    "approval": ("是否需审批", "requires_approval"),
    "requires_approval": ("是否需审批", "requires_approval"),
    "approval_threshold": ("审批阈值(元)", "approval_threshold"),
    "available": ("可用库存", "available_stock"),
    "available_stock": ("可用库存", "available_stock"),
    "pending": ("待审批数量", "pending_count"),
    "pending_count": ("待审批数量", "pending_count"),
    "borrowed": ("已借出数量", "borrowed_count"),
    "borrowed_count": ("已借出数量", "borrowed_count"),
    "total": ("总库存", "total_stock"),
    "total_stock": ("总库存", "total_stock"),
    "status": ("库存状态", "_status"),
}

STOCK_LOG_COLUMN_MAP = {
    "id": ("日志ID", "id"),
    "part_code": ("备件编码", "part_code"),
    "part_name": ("备件名称", "part_name"),
    "operation_type": ("操作类型", "operation_type"),
    "quantity_change": ("库存变动", "quantity_change"),
    "before_available": ("变动前可用", "before_available"),
    "after_available": ("变动后可用", "after_available"),
    "operator_name": ("操作人", "operator_name"),
    "remark": ("备注", "remark"),
    "created_at": ("操作时间", "created_at"),
}

BORROW_DEFAULT_COLUMNS = [
    "record_no", "part_code", "part_name", "quantity", "unit",
    "unit_price", "total_amount", "borrower", "purpose", "status",
    "approver", "approval_remark", "approval_at", "borrow_at",
    "return_qty", "return_at", "return_remark", "created_at"
]

STOCK_DEFAULT_COLUMNS = [
    "part_code", "part_name", "category", "specification", "unit",
    "unit_price", "approval", "approval_threshold", "total",
    "available", "pending", "borrowed", "status"
]

STOCK_LOG_DEFAULT_COLUMNS = [
    "id", "part_code", "part_name", "operation_type", "quantity_change",
    "before_available", "after_available", "operator_name", "remark", "created_at"
]


def _get_export_dir():
    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), EXPORT_DIR_NAME)
    os.makedirs(export_dir, exist_ok=True)
    return export_dir


def _generate_task_no():
    now = datetime.now()
    return f"ET{now.strftime('%Y%m%d%H%M%S')}{now.microsecond // 1000:03d}"


def _generate_batch_no():
    now = datetime.now()
    return f"EB{now.strftime('%Y%m%d%H%M%S')}{now.microsecond // 1000:03d}"


def _compute_data_fingerprint(records):
    if not records:
        return hashlib.md5(b"empty").hexdigest()
    content_parts = []
    for r in records:
        key = r.get("id", r.get("record_no", ""))
        val_str = json.dumps(r, sort_keys=True, ensure_ascii=False, default=str)
        content_parts.append(f"{key}:{val_str}")
    full_str = "|".join(content_parts)
    return hashlib.md5(full_str.encode("utf-8")).hexdigest()


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
                 page=None, page_size=None, columns=None,
                 export_format=FORMAT_CSV, export_current_page_only=False):
        self.filters = filters or {}
        self.sort_by = sort_by
        self.sort_order = sort_order
        self.page = page
        self.page_size = page_size
        self.columns = columns or []
        self.export_format = export_format if export_format in EXPORT_FORMATS else FORMAT_CSV
        self.export_current_page_only = bool(export_current_page_only)

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
        d["export_format"] = self.export_format
        d["export_current_page_only"] = self.export_current_page_only
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
            export_format=data.get("export_format", FORMAT_CSV),
            export_current_page_only=data.get("export_current_page_only", False),
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


def check_conflict(user_id, task_type, filters_snapshot, columns_snapshot=None, export_format=None):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT id, task_no, status, created_at, filters_snapshot, columns_snapshot, export_format
            FROM export_tasks
            WHERE user_id = ? AND task_type = ? AND status IN ('pending', 'running')
            ORDER BY created_at DESC
        """, (user_id, task_type)).fetchall()

        if not rows:
            return None

        snapshot_dict = filters_snapshot if isinstance(filters_snapshot, dict) else json.loads(filters_snapshot) if filters_snapshot else {}
        snapshot_key = json.dumps(snapshot_dict, sort_keys=True, ensure_ascii=False)
        cols_set = set(columns_snapshot) if columns_snapshot else None
        fmt_to_check = export_format

        for row in rows:
            row_dict = dict(row)
            try:
                existing_filters = json.loads(row_dict["filters_snapshot"]) if row_dict["filters_snapshot"] else {}
                existing_key = json.dumps(existing_filters, sort_keys=True, ensure_ascii=False)
                if existing_key != snapshot_key:
                    continue

                existing_cols = json.loads(row_dict["columns_snapshot"]) if row_dict.get("columns_snapshot") else []
                if cols_set is not None and set(existing_cols) != cols_set:
                    continue

                existing_fmt = row_dict.get("export_format", FORMAT_CSV)
                if fmt_to_check is not None and existing_fmt != fmt_to_check:
                    continue

                status_text = EXPORT_TASK_DISPLAY.get(row_dict["status"], (row_dict["status"], ""))[0]
                return ConflictInfo(
                    conflict_task_id=row_dict["id"],
                    conflict_task_no=row_dict["task_no"],
                    conflict_status=row_dict["status"],
                    conflict_created_at=row_dict["created_at"],
                    message=f"存在相同条件+列配置+格式的{status_text}任务 {row_dict['task_no']}（提交于 {row_dict['created_at']}），请先处理冲突"
                )
            except (json.JSONDecodeError, TypeError):
                continue

        return None


def submit_export_task(user_id, task_type, snapshot, force=False, formats=None):
    if not snapshot or not isinstance(snapshot, ExportTaskSnapshot):
        raise BusinessException("任务快照不能为空")

    primary_fmt = snapshot.export_format
    if primary_fmt not in EXPORT_FORMATS:
        raise BusinessException(f"不支持的导出格式: {primary_fmt}")

    fmt_list = formats if formats else [primary_fmt]
    for f in fmt_list:
        if f not in EXPORT_FORMATS:
            raise BusinessException(f"不支持的导出格式: {f}")

    filters_json = json.dumps(snapshot.filters, ensure_ascii=False)
    sort_json = json.dumps({"sort_by": snapshot.sort_by, "sort_order": snapshot.sort_order},
                           ensure_ascii=False) if snapshot.sort_by else ""
    page_json = json.dumps({
        "page": snapshot.page,
        "page_size": snapshot.page_size,
        "export_current_page_only": snapshot.export_current_page_only,
    }, ensure_ascii=False) if snapshot.page is not None else ""
    columns_json = json.dumps(snapshot.columns, ensure_ascii=False) if snapshot.columns else ""

    records = _query_records_for_task(task_type, snapshot.filters)
    record_count = len(records)
    data_fingerprint = _compute_data_fingerprint(records)

    export_dir = _get_export_dir()
    ok, err = _check_write_permission(export_dir)
    if not ok:
        raise BusinessException(err)

    est_bytes = record_count * 500 * len(fmt_list)
    ok, err = _check_disk_space(export_dir, est_bytes)
    if not ok:
        raise BusinessException(err)

    now = datetime.now().isoformat()
    expires_at = (datetime.now() + timedelta(days=FILE_EXPIRE_DAYS)).isoformat()
    batch_no = _generate_batch_no()
    frozen_data_json = json.dumps(records, ensure_ascii=False, default=str)

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO export_batch_snapshots (
                batch_no, user_id, task_type, filters_snapshot, sort_snapshot,
                page_snapshot, columns_snapshot, data_fingerprint, record_count,
                frozen_data_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_no, user_id, task_type, filters_json, sort_json,
            page_json, columns_json, data_fingerprint, record_count,
            frozen_data_json, now
        ))

        created_tasks = []
        for fmt in fmt_list:
            conflict = check_conflict(user_id, task_type, filters_json, snapshot.columns, fmt)
            if conflict and not force:
                raise BusinessException(conflict.message)

            task_no = _generate_task_no()
            conflict_task_id = conflict.conflict_task_id if conflict else None

            cursor = conn.execute("""
                INSERT INTO export_tasks (
                    task_no, batch_no, user_id, task_type, status, export_format,
                    filters_snapshot, sort_snapshot, page_snapshot, columns_snapshot,
                    record_count, data_fingerprint, conflict_task_id,
                    created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                task_no, batch_no, user_id, task_type, TASK_STATUS_PENDING, fmt,
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
                                f"提交导出任务 {task_no}, 批次={batch_no}, 类型={TASK_TYPE_DISPLAY.get(task_type, task_type)}, 格式={FORMAT_DISPLAY.get(fmt, fmt)}, 预计{record_count}条")
            created_tasks.append(task_id)

    return get_export_task(created_tasks[0])


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


def _apply_sort(records, sort_by, sort_order, column_map):
    if not sort_by or not records:
        return records
    mapped = column_map.get(sort_by, (None, sort_by))
    sort_key_field = mapped[1]
    if not sort_key_field:
        return records

    def _key(r):
        val = r.get(sort_key_field, "")
        if val is None:
            val = ""
        return val

    reverse = (sort_order == "desc")
    try:
        return sorted(records, key=_key, reverse=reverse)
    except Exception:
        return records


def _apply_page(records, page, page_size, current_page_only):
    if not current_page_only or page is None or page_size is None:
        return records
    if page <= 0 or page_size <= 0:
        return records
    start = (page - 1) * page_size
    end = start + page_size
    return records[start:end]


def _resolve_columns(col_keys, column_map, default_cols):
    resolved = []
    used_keys = set()
    if col_keys:
        for k in col_keys:
            if k in column_map:
                header, field = column_map[k]
                if k not in used_keys:
                    resolved.append((k, header, field))
                    used_keys.add(k)
    if not resolved:
        for k in default_cols:
            if k in column_map:
                header, field = column_map[k]
                resolved.append((k, header, field))
    return resolved


def _transform_borrow_row(r):
    total_amount = r.get("quantity", 0) * r.get("unit_price", 0)
    status_text = STATUS_DISPLAY.get(r.get("status", ""), (r.get("status", ""), ""))[0]
    transformed = dict(r)
    transformed["_total"] = total_amount
    transformed["status"] = status_text
    for k in list(transformed.keys()):
        if transformed[k] is None:
            transformed[k] = ""
    return transformed


def _transform_stock_row(p):
    approval_flag = "是" if p.get("requires_approval") else "否"
    available = p.get("available_stock", 0) or 0
    pending = p.get("pending_count", 0) or 0
    borrowed = p.get("borrowed_count", 0) or 0
    if available > 0:
        stock_status = f"可借 ({available})"
    elif pending > 0:
        stock_status = "待审批"
    elif borrowed > 0:
        stock_status = "已借空"
    else:
        stock_status = "无库存"
    transformed = dict(p)
    transformed["requires_approval"] = approval_flag
    transformed["_status"] = stock_status
    for k in list(transformed.keys()):
        if transformed[k] is None:
            transformed[k] = ""
    return transformed


def _transform_stock_log_row(log):
    op_text = OPERATION_DISPLAY.get(log.get("operation_type", ""), (log.get("operation_type", ""), ""))[0]
    change = f"{log.get('quantity_change', 0):+d}"
    transformed = dict(log)
    transformed["operation_type"] = op_text
    transformed["quantity_change"] = change
    for k in list(transformed.keys()):
        if transformed[k] is None:
            transformed[k] = ""
    return transformed


def _format_cell_value(val, field_key):
    if val is None:
        return ""
    if field_key in ("unit_price", "approval_threshold", "_total"):
        try:
            return f"{float(val):.2f}"
        except (ValueError, TypeError):
            return str(val)
    if field_key in ("quantity", "available_stock", "pending_count", "borrowed_count",
                     "total_stock", "return_quantity", "before_available", "after_available",
                     "id", "part_id", "record_id"):
        try:
            return str(int(val))
        except (ValueError, TypeError):
            return str(val)
    return str(val)


def _write_csv_file(file_path, headers, rows):
    with open(file_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(row)


def _write_xlsx_file(file_path, headers, rows):
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>'''

    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>'''

    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Sheet1" sheetId="1" r:id="rId1"/>
  </sheets>
</workbook>'''

    wb_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''

    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2">
    <font><sz val="11"/><name val="Calibri"/></font>
    <font><b/><sz val="11"/><name val="Calibri"/></font>
  </fonts>
  <fills count="1"><fill><patternFill patternType="none"/></fill></fills>
  <borders count="1"><border/></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0"/></cellStyleXfs>
  <cellXfs count="2">
    <xf numFmtId="0" fontId="0" applyFont="1"/>
    <xf numFmtId="0" fontId="1" applyFont="1"/>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''

    now_str = datetime.now().isoformat()
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>ExportTaskCenter</dc:creator>
  <cp:lastModifiedBy>ExportTaskCenter</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now_str}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now_str}</dcterms:modified>
</cp:coreProperties>'''

    def col_letter(idx):
        s = ""
        while idx > 0:
            idx, r = divmod(idx - 1, 26)
            s = chr(65 + r) + s
        return s

    def cell_ref(col, row):
        return f"{col_letter(col)}{row}"

    sheet_buf = BytesIO()
    sheet_buf.write(b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>''')

    def write_xml_row(row_idx, values, style=0):
        sheet_buf.write(f'<row r="{row_idx}">'.encode("utf-8"))
        for col_idx, v in enumerate(values, 1):
            ref = cell_ref(col_idx, row_idx)
            v_str = "" if v is None else str(v)
            escaped = escape(v_str)
            is_number = False
            if style == 0 and v_str != "":
                try:
                    float(v_str)
                    is_number = True
                except ValueError:
                    pass
            if is_number:
                sheet_buf.write(f'<c r="{ref}" s="{style}"><v>{escaped}</v></c>'.encode("utf-8"))
            else:
                sheet_buf.write(f'<c r="{ref}" t="inlineStr" s="{style}"><is><t>{escaped}</t></is></c>'.encode("utf-8"))
        sheet_buf.write(b'</row>')

    write_xml_row(1, headers, style=1)
    for i, row in enumerate(rows, 2):
        write_xml_row(i, row)

    sheet_buf.write(b'</sheetData></worksheet>')

    with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("docProps/core.xml", core)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/styles.xml", styles)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_buf.getvalue().decode("utf-8"))


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

        fmt = task.get("export_format", FORMAT_CSV)
        est_bytes = task["record_count"] * 500
        if fmt == FORMAT_XLSX:
            est_bytes = int(est_bytes * 1.3)
        ok, err = _check_disk_space(export_dir, est_bytes)
        if not ok:
            _mark_task_failed(task_id, task["user_id"], err)
            return

        filters = _deserialize_filters(task["filters_snapshot"])
        current_records = _query_records_for_task(task["task_type"], filters)
        current_fingerprint = _compute_data_fingerprint(current_records)

        if current_fingerprint != (task["data_fingerprint"] or ""):
            _mark_task_pending_confirmation(task_id, task["user_id"],
                              f"源数据已变化（提交时 {task['record_count']} 条，当前 {len(current_records)} 条），请确认后继续或重新提交")
            return

        sort_by = None
        sort_order = "asc"
        if task.get("sort_snapshot"):
            try:
                sort_data = json.loads(task["sort_snapshot"])
                sort_by = sort_data.get("sort_by")
                sort_order = sort_data.get("sort_order", "asc")
            except (json.JSONDecodeError, TypeError):
                pass

        page = None
        page_size = None
        current_page_only = False
        if task.get("page_snapshot"):
            try:
                page_data = json.loads(task["page_snapshot"])
                page = page_data.get("page")
                page_size = page_data.get("page_size")
                current_page_only = page_data.get("export_current_page_only", False)
            except (json.JSONDecodeError, TypeError):
                pass

        col_keys = []
        if task.get("columns_snapshot"):
            try:
                col_keys = json.loads(task["columns_snapshot"])
            except (json.JSONDecodeError, TypeError):
                pass

        task_type = task["task_type"]
        if task_type == TASK_TYPE_BORROW:
            col_map = BORROW_COLUMN_MAP
            default_cols = BORROW_DEFAULT_COLUMNS
            transformed = [_transform_borrow_row(r) for r in current_records]
        elif task_type == TASK_TYPE_STOCK:
            col_map = STOCK_COLUMN_MAP
            default_cols = STOCK_DEFAULT_COLUMNS
            transformed = [_transform_stock_row(r) for r in current_records]
        elif task_type == TASK_TYPE_STOCK_LOG:
            col_map = STOCK_LOG_COLUMN_MAP
            default_cols = STOCK_LOG_DEFAULT_COLUMNS
            transformed = [_transform_stock_log_row(r) for r in current_records]
        else:
            _mark_task_failed(task_id, task["user_id"], f"不支持的任务类型: {task_type}")
            return

        sorted_records = _apply_sort(transformed, sort_by, sort_order, col_map)
        paged_records = _apply_page(sorted_records, page, page_size, current_page_only)
        resolved_cols = _resolve_columns(col_keys, col_map, default_cols)

        headers = [h for _, h, _ in resolved_cols]
        output_rows = []
        for r in paged_records:
            row = []
            for _, _, field_key in resolved_cols:
                raw_val = r.get(field_key, "")
                row.append(_format_cell_value(raw_val, field_key))
            output_rows.append(row)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = "xlsx" if fmt == FORMAT_XLSX else "csv"
        filename = f"{TASK_TYPE_DISPLAY.get(task_type, task_type)}_{timestamp}.{ext}"
        file_path = os.path.join(export_dir, filename)

        if fmt == FORMAT_XLSX:
            _write_xlsx_file(file_path, headers, output_rows)
        else:
            _write_csv_file(file_path, headers, output_rows)

        export_count = len(output_rows)
        now = datetime.now().isoformat()
        with get_connection() as conn:
            conn.execute("""
                UPDATE export_tasks SET status = ?, export_file_path = ?,
                    export_count = ?, completed_at = ? WHERE id = ?
            """, (TASK_STATUS_SUCCESS, file_path, export_count, now, task_id))
            _log_task_operation(conn, task["user_id"], "export_task_success", task_id,
                                f"导出完成 {filename}, 格式={FORMAT_DISPLAY.get(fmt, fmt)}, {export_count}条")

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


def _mark_task_pending_confirmation(task_id, user_id, message):
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            UPDATE export_tasks SET status = ?, error_message = ? WHERE id = ?
        """, (TASK_STATUS_PENDING_CONFIRMATION, message, task_id))
        _log_task_operation(conn, user_id, "export_task_data_changed", task_id,
                            f"数据变化拦截: {message}", success=False, error_message=message)


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


def get_task_operation_logs(task_id, limit=100):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ol.*, u.display_name AS operator_name
            FROM operation_logs ol
            JOIN users u ON ol.operator_id = u.id
            WHERE ol.target_type = 'export_task' AND ol.target_id = ?
            ORDER BY ol.created_at DESC LIMIT ?
        """, (task_id, limit)).fetchall()
        return [dict(row) for row in rows]


def get_batch_snapshot(batch_no):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM export_batch_snapshots WHERE batch_no = ?", (batch_no,)).fetchone()
        return dict(row) if row else None

def get_batch_tasks(batch_no):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT et.*, u.display_name AS user_name
            FROM export_tasks et
            JOIN users u ON et.user_id = u.id
            WHERE et.batch_no = ?
            ORDER BY et.export_format ASC, et.created_at ASC
        """, (batch_no,)).fetchall()
        return [dict(row) for row in rows]

def get_user_batches(user_id, limit=50):
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT ebs.*,
                (SELECT COUNT(*) FROM export_tasks WHERE batch_no = ebs.batch_no) AS task_count,
                (SELECT COUNT(*) FROM export_tasks WHERE batch_no = ebs.batch_no AND status = 'success') AS success_count,
                (SELECT COUNT(*) FROM export_tasks WHERE batch_no = ebs.batch_no AND status IN ('pending', 'running')) AS active_count,
                (SELECT COUNT(*) FROM export_tasks WHERE batch_no = ebs.batch_no AND status = 'failed') AS failed_count,
                (SELECT COUNT(*) FROM export_tasks WHERE batch_no = ebs.batch_no AND status = 'pending_confirmation') AS confirm_count
            FROM export_batch_snapshots ebs
            WHERE ebs.user_id = ?
            ORDER BY ebs.created_at DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return [dict(row) for row in rows]

def get_batch_aggregate_status(batch_no):
    tasks = get_batch_tasks(batch_no)
    if not tasks:
        return "empty"
    statuses = {t["status"] for t in tasks}
    if statuses == {TASK_STATUS_SUCCESS}:
        return TASK_STATUS_SUCCESS
    if statuses <= {TASK_STATUS_CANCELLED}:
        return TASK_STATUS_CANCELLED
    if TASK_STATUS_PENDING_CONFIRMATION in statuses:
        return TASK_STATUS_PENDING_CONFIRMATION
    if TASK_STATUS_FAILED in statuses and not statuses.intersection({TASK_STATUS_PENDING, TASK_STATUS_RUNNING}):
        return TASK_STATUS_FAILED
    if statuses.intersection({TASK_STATUS_PENDING, TASK_STATUS_RUNNING}):
        return TASK_STATUS_RUNNING
    return "mixed"


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
    if task["status"] not in (TASK_STATUS_FAILED, TASK_STATUS_PENDING_CONFIRMATION):
        raise BusinessException("只有失败或待确认的任务可以重试")

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


def confirm_pending_task(task_id, user_id):
    task = get_export_task(task_id)
    if not task:
        raise BusinessException("任务不存在")
    if task["user_id"] != user_id:
        raise BusinessException("只能确认自己提交的任务")
    if task["status"] != TASK_STATUS_PENDING_CONFIRMATION:
        raise BusinessException("只有待确认的任务可以确认继续")

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
                started_at = NULL, completed_at = NULL, expires_at = ?
            WHERE id = ?
        """, (TASK_STATUS_PENDING, record_count, new_fingerprint, new_expires, task_id))
        _log_task_operation(conn, user_id, "confirm_pending_task", task_id,
                            f"确认继续导出任务 {task['task_no']}，更新指纹后重新排队")

    return get_export_task(task_id)


def resubmit_as_new(task_id, user_id):
    task = get_export_task(task_id)
    if not task:
        raise BusinessException("任务不存在")
    if task["user_id"] != user_id:
        raise BusinessException("只能基于自己的任务重新提交")

    filters = _deserialize_filters(task.get("filters_snapshot", "{}"))
    sort_by = None
    sort_order = "asc"
    if task.get("sort_snapshot"):
        try:
            sd = json.loads(task["sort_snapshot"])
            sort_by = sd.get("sort_by")
            sort_order = sd.get("sort_order", "asc")
        except Exception:
            pass
    page = None
    page_size = None
    current_page_only = False
    if task.get("page_snapshot"):
        try:
            pd = json.loads(task["page_snapshot"])
            page = pd.get("page")
            page_size = pd.get("page_size")
            current_page_only = pd.get("export_current_page_only", False)
        except Exception:
            pass
    columns = []
    if task.get("columns_snapshot"):
        try:
            columns = json.loads(task["columns_snapshot"])
        except Exception:
            pass
    fmt = task.get("export_format", FORMAT_CSV)

    snapshot = ExportTaskSnapshot(
        filters=filters, sort_by=sort_by, sort_order=sort_order,
        page=page, page_size=page_size, columns=columns,
        export_format=fmt, export_current_page_only=current_page_only,
    )
    return submit_export_task(user_id, task["task_type"], snapshot)


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

    return {
        "available": True,
        "file_path": file_path,
        "export_count": task.get("export_count", 0),
        "export_format": task.get("export_format", FORMAT_CSV),
    }


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

    fmt = task.get("export_format", FORMAT_CSV)
    file_count = 0
    try:
        if fmt == FORMAT_XLSX:
            with zipfile.ZipFile(file_path, "r") as zf:
                with zf.open("xl/worksheets/sheet1.xml") as f:
                    content = f.read().decode("utf-8")
                    file_count = content.count("<row r=") - 1
        else:
            with open(file_path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                next(reader, None)
                for _ in reader:
                    file_count += 1
    except Exception as e:
        return {"consistent": False, "reason": f"读取导出文件失败: {e}"}

    page = None
    page_size = None
    current_page_only = False
    if task.get("page_snapshot"):
        try:
            pd = json.loads(task["page_snapshot"])
            page = pd.get("page")
            page_size = pd.get("page_size")
            current_page_only = pd.get("export_current_page_only", False)
        except Exception:
            pass
    expected_count = len(current_records)
    if current_page_only and page and page_size:
        start = (page - 1) * page_size
        end = start + page_size
        expected_count = len(current_records[start:end])

    if file_count != expected_count:
        return {
            "consistent": False,
            "reason": f"数量不一致: 提交时 {task['record_count']} 条, 文件 {file_count} 条, 当前查询 {len(current_records)} 条",
            "task_record_count": task["record_count"],
            "file_count": file_count,
            "csv_count": file_count,
            "current_count": len(current_records),
        }

    current_fingerprint = _compute_data_fingerprint(current_records)
    if current_fingerprint != (task.get("data_fingerprint") or ""):
        return {
            "consistent": False,
            "reason": f"源数据已变化: 提交时 {task['record_count']} 条, 当前 {len(current_records)} 条",
            "task_record_count": task["record_count"],
            "csv_count": file_count,
            "current_count": len(current_records),
        }

    return {
        "consistent": True,
        "reason": "数据完全一致",
        "task_record_count": task["record_count"],
        "file_count": file_count,
        "csv_count": file_count,
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

        pending_confirm = conn.execute("""
            SELECT id, user_id, task_no FROM export_tasks WHERE status = 'pending_confirmation'
        """).fetchall()

        for row in pending_confirm:
            _log_task_operation(conn, row["user_id"], "recover_pending_confirmation", row["id"],
                                f"程序重启恢复: 任务 {row['task_no']} 保持待确认状态")


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
