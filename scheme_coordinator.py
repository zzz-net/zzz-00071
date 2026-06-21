import json
import logging
import os
from datetime import datetime, timedelta
from database import get_connection
from services import (
    get_filter_scheme_by_id, get_filter_schemes, get_borrow_records,
    _deserialize_filters, _serialize_filters, _is_filter_empty,
    BusinessException, save_user_preference, get_user_preference
)

logger = logging.getLogger(__name__)

GLOBAL_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "workbench_global_config.json"
)


def _load_global_config():
    """加载全局配置（用于存储跨用户的全局状态，如上次登录用户ID）"""
    if not os.path.exists(GLOBAL_CONFIG_FILE):
        return {}
    try:
        with open(GLOBAL_CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_global_config(config):
    """保存全局配置"""
    try:
        with open(GLOBAL_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except IOError as e:
        logger.warning(f"保存全局配置失败: {e}")


# ============================================================
# 数据模型层
# ============================================================

class WorkbenchState:
    """工作台完整状态 - 包含筛选条件、排序、分页、激活方案"""

    def __init__(self):
        self.filters = {}
        self.sort_by = "created_at"
        self.sort_order = "desc"
        self.page = 1
        self.page_size = 20
        self.active_scheme_id = None
        self.active_scheme_name = None

    def to_dict(self):
        return {
            "filters": self.filters,
            "sort_by": self.sort_by,
            "sort_order": self.sort_order,
            "page": self.page,
            "page_size": self.page_size,
            "active_scheme_id": self.active_scheme_id,
            "active_scheme_name": self.active_scheme_name,
        }

    @classmethod
    def from_dict(cls, data):
        state = cls()
        if data:
            state.filters = data.get("filters", {}) or {}
            state.sort_by = data.get("sort_by", "created_at")
            state.sort_order = data.get("sort_order", "desc")
            state.page = data.get("page", 1)
            state.page_size = data.get("page_size", 20)
            state.active_scheme_id = data.get("active_scheme_id")
            state.active_scheme_name = data.get("active_scheme_name")
        return state


class RestoreResult:
    """恢复结果 - 包含成功状态、数据、回退原因、警告信息"""

    def __init__(self, success=False, state=None, scheme=None, filters=None,
                 fallback_reason=None, warnings=None, fallback_level="none"):
        self.success = success
        self.state = state or WorkbenchState()
        self.scheme = scheme
        self.fallback_reason = fallback_reason
        self.warnings = warnings or []
        self.fallback_level = fallback_level  # none / full_state / last_filters / default / corrupt
        if filters is not None:
            self.state.filters = filters or {}

    @property
    def filters(self):
        return self.state.filters if self.state else {}

    @filters.setter
    def filters(self, value):
        if self.state:
            self.state.filters = value or {}


class DeletedSchemeInfo:
    """已删除方案信息 - 用于回收站和回退"""

    def __init__(self, scheme_id, name, owner_id, scope, filters,
                 deleted_at, deleted_by):
        self.scheme_id = scheme_id
        self.name = name
        self.owner_id = owner_id
        self.scope = scope
        self.filters = filters
        self.deleted_at = deleted_at
        self.deleted_by = deleted_by


# ============================================================
# 状态持久化层 - StatePersistence
# ============================================================

class StatePersistence:
    """状态持久化层 - 负责所有状态的存取，不包含业务逻辑"""

    PREF_KEY_WORKBENCH_STATE = "workbench_full_state"
    PREF_KEY_LAST_USER_ID = "last_login_user_id"
    PREF_KEY_DELETED_SCHEMES = "deleted_schemes_recycle_bin"
    MAX_RECYCLE_ITEMS = 20
    RECYCLE_EXPIRE_DAYS = 7

    @staticmethod
    def save_workbench_full_state(user_id, state):
        """保存完整工作台状态（筛选+排序+分页+激活方案）"""
        if not isinstance(state, WorkbenchState):
            state = WorkbenchState.from_dict(state)
        state_json = json.dumps(state.to_dict(), ensure_ascii=False)
        save_user_preference(user_id, StatePersistence.PREF_KEY_WORKBENCH_STATE, state_json)
        _log_operation(user_id, "save_workbench_state", "workbench", None,
                       f"保存工作台完整状态: 方案={state.active_scheme_name or '无'}, 页码={state.page}")

    @staticmethod
    def load_workbench_full_state(user_id):
        """加载完整工作台状态，异常时返回 None"""
        raw = get_user_preference(user_id, StatePersistence.PREF_KEY_WORKBENCH_STATE)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            state = WorkbenchState.from_dict(data)
            if not isinstance(state.filters, dict):
                state.filters = {}
            if not isinstance(state.page, int) or state.page < 1:
                state.page = 1
            if not isinstance(state.page_size, int) or state.page_size < 1:
                state.page_size = 20
            return state
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"用户 {user_id} 的工作台状态损坏: {e}")
            _log_operation(user_id, "load_workbench_state_failed", "workbench", None,
                           f"工作台状态数据损坏: {e}", success=False, error_message=str(e))
            return None

    @staticmethod
    def clear_workbench_state(user_id):
        """清空工作台状态"""
        save_user_preference(user_id, StatePersistence.PREF_KEY_WORKBENCH_STATE, "")

    @staticmethod
    def save_last_user_id(user_id):
        """记录上次登录的用户ID（用于检测账号切换）"""
        config = _load_global_config()
        config["last_user_id"] = int(user_id)
        _save_global_config(config)

    @staticmethod
    def get_last_user_id():
        """获取上次登录的用户ID"""
        config = _load_global_config()
        raw = config.get("last_user_id")
        if raw is not None:
            try:
                return int(raw)
            except (ValueError, TypeError):
                return None
        return None

    @staticmethod
    def save_last_filters(user_id, filters):
        """保存上次使用的筛选条件（兼容旧接口）"""
        if not filters or _is_filter_empty(filters):
            filters_json = ""
        else:
            filters_json = _serialize_filters(filters)
        save_user_preference(user_id, "last_filters", filters_json)

    @staticmethod
    def get_last_filters(user_id):
        """获取上次使用的筛选条件（兼容旧接口）"""
        raw = get_user_preference(user_id, "last_filters")
        if not raw:
            return {}
        return _deserialize_filters(raw)

    @staticmethod
    def save_list_state(user_id, sort_by=None, sort_order=None, page=None, page_size=None):
        """保存列表状态（排序、分页）（兼容旧接口）"""
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
        save_user_preference(user_id, "last_list_state", state_json)

    @staticmethod
    def get_list_state(user_id):
        """获取列表状态（兼容旧接口）"""
        raw = get_user_preference(user_id, "last_list_state")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}

    @staticmethod
    def add_to_recycle_bin(scheme, deleted_by_user_id):
        """将方案移入回收站（软删除）"""
        recycle = StatePersistence._get_recycle_bin(deleted_by_user_id)
        item = {
            "scheme_id": scheme["id"],
            "name": scheme["name"],
            "owner_id": scheme["owner_id"],
            "scope": scheme["scope"],
            "filters": scheme["filters"],
            "deleted_at": datetime.now().isoformat(),
            "deleted_by": deleted_by_user_id,
        }
        recycle.insert(0, item)
        StatePersistence._prune_recycle_bin(recycle)
        StatePersistence._save_recycle_bin(deleted_by_user_id, recycle)
        _log_operation(deleted_by_user_id, "move_scheme_to_recycle", "filter_scheme",
                       scheme["id"], f"方案「{scheme['name']}」移入回收站")

    @staticmethod
    def get_recycle_bin(user_id):
        """获取回收站列表"""
        recycle = StatePersistence._get_recycle_bin(user_id)
        return [DeletedSchemeInfo(
            scheme_id=item["scheme_id"],
            name=item["name"],
            owner_id=item["owner_id"],
            scope=item["scope"],
            filters=item["filters"],
            deleted_at=item["deleted_at"],
            deleted_by=item["deleted_by"],
        ) for item in recycle]

    @staticmethod
    def restore_from_recycle_bin(user_id, scheme_name):
        """从回收站恢复方案（返回方案数据用于重建）"""
        recycle = StatePersistence._get_recycle_bin(user_id)
        for i, item in enumerate(recycle):
            if item["name"] == scheme_name:
                restored_item = recycle.pop(i)
                StatePersistence._save_recycle_bin(user_id, recycle)
                _log_operation(user_id, "restore_scheme_from_recycle", "filter_scheme",
                               restored_item["scheme_id"],
                               f"从回收站恢复方案「{restored_item['name']}」")
                return DeletedSchemeInfo(
                    scheme_id=restored_item["scheme_id"],
                    name=restored_item["name"],
                    owner_id=restored_item["owner_id"],
                    scope=restored_item["scope"],
                    filters=restored_item["filters"],
                    deleted_at=restored_item["deleted_at"],
                    deleted_by=restored_item["deleted_by"],
                )
        return None

    @staticmethod
    def clear_expired_recycle_items(user_id):
        """清理过期的回收站项目"""
        recycle = StatePersistence._get_recycle_bin(user_id)
        now = datetime.now()
        expire_delta = timedelta(days=StatePersistence.RECYCLE_EXPIRE_DAYS)
        original_count = len(recycle)
        recycle = [
            item for item in recycle
            if _parse_datetime_safe(item.get("deleted_at")) and
               (now - _parse_datetime_safe(item["deleted_at"])) < expire_delta
        ]
        if len(recycle) != original_count:
            StatePersistence._save_recycle_bin(user_id, recycle)
            _log_operation(user_id, "clear_expired_recycle", "filter_scheme", None,
                           f"清理了 {original_count - len(recycle)} 个过期回收站项目")

    @staticmethod
    def _get_recycle_bin(user_id):
        raw = get_user_preference(user_id, StatePersistence.PREF_KEY_DELETED_SCHEMES)
        if not raw:
            return []
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []

    @staticmethod
    def _save_recycle_bin(user_id, recycle):
        recycle_json = json.dumps(recycle, ensure_ascii=False)
        save_user_preference(user_id, StatePersistence.PREF_KEY_DELETED_SCHEMES, recycle_json)

    @staticmethod
    def _prune_recycle_bin(recycle):
        if len(recycle) > StatePersistence.MAX_RECYCLE_ITEMS:
            del recycle[StatePersistence.MAX_RECYCLE_ITEMS:]

    @staticmethod
    def clear_all_user_state(user_id):
        """清除用户所有状态"""
        with get_connection() as conn:
            conn.execute(
                "DELETE FROM user_preferences WHERE user_id = ?",
                (user_id,)
            )


# ============================================================
# 权限与校验层 - Permission & Validation
# ============================================================

class PermissionGuard:
    """权限校验层 - 统一处理方案的可见范围和编辑权限"""

    @staticmethod
    def can_access_scheme(scheme, user_id, role):
        """判断用户是否可以访问（查看/使用）方案"""
        if not scheme:
            return False
        if scheme.get("owner_id") == user_id:
            return True
        if scheme.get("scope") == "shared":
            return True
        return False

    @staticmethod
    def can_edit_scheme(scheme, user_id, role):
        """判断用户是否可以编辑（修改/删除/重命名）方案"""
        if not scheme:
            return False
        if scheme["owner_id"] == user_id:
            return True
        if scheme["scope"] == "shared" and role == "supervisor":
            return True
        return False

    @staticmethod
    def can_create_shared_scheme(role):
        """判断用户是否可以创建共享方案"""
        return role == "supervisor"

    @staticmethod
    def check_name_conflict(name, user_id, scheme_id=None):
        """检查方案名称是否冲突（个人同名 + 共享同名）"""
        name = name.strip()
        with get_connection() as conn:
            if scheme_id:
                row = conn.execute(
                    "SELECT id FROM filter_schemes WHERE name = ? AND id != ? "
                    "AND ((owner_id = ? AND scope = 'personal') OR scope = 'shared')",
                    (name, scheme_id, user_id)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM filter_schemes WHERE name = ? "
                    "AND ((owner_id = ? AND scope = 'personal') OR scope = 'shared')",
                    (name, user_id)
                ).fetchone()
            return row is not None


# ============================================================
# 恢复协调层 - RestoreCoordinator
# ============================================================

class RestoreCoordinator:
    """
    恢复协调层 - 按优先级逐级尝试恢复
    优先级: 激活方案 > 完整工作台状态 > 上次筛选条件 > 默认状态
    每一层失败都有对应的回退和警告
    """

    @staticmethod
    def restore_workbench(user_id, role):
        """
        完整恢复工作台状态
        返回 RestoreResult，包含恢复的状态和回退信息
        
        fallback_level 说明:
        - none: 从激活方案恢复（最高优先级，无回退）
        - full_state: 从完整工作台状态恢复（回退1级）
        - last_filters: 从上次筛选条件恢复（回退2级）
        - default: 使用默认状态（回退3级）
        - corrupt: 配置损坏，强制重置
        """
        result = RestoreResult()
        result.warnings = []
        result.state = WorkbenchState()

        StatePersistence.clear_expired_recycle_items(user_id)

        # 第1层: 尝试从激活方案恢复
        scheme_restored = RestoreCoordinator._try_restore_from_active_scheme(user_id, role, result)
        if scheme_restored:
            result.success = True
            result.fallback_level = "none"
            _log_operation(user_id, "restore_filter_state", "workbench", None,
                           f"工作台状态恢复: 激活方案「{result.scheme.get('name')}」，级别=none")
            return result

        # 第2层: 尝试从完整工作台状态恢复
        state_restored = RestoreCoordinator._try_restore_from_full_state(user_id, role, result)
        if state_restored:
            result.success = True
            result.fallback_level = "full_state"
            _log_operation(user_id, "restore_filter_state", "workbench", None,
                           f"工作台状态恢复: 完整状态，级别=full_state，原因={result.fallback_reason or '无激活方案'}")
            return result

        # 第3层: 尝试从上次筛选条件恢复
        filters_restored = RestoreCoordinator._try_restore_from_last_filters(user_id, result)
        if filters_restored:
            result.success = True
            result.fallback_level = "last_filters"
            _log_operation(user_id, "restore_filter_state", "workbench", None,
                           f"工作台状态恢复: 上次筛选，级别=last_filters")
            return result

        # 第4层: 使用默认状态
        result.state = WorkbenchState()
        result.success = True
        result.fallback_level = "default"
        result.warnings.append("无可用的历史状态，使用默认视图")
        _log_operation(user_id, "restore_workbench_default", "workbench", None,
                       "无历史状态，使用默认视图")
        _log_operation(user_id, "restore_filter_state", "workbench", None,
                       "工作台状态恢复: 默认视图，级别=default")

        return result

    @staticmethod
    def _try_restore_from_active_scheme(user_id, role, result):
        """尝试从激活方案恢复"""
        from services import get_active_scheme_id as get_scheme_id
        active_scheme_id = get_scheme_id(user_id)
        if not active_scheme_id:
            return False

        try:
            scheme = get_filter_scheme_by_id(active_scheme_id)
        except Exception as e:
            result.warnings.append(f"查询激活方案失败: {e}，尝试其他恢复方式")
            _log_operation(user_id, "restore_scheme_query_failed", "filter_scheme",
                           active_scheme_id, f"查询激活方案异常: {e}", success=False,
                           error_message=str(e))
            return False

        if not scheme:
            result.fallback_reason = "方案已被删除"
            result.warnings.append("激活方案已被删除，已回退到其他恢复方式")
            _clear_active_scheme_safe(user_id)
            _log_operation(user_id, "restore_scheme_deleted", "filter_scheme",
                           active_scheme_id, "激活方案已删除，回退", success=False)
            return False

        if not PermissionGuard.can_access_scheme(scheme, user_id, role):
            result.fallback_reason = "无权限访问该方案"
            result.warnings.append("无权限访问激活方案，已回退到其他恢复方式")
            _clear_active_scheme_safe(user_id)
            _log_operation(user_id, "restore_scheme_no_permission", "filter_scheme",
                           active_scheme_id, "无权限访问激活方案，回退", success=False)
            return False

        result.scheme = scheme
        result.state.filters = scheme["filters"] or {}
        result.state.active_scheme_id = scheme["id"]
        result.state.active_scheme_name = scheme["name"]

        list_state = StatePersistence.get_list_state(user_id)
        if list_state:
            result.state.sort_by = list_state.get("sort_by", "created_at")
            result.state.sort_order = list_state.get("sort_order", "desc")
            result.state.page = list_state.get("page", 1)
            result.state.page_size = list_state.get("page_size", 20)

        _log_operation(user_id, "restore_from_scheme", "filter_scheme", scheme["id"],
                       f"从方案「{scheme['name']}」恢复工作台状态")
        return True

    @staticmethod
    def _try_restore_from_full_state(user_id, role, result):
        """尝试从完整工作台状态恢复"""
        state = StatePersistence.load_workbench_full_state(user_id)
        if not state:
            return False

        if state.active_scheme_id:
            try:
                scheme = get_filter_scheme_by_id(state.active_scheme_id)
                if scheme and PermissionGuard.can_access_scheme(scheme, user_id, role):
                    result.scheme = scheme
                    result.state = state
                    result.state.filters = scheme["filters"] or {}
                    result.state.active_scheme_name = scheme["name"]
                    _log_operation(user_id, "restore_from_full_state", "workbench", None,
                                   f"从完整状态恢复（方案: {scheme['name']}）")
                    return True
                else:
                    result.warnings.append("完整状态中的方案不可用，仅恢复筛选和列表状态")
                    state.active_scheme_id = None
                    state.active_scheme_name = None
            except Exception as e:
                result.warnings.append(f"校验完整状态中的方案失败: {e}")
                state.active_scheme_id = None
                state.active_scheme_name = None

        if state.filters and not _is_filter_empty(state.filters):
            result.state = state
            _log_operation(user_id, "restore_from_full_state_partial", "workbench", None,
                           "从完整状态恢复（无激活方案）")
            return True

        return False

    @staticmethod
    def _try_restore_from_last_filters(user_id, result):
        """尝试从上次筛选条件恢复"""
        filters = StatePersistence.get_last_filters(user_id)
        if not filters or _is_filter_empty(filters):
            return False

        result.state.filters = filters

        list_state = StatePersistence.get_list_state(user_id)
        if list_state:
            result.state.sort_by = list_state.get("sort_by", "created_at")
            result.state.sort_order = list_state.get("sort_order", "desc")
            result.state.page = list_state.get("page", 1)
            result.state.page_size = list_state.get("page_size", 20)

        result.warnings.append("已从上次使用的筛选条件恢复")
        _log_operation(user_id, "restore_from_last_filters", "workbench", None,
                       "从上次筛选条件恢复")
        return True


# ============================================================
# 异常兜底层 - FallbackHandler
# ============================================================

class FallbackHandler:
    """异常兜底层 - 处理各种异常场景的安全回退"""

    @staticmethod
    def handle_corrupt_state(user_id):
        """处理本地配置损坏的情况：清除损坏数据，返回干净的默认状态"""
        StatePersistence.clear_workbench_state(user_id)
        StatePersistence.save_last_filters(user_id, {})
        StatePersistence.save_list_state(user_id)
        _clear_active_scheme_safe(user_id)

        _log_operation(user_id, "handle_corrupt_state", "workbench", None,
                       "检测到配置损坏，已重置为默认状态", success=False)

        result = RestoreResult()
        result.state = WorkbenchState()
        result.success = True
        result.fallback_level = "corrupt"
        result.warnings.append("检测到本地配置损坏，已重置为默认视图")
        result.fallback_reason = "本地配置已损坏"
        return result

    @staticmethod
    def handle_scheme_deletion(user_id, scheme_id, role):
        """
        处理方案被删除的情况：
        1. 如果该方案是激活方案，清除激活状态
        2. 尝试回退到上次筛选条件
        3. 如果都没有，返回默认状态
        """
        active_id = _get_active_scheme_id_safe(user_id)
        was_active = (active_id == scheme_id)

        if was_active:
            _clear_active_scheme_safe(user_id)

        result = RestoreResult()
        result.state = WorkbenchState()
        result.success = True

        last_filters = StatePersistence.get_last_filters(user_id)
        if last_filters and not _is_filter_empty(last_filters):
            result.state.filters = last_filters
            result.fallback_level = "last_filters"
            result.warnings.append("方案已删除，已回退到上次使用的筛选条件")
            result.fallback_reason = "方案已删除"
        else:
            result.fallback_level = "default"
            result.warnings.append("方案已删除，已回退到默认视图")
            result.fallback_reason = "方案已删除"

        _log_operation(user_id, "handle_scheme_deletion", "filter_scheme", scheme_id,
                       f"方案删除后的回退处理: {result.fallback_level}")

        return result

    @staticmethod
    def handle_user_switch(prev_user_id, new_user_id):
        """
        处理账号切换：
        1. 保存旧用户的最后状态
        2. 清除当前界面状态（由调用方处理）
        3. 返回新用户应该恢复的状态
        """
        StatePersistence.save_last_user_id(new_user_id)
        _log_operation(new_user_id, "user_switch", "workbench", None,
                       f"账号切换: 从用户{prev_user_id}切换到用户{new_user_id}")

    @staticmethod
    def handle_empty_filters(user_id):
        """处理空筛选条件：返回默认状态，保留激活方案信息"""
        result = RestoreResult()
        result.state = WorkbenchState()
        result.success = True
        result.fallback_level = "default"

        active_id = _get_active_scheme_id_safe(user_id)
        if active_id:
            try:
                scheme = get_filter_scheme_by_id(active_id)
                if scheme and not _is_filter_empty(scheme.get("filters", {})):
                    result.scheme = scheme
                    result.state.filters = scheme["filters"]
                    result.state.active_scheme_id = scheme["id"]
                    result.state.active_scheme_name = scheme["name"]
                    result.fallback_level = "none"
                    result.warnings.append("已从激活方案恢复筛选条件")
                    return result
            except Exception:
                pass

        result.warnings.append("当前筛选条件为空，使用默认视图")
        return result


# ============================================================
# 方案操作层 - SchemeOperations
# ============================================================

class SchemeOperations:
    """方案操作层 - 统一的方案CRUD操作，包含完整的权限和冲突处理"""

    @staticmethod
    def rename_scheme(scheme_id, new_name, user_id, role):
        """重命名方案（含权限校验和同名冲突检测）"""
        new_name = new_name.strip()
        if not new_name:
            raise BusinessException("方案名称不能为空")

        scheme = get_filter_scheme_by_id(scheme_id)
        if not scheme:
            raise BusinessException("方案不存在")

        if not PermissionGuard.can_edit_scheme(scheme, user_id, role):
            raise BusinessException("无权限重命名该方案")

        if PermissionGuard.check_name_conflict(new_name, user_id, scheme_id):
            raise BusinessException(f"同名方案已存在: {new_name}")

        with get_connection() as conn:
            conn.execute(
                "UPDATE filter_schemes SET name = ?, updated_at = ? WHERE id = ?",
                (new_name, datetime.now().isoformat(), scheme_id)
            )

        _log_operation(user_id, "rename_scheme", "filter_scheme", scheme_id,
                       f"方案重命名: {scheme['name']} -> {new_name}")

        active_id = _get_active_scheme_id_safe(user_id)
        if active_id == scheme_id:
            state = StatePersistence.load_workbench_full_state(user_id)
            if state:
                state.active_scheme_name = new_name
                StatePersistence.save_workbench_full_state(user_id, state)

        return get_filter_scheme_by_id(scheme_id)

    @staticmethod
    def soft_delete_scheme(scheme_id, user_id, role):
        """软删除方案（移入回收站），可在一定时间内恢复"""
        from services import delete_filter_scheme

        scheme = get_filter_scheme_by_id(scheme_id)
        if not scheme:
            raise BusinessException("方案不存在")

        if not PermissionGuard.can_edit_scheme(scheme, user_id, role):
            raise BusinessException("无权限删除该方案")

        StatePersistence.add_to_recycle_bin(scheme, user_id)

        was_active = (_get_active_scheme_id_safe(user_id) == scheme_id)

        delete_filter_scheme(scheme_id, user_id, role)

        return {
            "was_active": was_active,
            "scheme_name": scheme["name"],
            "message": f"方案「{scheme['name']}」已删除，可在回收站中恢复"
        }

    @staticmethod
    def restore_scheme_from_recycle(user_id, scheme_name, role):
        """从回收站恢复方案"""
        deleted_info = StatePersistence.restore_from_recycle_bin(user_id, scheme_name)
        if not deleted_info:
            raise BusinessException(f"回收站中未找到方案: {scheme_name}")

        if PermissionGuard.check_name_conflict(scheme_name, user_id):
            raise BusinessException(f"当前已有同名方案「{scheme_name}」，无法恢复")

        if deleted_info.scope == "shared" and not PermissionGuard.can_create_shared_scheme(role):
            raise BusinessException("您无权限创建共享方案，无法恢复该共享方案")

        from services import save_filter_scheme
        new_scheme_id = save_filter_scheme(
            name=scheme_name,
            owner_id=user_id,
            filters=deleted_info.filters,
            scope=deleted_info.scope,
            role=role
        )

        _log_operation(user_id, "restore_scheme", "filter_scheme", new_scheme_id,
                       f"从回收站恢复方案「{scheme_name}」")

        return get_filter_scheme_by_id(new_scheme_id)

    @staticmethod
    def get_recycle_list(user_id):
        """获取回收站列表"""
        return StatePersistence.get_recycle_bin(user_id)

    @staticmethod
    def activate_scheme(user_id, scheme_id, role):
        """激活方案"""
        scheme = get_filter_scheme_by_id(scheme_id)
        if not scheme:
            raise BusinessException("方案不存在")
        if not PermissionGuard.can_access_scheme(scheme, user_id, role):
            raise BusinessException("无权限访问该方案")

        from services import set_active_scheme_id
        set_active_scheme_id(user_id, scheme_id)

        if scheme["filters"] and not _is_filter_empty(scheme["filters"]):
            StatePersistence.save_last_filters(user_id, scheme["filters"])

        state = StatePersistence.load_workbench_full_state(user_id) or WorkbenchState()
        state.active_scheme_id = scheme["id"]
        state.active_scheme_name = scheme["name"]
        state.filters = dict(scheme["filters"] or {})
        StatePersistence.save_workbench_full_state(user_id, state)

        return scheme

    @staticmethod
    def deactivate_scheme(user_id):
        """取消激活方案"""
        from services import set_active_scheme_id
        set_active_scheme_id(user_id, None)

        state = StatePersistence.load_workbench_full_state(user_id)
        if state:
            state.active_scheme_id = None
            state.active_scheme_name = None
            StatePersistence.save_workbench_full_state(user_id, state)


# ============================================================
# 导出一致性验证层 - ExportVerifier
# ============================================================

class ExportVerifier:
    """导出一致性验证 - 确保CSV导出与当前列表一一对应"""

    @staticmethod
    def verify_export_consistency(file_path, filters):
        """
        验证CSV导出与数据库查询结果的一致性
        返回: {consistent, reason, db_count, csv_count, missing_records, extra_records}
        """
        try:
            records_from_db = get_borrow_records(**filters)
        except Exception as e:
            return {"consistent": False, "reason": f"查询数据库失败: {e}",
                    "db_count": 0, "csv_count": 0}

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
        except FileNotFoundError:
            return {"consistent": False, "reason": "CSV文件不存在",
                    "db_count": len(records_from_db), "csv_count": 0}
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


# ============================================================
# 操作日志辅助
# ============================================================

def _log_operation(operator_id, action, target_type=None, target_id=None,
                   detail=None, success=True, error_message=None):
    """统一的操作日志记录"""
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO operation_logs (operator_id, action, target_type, target_id,
                    detail, success, error_message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                operator_id, action, target_type, target_id, detail,
                1 if success else 0, error_message, datetime.now().isoformat()
            ))
    except Exception as e:
        logger.warning(f"记录操作日志失败: {e}")


def _parse_datetime_safe(dt_str):
    """安全解析日期时间字符串"""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return None


def _clear_active_scheme_safe(user_id):
    """安全清除激活方案状态"""
    try:
        from services import set_active_scheme_id
        set_active_scheme_id(user_id, None)
    except Exception as e:
        logger.warning(f"清除激活方案失败: {e}")


def _get_active_scheme_id_safe(user_id):
    """安全获取激活方案ID"""
    try:
        from services import get_active_scheme_id
        return get_active_scheme_id(user_id)
    except Exception:
        return None


# ============================================================
# 兼容旧接口的函数封装
# ============================================================

def save_last_filters(user_id, filters):
    StatePersistence.save_last_filters(user_id, filters)


def get_last_filters(user_id):
    return StatePersistence.get_last_filters(user_id)


def save_last_list_state(user_id, sort_by=None, sort_order=None, page=None, page_size=None):
    StatePersistence.save_list_state(user_id, sort_by, sort_order, page, page_size)


def get_last_list_state(user_id):
    return StatePersistence.get_list_state(user_id)


def set_active_scheme_id(user_id, scheme_id):
    from services import set_active_scheme_id as services_set
    services_set(user_id, scheme_id)


def get_active_scheme_id(user_id):
    from services import get_active_scheme_id as services_get
    return services_get(user_id)


def _can_access_scheme(scheme, user_id, role):
    return PermissionGuard.can_access_scheme(scheme, user_id, role)


def restore_workbench_state(user_id, role):
    """恢复工作台状态（兼容旧接口）"""
    coordinator_result = RestoreCoordinator.restore_workbench(user_id, role)
    result = RestoreResult(
        success=coordinator_result.success,
        scheme=coordinator_result.scheme,
        filters=coordinator_result.state.filters,
        fallback_reason=coordinator_result.fallback_reason,
        warnings=coordinator_result.warnings,
    )
    result.state = coordinator_result.state
    result.fallback_level = coordinator_result.fallback_level
    return result


def _log_restore_operation(user_id, scheme, success=True, detail=""):
    _log_operation(user_id, "restore_filter_state", "filter_scheme",
                   scheme["id"] if scheme else None, detail, success)


def log_query_operation(user_id, filters, record_count):
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
    _log_operation(user_id, "query_borrow_records", "borrow_record", None, detail)


def log_export_operation(user_id, filters, record_count, file_name, scheme_id=None):
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
    _log_operation(user_id, "export_borrow_records", "borrow_record", None, detail)


def verify_export_consistency(file_path, filters):
    return ExportVerifier.verify_export_consistency(file_path, filters)


def clear_all_user_state(user_id):
    StatePersistence.clear_all_user_state(user_id)


def get_available_schemes(user_id, role):
    return get_filter_schemes(user_id, role)


def activate_scheme(user_id, scheme_id, role):
    return SchemeOperations.activate_scheme(user_id, scheme_id, role)


def deactivate_scheme(user_id):
    SchemeOperations.deactivate_scheme(user_id)


def delete_scheme_and_cleanup(scheme_id, user_id, role):
    result = SchemeOperations.soft_delete_scheme(scheme_id, user_id, role)
    return result["was_active"]


def save_workbench_full_state(user_id, state):
    StatePersistence.save_workbench_full_state(user_id, state)


def load_workbench_full_state(user_id):
    return StatePersistence.load_workbench_full_state(user_id)


def rename_scheme(scheme_id, new_name, user_id, role):
    return SchemeOperations.rename_scheme(scheme_id, new_name, user_id, role)


def get_recycle_bin(user_id):
    return SchemeOperations.get_recycle_list(user_id)


def restore_scheme_from_recycle(user_id, scheme_name, role):
    return SchemeOperations.restore_scheme_from_recycle(user_id, scheme_name, role)


def handle_user_switch(prev_user_id, new_user_id):
    FallbackHandler.handle_user_switch(prev_user_id, new_user_id)


def handle_corrupt_state(user_id):
    return FallbackHandler.handle_corrupt_state(user_id)


def get_last_login_user_id():
    return StatePersistence.get_last_user_id()
