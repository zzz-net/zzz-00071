import json
from datetime import datetime
from database import get_connection
from services import (
    get_filter_scheme_by_id, get_filter_schemes, get_borrow_records,
    _deserialize_filters, _serialize_filters, _is_filter_empty,
    BusinessException
)


class RestoreResult:
    def __init__(self, success=False, scheme=None, filters=None,
                 fallback_reason=None, warnings=None):
        self.success = success
        self.scheme = scheme
        self.filters = filters or {}
        self.fallback_reason = fallback_reason
        self.warnings = warnings or []


def save_last_filters(user_id, filters):
    if not filters or _is_filter_empty(filters):
        filters_json = ""
    else:
        filters_json = _serialize_filters(filters)
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO user_preferences (user_id, pref_key, pref_value, updated_at)
            VALUES (?, 'last_filters', ?, ?)
            ON CONFLICT(user_id, pref_key) DO UPDATE SET
                pref_value = excluded.pref_value,
                updated_at = excluded.updated_at
        """, (user_id, filters_json, now))


def get_last_filters(user_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE user_id = ? AND pref_key = 'last_filters'",
            (user_id,)
        ).fetchone()
        if not row or not row["pref_value"]:
            return {}
        return _deserialize_filters(row["pref_value"])


def save_last_list_state(user_id, sort_by=None, sort_order=None, page=None, page_size=None):
    state = {}
    if sort_by:
        state["sort_by"] = sort_by
    if sort_order:
        state["sort_order"] = sort_order
    if page is not None:
        state["page"] = page
    if page_size:
        state["page_size"] = page_size
    state_json = json.dumps(state, ensure_ascii=False) if state else ""
    now = datetime.now().isoformat()
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO user_preferences (user_id, pref_key, pref_value, updated_at)
            VALUES (?, 'last_list_state', ?, ?)
            ON CONFLICT(user_id, pref_key) DO UPDATE SET
                pref_value = excluded.pref_value,
                updated_at = excluded.updated_at
        """, (user_id, state_json, now))


def get_last_list_state(user_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT pref_value FROM user_preferences WHERE user_id = ? AND pref_key = 'last_list_state'",
            (user_id,)
        ).fetchone()
        if not row or not row["pref_value"]:
            return {}
        try:
            return json.loads(row["pref_value"])
        except (json.JSONDecodeError, TypeError):
            return {}


def set_active_scheme_id(user_id, scheme_id):
    from services import save_user_preference
    save_user_preference(user_id, "last_active_scheme_id",
                         str(scheme_id) if scheme_id else "")


def get_active_scheme_id(user_id):
    from services import get_user_preference
    val = get_user_preference(user_id, "last_active_scheme_id")
    if val:
        try:
            return int(val)
        except (ValueError, TypeError):
            return None
    return None


def _can_access_scheme(scheme, user_id, role):
    if not scheme:
        return False
    if scheme["owner_id"] == user_id:
        return True
    if scheme["scope"] == "shared":
        return True
    return False


def restore_workbench_state(user_id, role):
    result = RestoreResult()
    result.warnings = []

    active_scheme_id = get_active_scheme_id(user_id)

    if active_scheme_id:
        scheme = get_filter_scheme_by_id(active_scheme_id)
        if scheme and _can_access_scheme(scheme, user_id, role):
            result.scheme = scheme
            result.filters = scheme["filters"]
            result.success = True
            _log_restore_operation(user_id, scheme, success=True,
                                   detail=f"从方案「{scheme['name']}」恢复筛选条件")
            return result
        else:
            reason = "方案已被删除" if not scheme else "无权限访问该方案"
            result.fallback_reason = reason
            result.warnings.append(f"激活方案不可用: {reason}，已回退到上次使用的条件")
            set_active_scheme_id(user_id, None)
            _log_restore_operation(user_id, None, success=False,
                                   detail=f"方案恢复失败: {reason}")

    last_filters = get_last_filters(user_id)
    if last_filters and not _is_filter_empty(last_filters):
        result.filters = last_filters
        result.success = True
        result.warnings.append("已从上次使用的筛选条件恢复")
        _log_restore_operation(user_id, None, success=True,
                               detail="从上次筛选条件恢复")
        return result

    result.filters = {}
    result.success = True
    result.warnings.append("无可用的历史筛选条件，使用默认视图")
    return result


def _log_restore_operation(user_id, scheme, success=True, detail=""):
    from database import get_connection
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO operation_logs (operator_id, action, target_type, target_id,
                detail, success, error_message, created_at)
            VALUES (?, 'restore_filter_state', 'filter_scheme', ?, ?, ?, ?, ?)
        """, (
            user_id,
            scheme["id"] if scheme else None,
            detail,
            1 if success else 0,
            None if success else detail,
            datetime.now().isoformat()
        ))


def log_query_operation(user_id, filters, record_count):
    from database import get_connection
    detail_parts = []
    if filters.get("status"):
        detail_parts.append(f"状态:{filters['status']}")
    if filters.get("keyword"):
        detail_parts.append(f"关键字:{filters['keyword']}")
    if filters.get("borrower_id"):
        detail_parts.append(f"借用人ID:{filters['borrower_id']}")
    if filters.get("date_from") or filters.get("date_to"):
        date_range = f"{filters.get('date_from', '')}~{filters.get('date_to', '')}"
        detail_parts.append(f"日期:{date_range}")
    detail = f"查询借还记录, 共{record_count}条"
    if detail_parts:
        detail += " (" + ", ".join(detail_parts) + ")"

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO operation_logs (operator_id, action, target_type, target_id,
                detail, success, error_message, created_at)
            VALUES (?, 'query_borrow_records', 'borrow_record', NULL, ?, 1, NULL, ?)
        """, (user_id, detail, datetime.now().isoformat()))


def log_export_operation(user_id, filters, record_count, file_name, scheme_id=None):
    from database import get_connection
    detail_parts = [f"导出{record_count}条记录", f"文件:{file_name}"]
    if filters.get("status"):
        detail_parts.append(f"状态:{filters['status']}")
    if filters.get("keyword"):
        detail_parts.append(f"关键字:{filters['keyword']}")
    if scheme_id:
        scheme = get_filter_scheme_by_id(scheme_id)
        if scheme:
            detail_parts.append(f"方案:{scheme['name']}")
    detail = ", ".join(detail_parts)

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO operation_logs (operator_id, action, target_type, target_id,
                detail, success, error_message, created_at)
            VALUES (?, 'export_borrow_records', 'borrow_record', NULL, ?, 1, NULL, ?)
        """, (user_id, detail, datetime.now().isoformat()))


def verify_export_consistency(file_path, filters):
    records_from_db = get_borrow_records(**filters)

    import csv
    csv_count = 0
    csv_record_nos = set()
    try:
        with open(file_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                csv_count += 1
                if "记录编号" in row:
                    csv_record_nos.add(row["记录编号"])
    except Exception as e:
        return {"consistent": False, "reason": f"读取CSV失败: {e}",
                "db_count": len(records_from_db), "csv_count": csv_count}

    db_record_nos = {r["record_no"] for r in records_from_db}

    if csv_count != len(records_from_db):
        return {
            "consistent": False,
            "reason": f"数量不一致: 数据库{len(records_from_db)}条, CSV{csv_count}条",
            "db_count": len(records_from_db),
            "csv_count": csv_count
        }

    if csv_record_nos and db_record_nos:
        if csv_record_nos != db_record_nos:
            missing = db_record_nos - csv_record_nos
            extra = csv_record_nos - db_record_nos
            return {
                "consistent": False,
                "reason": f"记录集合不一致: 缺少{len(missing)}条, 多出{len(extra)}条",
                "db_count": len(records_from_db),
                "csv_count": csv_count,
                "missing_records": list(missing),
                "extra_records": list(extra)
            }

    return {
        "consistent": True,
        "reason": "数据完全一致",
        "db_count": len(records_from_db),
        "csv_count": csv_count
    }


def clear_all_user_state(user_id):
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM user_preferences WHERE user_id = ?",
            (user_id,)
        )


def get_available_schemes(user_id, role):
    return get_filter_schemes(user_id, role)


def activate_scheme(user_id, scheme_id, role):
    scheme = get_filter_scheme_by_id(scheme_id)
    if not scheme:
        raise BusinessException("方案不存在")
    if not _can_access_scheme(scheme, user_id, role):
        raise BusinessException("无权限访问该方案")

    set_active_scheme_id(user_id, scheme_id)

    if scheme["filters"] and not _is_filter_empty(scheme["filters"]):
        save_last_filters(user_id, scheme["filters"])

    return scheme


def deactivate_scheme(user_id):
    set_active_scheme_id(user_id, None)


def delete_scheme_and_cleanup(scheme_id, user_id, role):
    from services import delete_filter_scheme

    scheme = get_filter_scheme_by_id(scheme_id)
    if not scheme:
        raise BusinessException("方案不存在")

    was_active = (get_active_scheme_id(user_id) == scheme_id)

    delete_filter_scheme(scheme_id, user_id, role)

    if was_active:
        deactivate_scheme(user_id)

    return was_active
