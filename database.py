import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spare_parts.db")


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('operator', 'supervisor')),
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS spare_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_code TEXT UNIQUE NOT NULL,
                part_name TEXT NOT NULL,
                category TEXT NOT NULL,
                specification TEXT,
                unit TEXT NOT NULL DEFAULT '个',
                unit_price REAL NOT NULL DEFAULT 0,
                requires_approval INTEGER NOT NULL DEFAULT 0,
                approval_threshold REAL NOT NULL DEFAULT 0,
                total_stock INTEGER NOT NULL DEFAULT 0,
                available_stock INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS borrow_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_no TEXT UNIQUE NOT NULL,
                part_id INTEGER NOT NULL,
                borrower_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                purpose TEXT,
                status TEXT NOT NULL CHECK(status IN (
                    'pending_approval', 'approved', 'rejected',
                    'borrowed', 'returned', 'rollback', 'cancelled'
                )),
                approver_id INTEGER,
                approval_remark TEXT,
                approval_at TEXT,
                borrow_at TEXT,
                return_quantity INTEGER DEFAULT 0,
                return_at TEXT,
                return_remark TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (part_id) REFERENCES spare_parts(id),
                FOREIGN KEY (borrower_id) REFERENCES users(id),
                FOREIGN KEY (approver_id) REFERENCES users(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stock_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                part_id INTEGER NOT NULL,
                record_id INTEGER,
                operation_type TEXT NOT NULL CHECK(operation_type IN (
                    'init', 'stock_in', 'borrow_approve', 'borrow_reject',
                    'return', 'rollback', 'cancel', 'adjust'
                )),
                quantity_change INTEGER NOT NULL,
                before_available INTEGER NOT NULL,
                after_available INTEGER NOT NULL,
                operator_id INTEGER NOT NULL,
                remark TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (part_id) REFERENCES spare_parts(id),
                FOREIGN KEY (record_id) REFERENCES borrow_records(id),
                FOREIGN KEY (operator_id) REFERENCES users(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT,
                target_id INTEGER,
                detail TEXT,
                success INTEGER NOT NULL DEFAULT 1,
                error_message TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (operator_id) REFERENCES users(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filter_schemes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                scope TEXT NOT NULL DEFAULT 'personal' CHECK(scope IN ('personal', 'shared')),
                filters TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (owner_id) REFERENCES users(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER NOT NULL,
                pref_key TEXT NOT NULL,
                pref_value TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, pref_key),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS export_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_no TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL,
                task_type TEXT NOT NULL DEFAULT 'borrow_records',
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'success', 'failed', 'cancelled')),
                filters_snapshot TEXT NOT NULL,
                sort_snapshot TEXT,
                page_snapshot TEXT,
                columns_snapshot TEXT,
                record_count INTEGER DEFAULT 0,
                export_file_path TEXT,
                export_count INTEGER DEFAULT 0,
                error_message TEXT,
                conflict_task_id INTEGER,
                data_fingerprint TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                expires_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_export_tasks_user ON export_tasks(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_export_tasks_status ON export_tasks(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_export_tasks_type ON export_tasks(task_type)
        """)

        try:
            cursor.execute("ALTER TABLE export_tasks ADD COLUMN export_format TEXT NOT NULL DEFAULT 'csv'")
        except sqlite3.OperationalError:
            pass

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_filter_scheme_owner ON filter_schemes(owner_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_borrow_part ON borrow_records(part_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_borrow_borrower ON borrow_records(borrower_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_borrow_status ON borrow_records(status)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_stocklog_part ON stock_logs(part_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_stocklog_record ON stock_logs(record_id)
        """)


def seed_sample_data():
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            now = datetime.now().isoformat()
            cursor.executemany("""
                INSERT INTO users (username, role, display_name, created_at) VALUES
                (?, ?, ?, ?)
            """, [
                ('admin', 'supervisor', '系统管理员', now),
                ('zhangsan', 'operator', '张三', now),
                ('lisi', 'operator', '李四', now),
                ('wangwu', 'supervisor', '王主管', now),
            ])

        cursor.execute("SELECT COUNT(*) FROM spare_parts")
        if cursor.fetchone()[0] == 0:
            now = datetime.now().isoformat()
            sample_parts = [
                ("SP-001", "CPU处理器", "电子元件", "Intel i7-12700", "个", 2200.0, 1, 1000, 10, 10, "active", now, now),
                ("SP-002", "内存条16GB", "电子元件", "DDR4 3200MHz", "条", 350.0, 1, 500, 50, 50, "active", now, now),
                ("SP-003", "机械硬盘1TB", "存储设备", "7200转 SATA", "块", 280.0, 0, 0, 30, 30, "active", now, now),
                ("SP-004", "固态硬盘512GB", "存储设备", "NVMe M.2", "块", 450.0, 1, 500, 20, 20, "active", now, now),
                ("SP-005", "电源500W", "电源设备", "ATX 80Plus铜牌", "个", 320.0, 0, 0, 15, 15, "active", now, now),
                ("SP-006", "主板", "电子元件", "B660M LGA1700", "块", 890.0, 1, 1000, 8, 8, "active", now, now),
                ("SP-007", "散热风扇", "机械配件", "120mm PWM", "个", 45.0, 0, 0, 100, 100, "active", now, now),
                ("SP-008", "网线Cat6", "网络配件", "3米 千兆", "根", 12.0, 0, 0, 200, 200, "active", now, now),
                ("SP-009", "显卡RTX4060", "电子元件", "8GB GDDR6", "块", 2800.0, 1, 1000, 5, 5, "active", now, now),
                ("SP-010", "键盘", "外设", "机械键盘 青轴", "个", 180.0, 0, 0, 25, 25, "active", now, now),
            ]
            cursor.executemany("""
                INSERT INTO spare_parts (part_code, part_name, category, specification, unit,
                    unit_price, requires_approval, approval_threshold, total_stock, available_stock,
                    status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, sample_parts)

            for row in cursor.execute("SELECT id, available_stock FROM spare_parts").fetchall():
                cursor.execute("""
                    INSERT INTO stock_logs (part_id, operation_type, quantity_change,
                        before_available, after_available, operator_id, remark, created_at)
                    VALUES (?, 'init', ?, 0, ?, 1, '初始化库存', ?)
                """, (row["id"], row["available_stock"], row["available_stock"], now))
