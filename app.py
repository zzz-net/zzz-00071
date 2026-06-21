import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
from database import init_db, seed_sample_data
from services import (
    get_all_users, get_user_by_username, get_all_parts, get_all_categories,
    get_part_by_id, create_part, update_part, adjust_stock, delete_part,
    submit_borrow, approve_borrow, reject_borrow, return_part, rollback_borrow,
    cancel_borrow, undo_return, get_borrow_records, get_borrow_record, get_stock_logs,
    get_operation_logs, STATUS_DISPLAY, OPERATION_DISPLAY, BusinessException,
    save_filter_scheme, get_filter_schemes, delete_filter_scheme, get_filter_scheme_by_id,
    set_active_scheme_id, get_active_scheme_id, _is_filter_empty
)
from exporter import (
    export_stock_details, export_borrow_records, export_stock_logs,
    generate_default_filename
)
from scheme_coordinator import (
    restore_workbench_state, save_last_filters, get_last_filters,
    log_query_operation, log_export_operation, verify_export_consistency,
    activate_scheme, deactivate_scheme, delete_scheme_and_cleanup,
    get_available_schemes, _can_access_scheme, RestoreResult,
    rename_scheme, get_recycle_bin, restore_scheme_from_recycle,
    handle_user_switch, get_last_login_user_id, save_workbench_full_state,
    load_workbench_full_state, WorkbenchState, handle_corrupt_state
)


class LoginDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("登录 - 维修备件借还系统")
        self.geometry("380x280")
        self.resizable(False, False)
        self.result = None
        self.users = get_all_users()
        self._build_ui()
        self.grab_set()
        self.transient(master)

    def _build_ui(self):
        frame = ttk.Frame(self, padding=30)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="维修备件借还系统", font=("Microsoft YaHei", 16, "bold")).pack(pady=(0, 20))
        ttk.Label(frame, text="选择登录用户:", font=("Microsoft YaHei", 10)).pack(anchor="w", pady=(0, 5))
        self.user_var = tk.StringVar()
        self.combo = ttk.Combobox(
            frame, textvariable=self.user_var, state="readonly",
            values=[f"{u['display_name']} ({u['username']} - {'主管' if u['role']=='supervisor' else '操作员'})"
                    for u in self.users]
        )
        self.combo.pack(fill="x", pady=(0, 15))
        if self.users:
            self.combo.current(0)
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(btn_frame, text="登录", command=self._on_login, width=12).pack(side="left", padx=(0, 10))
        ttk.Button(btn_frame, text="退出", command=self._on_cancel, width=12).pack(side="left")

    def _on_login(self):
        idx = self.combo.current()
        if idx >= 0:
            self.result = self.users[idx]
            self.destroy()
        else:
            messagebox.showwarning("提示", "请选择登录用户", parent=self)

    def _on_cancel(self):
        self.result = None
        self.destroy()


class PartDialog(tk.Toplevel):
    def __init__(self, master, part_data=None):
        super().__init__(master)
        self.title("编辑备件" if part_data else "新增备件")
        self.geometry("480x520")
        self.resizable(False, False)
        self.part_data = part_data
        self.result = None
        self._build_ui()
        self.grab_set()
        self.transient(master)

    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        fields = [
            ("part_code", "备件编码", True),
            ("part_name", "备件名称", True),
            ("category", "分类", True),
            ("specification", "规格型号", False),
            ("unit", "单位", True),
            ("unit_price", "单价(元)", True),
            ("requires_approval", "是否需审批", False),
            ("approval_threshold", "审批阈值(元)", True),
            ("total_stock", "初始库存", True),
        ]
        self.vars = {}
        for i, (key, label, required) in enumerate(fields):
            ttk.Label(frame, text=f"{label}{'*' if required else ''}:").grid(
                row=i, column=0, sticky="w", pady=6, padx=(0, 10)
            )
            if key == "requires_approval":
                var = tk.IntVar(value=1 if self.part_data and self.part_data.get(key) else 0)
                cb = ttk.Checkbutton(frame, variable=var, text="启用")
                cb.grid(row=i, column=1, sticky="w", pady=6)
            else:
                var = tk.StringVar(value=str(self.part_data.get(key, "")) if self.part_data else "")
                entry = ttk.Entry(frame, textvariable=var, width=35)
                entry.grid(row=i, column=1, sticky="w", pady=6)
                if self.part_data and key == "total_stock":
                    entry.configure(state="disabled")
            self.vars[key] = var
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=len(fields), column=0, columnspan=2, pady=20)
        ttk.Button(btn_frame, text="确定", command=self._on_ok, width=12).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="取消", command=self.destroy, width=12).pack(side="left", padx=5)

    def _on_ok(self):
        try:
            data = {}
            for key, var in self.vars.items():
                if key == "requires_approval":
                    data[key] = var.get()
                else:
                    data[key] = var.get().strip()
            if not data["part_code"]:
                raise BusinessException("请输入备件编码")
            if not data["part_name"]:
                raise BusinessException("请输入备件名称")
            if not data["category"]:
                raise BusinessException("请输入分类")
            data["unit_price"] = float(data["unit_price"] or 0)
            data["approval_threshold"] = float(data["approval_threshold"] or 0)
            if not self.part_data:
                data["total_stock"] = int(data["total_stock"] or 0)
            self.result = data
            self.destroy()
        except ValueError:
            messagebox.showerror("错误", "数字格式不正确", parent=self)
        except BusinessException as e:
            messagebox.showerror("错误", e.message, parent=self)


class BorrowDialog(tk.Toplevel):
    def __init__(self, master, part, current_user):
        super().__init__(master)
        self.title(f"借用 - {part['part_name']}")
        self.geometry("420x360")
        self.resizable(False, False)
        self.part = part
        self.current_user = current_user
        self.result = None
        self._build_ui()
        self.grab_set()
        self.transient(master)

    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        info_frame = ttk.LabelFrame(frame, text="备件信息", padding=10)
        info_frame.pack(fill="x", pady=(0, 15))
        ttk.Label(info_frame, text=f"编码: {self.part['part_code']}").grid(row=0, column=0, sticky="w")
        ttk.Label(info_frame, text=f"名称: {self.part['part_name']}").grid(row=0, column=1, sticky="w", padx=20)
        ttk.Label(info_frame, text=f"可用: {self.part['available_stock']}{self.part['unit']}").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Label(info_frame, text=f"单价: {self.part['unit_price']}元").grid(row=1, column=1, sticky="w", padx=20, pady=5)
        need_approval = bool(self.part["requires_approval"]) or (self.part["unit_price"] > 0 and self.part["approval_threshold"] > 0)
        approval_text = "需主管审批" if need_approval and self.current_user["role"] != "supervisor" else "可直接借出"
        ttk.Label(info_frame, text=f"审批: {approval_text}", foreground="#E6A23C" if "需审批" in approval_text else "#67C23A").grid(row=2, column=0, columnspan=2, sticky="w")
        form_frame = ttk.Frame(frame)
        form_frame.pack(fill="x")
        ttk.Label(form_frame, text="借用数量*:").grid(row=0, column=0, sticky="w", pady=8)
        self.qty_var = tk.StringVar(value="1")
        ttk.Spinbox(form_frame, from_=1, to=self.part["available_stock"], textvariable=self.qty_var, width=20).grid(row=0, column=1, sticky="w", pady=8)
        ttk.Label(form_frame, text="用途说明:").grid(row=1, column=0, sticky="nw", pady=8)
        self.purpose_text = tk.Text(form_frame, width=30, height=4)
        self.purpose_text.grid(row=1, column=1, sticky="w", pady=8)
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x", pady=20)
        ttk.Button(btn_frame, text="提交", command=self._on_ok, width=12).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="取消", command=self.destroy, width=12).pack(side="left", padx=5)

    def _on_ok(self):
        try:
            qty = int(self.qty_var.get())
            purpose = self.purpose_text.get("1.0", "end").strip()
            self.result = {"quantity": qty, "purpose": purpose}
            self.destroy()
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数量", parent=self)


class ApprovalDialog(tk.Toplevel):
    def __init__(self, master, record, action):
        super().__init__(master)
        self.title(f"{'审批通过' if action == 'approve' else '审批驳回'} - {record['record_no']}")
        self.geometry("460x420")
        self.resizable(False, False)
        self.record = record
        self.action = action
        self.result = None
        self._build_ui()
        self.grab_set()
        self.transient(master)

    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        info_frame = ttk.LabelFrame(frame, text="借用记录", padding=10)
        info_frame.pack(fill="x", pady=(0, 15))
        info = [
            ("记录编号", self.record["record_no"]),
            ("备件编码", self.record["part_code"]),
            ("备件名称", self.record["part_name"]),
            ("借用数量", f"{self.record['quantity']}{self.record['unit']}"),
            ("借用人", self.record["borrower_name"]),
            ("用途", self.record.get("purpose", "") or ""),
            ("总金额", f"{self.record['quantity'] * self.record['unit_price']:.2f}元"),
        ]
        for i, (k, v) in enumerate(info):
            ttk.Label(info_frame, text=f"{k}:").grid(row=i, column=0, sticky="w", pady=3)
            ttk.Label(info_frame, text=str(v)).grid(row=i, column=1, sticky="w", padx=15, pady=3)
        ttk.Label(frame, text="审批备注:").pack(anchor="w")
        self.remark_text = tk.Text(frame, width=48, height=5)
        self.remark_text.pack(fill="x", pady=8)
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="确认", command=self._on_ok, width=12).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="取消", command=self.destroy, width=12).pack(side="left", padx=5)

    def _on_ok(self):
        remark = self.remark_text.get("1.0", "end").strip()
        self.result = {"remark": remark}
        self.destroy()


class ReturnDialog(tk.Toplevel):
    def __init__(self, master, record):
        super().__init__(master)
        self.title(f"归还 - {record['record_no']}")
        self.geometry("420x380")
        self.resizable(False, False)
        self.record = record
        self.result = None
        self._build_ui()
        self.grab_set()
        self.transient(master)

    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        remaining = self.record["quantity"] - self.record["return_quantity"]
        info_frame = ttk.LabelFrame(frame, text="借用记录", padding=10)
        info_frame.pack(fill="x", pady=(0, 15))
        info = [
            ("备件编码", self.record["part_code"]),
            ("备件名称", self.record["part_name"]),
            ("借用总数", f"{self.record['quantity']}{self.record['unit']}"),
            ("已归还", f"{self.record['return_quantity']}{self.record['unit']}"),
            ("未归还", f"{remaining}{self.record['unit']}"),
            ("借用人", self.record["borrower_name"]),
        ]
        for i, (k, v) in enumerate(info):
            ttk.Label(info_frame, text=f"{k}:").grid(row=i, column=0, sticky="w", pady=3)
            ttk.Label(info_frame, text=str(v)).grid(row=i, column=1, sticky="w", padx=15, pady=3)
        ttk.Label(frame, text="归还数量*:").grid(row=0, column=0, sticky="w", pady=8)
        self.qty_var = tk.StringVar(value=str(remaining))
        ttk.Spinbox(frame, from_=1, to=remaining, textvariable=self.qty_var, width=20).grid(row=0, column=1, sticky="w", pady=8)
        ttk.Label(frame, text="归还备注:").grid(row=1, column=0, sticky="nw", pady=8)
        self.remark_text = tk.Text(frame, width=35, height=4)
        self.remark_text.grid(row=1, column=1, sticky="w", pady=8)
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=15)
        ttk.Button(btn_frame, text="确认归还", command=self._on_ok, width=12).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="取消", command=self.destroy, width=12).pack(side="left", padx=5)

    def _on_ok(self):
        try:
            qty = int(self.qty_var.get())
            remark = self.remark_text.get("1.0", "end").strip()
            self.result = {"quantity": qty, "remark": remark}
            self.destroy()
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数量", parent=self)


class StockAdjustDialog(tk.Toplevel):
    def __init__(self, master, part):
        super().__init__(master)
        self.title(f"库存调整 - {part['part_name']}")
        self.geometry("400x300")
        self.resizable(False, False)
        self.part = part
        self.result = None
        self._build_ui()
        self.grab_set()
        self.transient(master)

    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"当前可用库存: {self.part['available_stock']}{self.part['unit']}",
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=(0, 15))
        ttk.Label(frame, text="调整数量(正数增加,负数减少):").pack(anchor="w")
        self.qty_var = tk.StringVar(value="0")
        ttk.Spinbox(frame, from_=-9999, to=9999, textvariable=self.qty_var, width=25).pack(fill="x", pady=8)
        ttk.Label(frame, text="调整备注:").pack(anchor="w")
        self.remark_text = tk.Text(frame, width=40, height=4)
        self.remark_text.pack(fill="x", pady=8)
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="确认调整", command=self._on_ok, width=12).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="取消", command=self.destroy, width=12).pack(side="left", padx=5)

    def _on_ok(self):
        try:
            qty = int(self.qty_var.get())
            remark = self.remark_text.get("1.0", "end").strip()
            self.result = {"quantity": qty, "remark": remark}
            self.destroy()
        except ValueError:
            messagebox.showerror("错误", "请输入有效的数量", parent=self)


class SaveSchemeDialog(tk.Toplevel):
    def __init__(self, master, current_user, active_scheme_id=None):
        super().__init__(master)
        self.title("保存筛选方案")
        self.geometry("400x250")
        self.resizable(False, False)
        self.current_user = current_user
        self.active_scheme_id = active_scheme_id
        self.result = None
        self._build_ui()
        self.grab_set()
        self.transient(master)

    def _build_ui(self):
        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="方案名称*:").grid(row=0, column=0, sticky="w", pady=8)
        self.name_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.name_var, width=30).grid(row=0, column=1, sticky="w", pady=8)
        ttk.Label(frame, text="可见范围:").grid(row=1, column=0, sticky="w", pady=8)
        self.scope_var = tk.StringVar(value="personal")
        scope_frame = ttk.Frame(frame)
        scope_frame.grid(row=1, column=1, sticky="w", pady=8)
        is_supervisor = self.current_user["role"] == "supervisor"
        personal_rb = ttk.Radiobutton(scope_frame, text="仅自己", variable=self.scope_var, value="personal")
        personal_rb.pack(side="left")
        shared_rb = ttk.Radiobutton(scope_frame, text="共享(所有人可见)", variable=self.scope_var,
                                     value="shared")
        shared_rb.pack(side="left", padx=10)
        if not is_supervisor:
            shared_rb.configure(state="disabled")
            self.scope_var.set("personal")
        if self.active_scheme_id:
            scheme = get_filter_scheme_by_id(self.active_scheme_id)
            if scheme:
                self.name_var.set(scheme["name"])
                self.scope_var.set(scheme["scope"])
                if not is_supervisor:
                    self.scope_var.set("personal")
        hint_label = ttk.Label(frame, text="", foreground="#909399", font=("Microsoft YaHei", 8))
        hint_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 5))
        if not is_supervisor:
            hint_label.configure(text="提示: 仅主管可创建共享方案")
        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=3, column=0, columnspan=2, pady=15)
        ttk.Button(btn_frame, text="保存", command=self._on_ok, width=12).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="取消", command=self.destroy, width=12).pack(side="left", padx=5)

    def _on_ok(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "方案名称不能为空", parent=self)
            return
        self.result = {
            "name": name,
            "scope": self.scope_var.get(),
            "scheme_id": self.active_scheme_id
        }
        self.destroy()


class MainApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("维修备件借还管理系统")
        self.geometry("1280x800")
        self.minsize(1100, 700)
        self.current_user = None
        self._setup_style()
        self._build_ui()
        self._show_login()

    def _setup_style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Treeview", font=("Microsoft YaHei", 9), rowheight=26)
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 9, "bold"))
        style.configure("TNotebook.Tab", font=("Microsoft YaHei", 10), padding=[15, 8])
        style.configure("TButton", font=("Microsoft YaHei", 9))
        style.configure("TLabel", font=("Microsoft YaHei", 9))
        style.configure("Header.TLabel", font=("Microsoft YaHei", 11, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei", 9))

    def _build_ui(self):
        self.header = ttk.Frame(self, padding=(15, 10))
        self.header.pack(fill="x")
        self.title_label = ttk.Label(self.header, text="维修备件借还管理系统", style="Header.TLabel")
        self.title_label.pack(side="left")
        self.user_info = ttk.Label(self.header, text="", style="Status.TLabel")
        self.user_info.pack(side="right")
        self.logout_btn = ttk.Button(self.header, text="切换用户", command=self._show_login, width=10)
        self.logout_btn.pack(side="right", padx=10)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        self._build_workbench_tab()
        self._build_parts_tab()
        self._build_borrow_tab()
        self._build_approval_tab()
        self._build_history_tab()
        self._build_logs_tab()

        self.status = ttk.Frame(self, padding=(15, 5), relief="sunken")
        self.status.pack(fill="x")
        self.status_label = ttk.Label(self.status, text="就绪", style="Status.TLabel")
        self.status_label.pack(side="left")
        self.restore_hint_label = ttk.Label(self.status, text="", style="Status.TLabel", foreground="#E6A23C")
        self.restore_hint_label.pack(side="right")

    def _build_workbench_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="恢复工作台")

        info_frame = ttk.LabelFrame(tab, text="工作台说明", padding=8)
        info_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(info_frame, text="本工作台整合了条件恢复、方案管理、结果重查和导出校验的完整链路。\n"
                                   "登录或重启程序后，将自动按上次生效条件、排序、分页和选中方案直接查询列表。",
                  wraplength=1000, foreground="#606266").pack(anchor="w")

        scheme_frame = ttk.LabelFrame(tab, text="方案管理", padding=8)
        scheme_frame.pack(fill="x", pady=(0, 8))

        row1 = ttk.Frame(scheme_frame)
        row1.pack(fill="x", pady=3)
        ttk.Label(row1, text="当前方案:", width=10).pack(side="left")
        self.wb_active_scheme_label = ttk.Label(row1, text="未激活", foreground="#909399")
        self.wb_active_scheme_label.pack(side="left", padx=5)

        row2 = ttk.Frame(scheme_frame)
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="选择方案:", width=10).pack(side="left")
        self.wb_scheme_var = tk.StringVar()
        self.wb_scheme_combo = ttk.Combobox(row2, textvariable=self.wb_scheme_var,
                                            state="readonly", width=35)
        self.wb_scheme_combo.pack(side="left", padx=5)
        self.wb_scheme_combo.bind("<<ComboboxSelected>>", self._on_wb_scheme_selected)
        ttk.Button(row2, text="激活方案", command=self._wb_activate_scheme, width=10).pack(side="left", padx=3)
        ttk.Button(row2, text="保存当前条件为方案", command=self._wb_save_scheme, width=16).pack(side="left", padx=3)
        ttk.Button(row2, text="重命名", command=self._wb_rename_scheme, width=8).pack(side="left", padx=3)
        ttk.Button(row2, text="删除方案", command=self._wb_delete_scheme, width=10).pack(side="left", padx=3)
        ttk.Button(row2, text="回收站", command=self._wb_show_recycle_bin, width=8).pack(side="left", padx=3)

        filter_frame = ttk.LabelFrame(tab, text="筛选条件（恢复状态后可直接修改并重查）", padding=8)
        filter_frame.pack(fill="x", pady=(0, 8))

        frow1 = ttk.Frame(filter_frame)
        frow1.pack(fill="x", pady=3)
        ttk.Label(frow1, text="状态:").pack(side="left")
        self.wb_status = tk.StringVar()
        wb_statuses = [("全部", ""), ("待审批", "pending_approval"), ("已借出", "approved"),
                       ("已借出(部分归还)", "borrowed"), ("已归还", "returned"), ("已驳回", "rejected"),
                       ("已回滚", "rollback"), ("已撤销", "cancelled")]
        self.wb_status_combo = ttk.Combobox(
            frow1, textvariable=self.wb_status, state="readonly", width=16,
            values=[s[0] for s in wb_statuses]
        )
        self.wb_status_combo.current(0)
        self.wb_status_combo.pack(side="left", padx=5)
        self._wb_status_map = {s[0]: s[1] for s in wb_statuses}
        ttk.Label(frow1, text="借用人:").pack(side="left", padx=(10, 0))
        self.wb_borrower = tk.StringVar()
        self.wb_borrower_combo = ttk.Combobox(frow1, textvariable=self.wb_borrower,
                                               state="readonly", width=14)
        self.wb_borrower_combo.pack(side="left", padx=5)
        self._wb_borrower_map = {}
        ttk.Label(frow1, text="关键字:").pack(side="left", padx=(10, 0))
        self.wb_keyword = tk.StringVar()
        ttk.Entry(frow1, textvariable=self.wb_keyword, width=22).pack(side="left", padx=5)

        frow2 = ttk.Frame(filter_frame)
        frow2.pack(fill="x", pady=3)
        ttk.Label(frow2, text="开始日期:").pack(side="left")
        self.wb_date_from = tk.StringVar()
        ttk.Entry(frow2, textvariable=self.wb_date_from, width=14).pack(side="left", padx=5)
        ttk.Label(frow2, text="(如 2025-01-01)", foreground="#909399").pack(side="left")
        ttk.Label(frow2, text="结束日期:").pack(side="left", padx=(15, 0))
        self.wb_date_to = tk.StringVar()
        ttk.Entry(frow2, textvariable=self.wb_date_to, width=14).pack(side="left", padx=5)
        ttk.Label(frow2, text="(如 2025-12-31)", foreground="#909399").pack(side="left")
        ttk.Button(frow2, text="查询", command=self._wb_refresh_records, width=8).pack(side="left", padx=10)
        ttk.Button(frow2, text="重置条件", command=self._wb_reset_filters, width=10).pack(side="left", padx=2)

        result_frame = ttk.LabelFrame(tab, text="查询结果（点击列头可排序，底部可翻页）", padding=8)
        result_frame.pack(fill="both", expand=True, pady=(0, 8))

        btn_row = ttk.Frame(result_frame)
        btn_row.pack(fill="x", pady=(0, 5))
        ttk.Label(btn_row, text="记录计数:").pack(side="left")
        self.wb_count_label = ttk.Label(btn_row, text="0 条", foreground="#409EFF", font=("Microsoft YaHei", 9, "bold"))
        self.wb_count_label.pack(side="left", padx=5)
        ttk.Label(btn_row, text="| 排序:").pack(side="left", padx=10)
        self.wb_sort_label = ttk.Label(btn_row, text="创建时间↓", foreground="#909399")
        self.wb_sort_label.pack(side="left", padx=3)
        ttk.Separator(btn_row, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btn_row, text="导出CSV", command=self._wb_export_csv, width=12).pack(side="left", padx=3)
        ttk.Button(btn_row, text="校验导出一致性", command=self._wb_verify_export, width=16).pack(side="left", padx=3)
        self.wb_verify_result_label = ttk.Label(btn_row, text="", foreground="#67C23A")
        self.wb_verify_result_label.pack(side="left", padx=10)

        wb_columns = ("record_no", "part_code", "part_name", "quantity", "unit", "borrower",
                      "status", "created_at")
        self.wb_tree = ttk.Treeview(result_frame, columns=wb_columns, show="headings", selectmode="browse")
        self._wb_col_sort_map = {
            "record_no": "record_no",
            "part_code": "part_code",
            "part_name": "part_name",
            "quantity": "quantity",
            "unit": "unit",
            "borrower": "borrower_name",
            "status": "status",
            "created_at": "created_at",
        }
        self._wb_col_text_map = {
            "record_no": "记录编号",
            "part_code": "备件编码",
            "part_name": "备件名称",
            "quantity": "数量",
            "unit": "单位",
            "borrower": "借用人",
            "status": "状态",
            "created_at": "创建时间",
        }
        wb_headers = [
            ("record_no", "记录编号", 140),
            ("part_code", "备件编码", 85),
            ("part_name", "备件名称", 130),
            ("quantity", "数量", 55),
            ("unit", "单位", 45),
            ("borrower", "借用人", 75),
            ("status", "状态", 75),
            ("created_at", "创建时间", 140),
        ]
        for col, text, width in wb_headers:
            self.wb_tree.heading(col, text=text,
                                 command=lambda c=col: self._wb_on_column_click(c))
            self.wb_tree.column(col, width=width, anchor="center")
        self.wb_tree.tag_configure("pending_approval", background="#FDF6EC")
        self.wb_tree.tag_configure("approved", background="#ECF5FF")
        self.wb_tree.tag_configure("rejected", background="#FEF0F0")
        self.wb_tree.tag_configure("returned", background="#F0F9EB")
        self.wb_tree.tag_configure("rollback", background="#F4F4F5")
        self.wb_tree.tag_configure("cancelled", background="#F4F4F5")
        self.wb_tree.tag_configure("borrowed", background="#ECF5FF")
        wb_vsb = ttk.Scrollbar(result_frame, orient="vertical", command=self.wb_tree.yview)
        self.wb_tree.configure(yscrollcommand=wb_vsb.set)
        self.wb_tree.pack(side="left", fill="both", expand=True)
        wb_vsb.pack(side="right", fill="y")

        pager_frame = ttk.Frame(result_frame)
        pager_frame.pack(fill="x", pady=(5, 0))
        ttk.Label(pager_frame, text="每页数量:").pack(side="left")
        self.wb_page_size_var = tk.IntVar(value=20)
        self.wb_page_size_combo = ttk.Combobox(
            pager_frame, textvariable=self.wb_page_size_var, state="readonly", width=6,
            values=[10, 20, 50, 100, 200, 500]
        )
        self.wb_page_size_combo.pack(side="left", padx=3)
        self.wb_page_size_combo.bind("<<ComboboxSelected>>", lambda e: self._wb_on_page_size_change())
        ttk.Separator(pager_frame, orient="vertical").pack(side="left", fill="y", padx=8)
        self.wb_btn_first = ttk.Button(pager_frame, text="首页", width=6,
                                        command=lambda: self._wb_goto_page(1))
        self.wb_btn_first.pack(side="left", padx=1)
        self.wb_btn_prev = ttk.Button(pager_frame, text="上一页", width=6,
                                       command=lambda: self._wb_goto_page(self.wb_current_page - 1))
        self.wb_btn_prev.pack(side="left", padx=1)
        ttk.Label(pager_frame, text="第").pack(side="left", padx=(5, 0))
        self.wb_page_var = tk.IntVar(value=1)
        self.wb_page_spinbox = ttk.Spinbox(
            pager_frame, from_=1, to=1, textvariable=self.wb_page_var, width=5,
            command=self._wb_on_page_spin_change
        )
        self.wb_page_spinbox.pack(side="left", padx=2)
        self.wb_page_spinbox.bind("<Return>", lambda e: self._wb_on_page_spin_change())
        self.wb_total_pages_label = ttk.Label(pager_frame, text="页 / 共 1 页")
        self.wb_total_pages_label.pack(side="left", padx=2)
        self.wb_btn_next = ttk.Button(pager_frame, text="下一页", width=6,
                                       command=lambda: self._wb_goto_page(self.wb_current_page + 1))
        self.wb_btn_next.pack(side="left", padx=1)
        self.wb_btn_last = ttk.Button(pager_frame, text="末页", width=6,
                                       command=lambda: self._wb_goto_page(self.wb_total_pages))
        self.wb_btn_last.pack(side="left", padx=1)
        ttk.Separator(pager_frame, orient="vertical").pack(side="left", fill="y", padx=8)
        self.wb_pager_info = ttk.Label(pager_frame, text="", foreground="#909399")
        self.wb_pager_info.pack(side="left", padx=5)

        self._wb_current_page = 1
        self._wb_total_pages = 1
        self._wb_total_count = 0
        self._wb_all_records = []
        self.wb_sort_by = "created_at"
        self.wb_sort_order = "desc"

        self._wb_last_export_path = None
        self._wb_last_export_filters = None

    def _build_parts_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="备件库存")

        filter_frame = ttk.Frame(tab)
        filter_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_frame, text="搜索:").pack(side="left")
        self.parts_keyword = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.parts_keyword, width=25).pack(side="left", padx=5)
        ttk.Label(filter_frame, text="分类:").pack(side="left", padx=(10, 0))
        self.parts_category = tk.StringVar()
        self.parts_category_combo = ttk.Combobox(filter_frame, textvariable=self.parts_category,
                                                  state="readonly", width=15)
        self.parts_category_combo.pack(side="left", padx=5)
        ttk.Button(filter_frame, text="查询", command=self._refresh_parts, width=8).pack(side="left", padx=5)
        ttk.Button(filter_frame, text="重置", command=self._reset_parts_filter, width=8).pack(side="left", padx=5)

        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill="x", pady=(0, 8))
        self.btn_add_part = ttk.Button(btn_frame, text="新增备件", command=self._add_part, width=10)
        self.btn_add_part.pack(side="left", padx=3)
        self.btn_edit_part = ttk.Button(btn_frame, text="编辑备件", command=self._edit_part, width=10)
        self.btn_edit_part.pack(side="left", padx=3)
        self.btn_adjust = ttk.Button(btn_frame, text="库存调整", command=self._adjust_stock, width=10)
        self.btn_adjust.pack(side="left", padx=3)
        self.btn_delete_part = ttk.Button(btn_frame, text="停用备件", command=self._delete_part, width=10)
        self.btn_delete_part.pack(side="left", padx=3)
        self.btn_borrow = ttk.Button(btn_frame, text="提交借用", command=self._submit_borrow, width=10)
        self.btn_borrow.pack(side="left", padx=3)
        ttk.Separator(btn_frame, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btn_frame, text="导出库存明细", command=self._export_stock, width=14).pack(side="left", padx=3)

        columns = ("part_code", "part_name", "category", "specification", "unit",
                   "unit_price", "approval", "available", "pending", "borrowed", "total", "status")
        self.parts_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headers = [
            ("part_code", "备件编码", 100),
            ("part_name", "备件名称", 150),
            ("category", "分类", 100),
            ("specification", "规格型号", 180),
            ("unit", "单位", 55),
            ("unit_price", "单价(元)", 80),
            ("approval", "审批", 60),
            ("available", "可用库存", 80),
            ("pending", "待审批", 70),
            ("borrowed", "已借出", 70),
            ("total", "总库存", 70),
            ("status", "状态", 90),
        ]
        for col, text, width in headers:
            self.parts_tree.heading(col, text=text)
            self.parts_tree.column(col, width=width, anchor="center")
        self.parts_tree.tag_configure("available", background="#F0F9EB")
        self.parts_tree.tag_configure("pending", background="#FDF6EC")
        self.parts_tree.tag_configure("empty", background="#FEF0F0")
        self.parts_tree.tag_configure("normal", background="white")
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.parts_tree.yview)
        self.parts_tree.configure(yscrollcommand=vsb.set)
        self.parts_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.parts_tree.bind("<Double-1>", lambda e: self._edit_part())

    def _build_borrow_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="借还记录")

        scheme_frame = ttk.LabelFrame(tab, text="筛选方案", padding=5)
        scheme_frame.pack(fill="x", pady=(0, 5))
        ttk.Label(scheme_frame, text="方案:").pack(side="left")
        self.scheme_var = tk.StringVar()
        self.scheme_combo = ttk.Combobox(scheme_frame, textvariable=self.scheme_var,
                                          state="readonly", width=18)
        self.scheme_combo.pack(side="left", padx=5)
        self.scheme_combo.bind("<<ComboboxSelected>>", self._on_scheme_selected)
        ttk.Button(scheme_frame, text="套用", command=self._apply_scheme, width=6).pack(side="left", padx=2)
        ttk.Button(scheme_frame, text="保存当前条件", command=self._save_scheme, width=12).pack(side="left", padx=2)
        ttk.Button(scheme_frame, text="删除方案", command=self._delete_scheme, width=8).pack(side="left", padx=2)
        self.active_scheme_id = None
        self.active_scheme_label = ttk.Label(scheme_frame, text="", foreground="#409EFF")
        self.active_scheme_label.pack(side="left", padx=10)

        filter_frame = ttk.LabelFrame(tab, text="筛选条件", padding=5)
        filter_frame.pack(fill="x", pady=(0, 5))

        row1 = ttk.Frame(filter_frame)
        row1.pack(fill="x", pady=2)
        ttk.Label(row1, text="状态:").pack(side="left")
        self.borrow_status = tk.StringVar()
        statuses = [("全部", ""), ("待审批", "pending_approval"), ("已借出", "approved"),
                    ("已借出(部分归还)", "borrowed"), ("已归还", "returned"), ("已驳回", "rejected"),
                    ("已回滚", "rollback"), ("已撤销", "cancelled")]
        self.borrow_status_combo = ttk.Combobox(
            row1, textvariable=self.borrow_status, state="readonly", width=14,
            values=[s[0] for s in statuses]
        )
        self.borrow_status_combo.current(0)
        self.borrow_status_combo.pack(side="left", padx=5)
        self._status_map = {s[0]: s[1] for s in statuses}
        ttk.Label(row1, text="借用人:").pack(side="left", padx=(10, 0))
        self.borrow_person = tk.StringVar()
        self.borrow_person_combo = ttk.Combobox(row1, textvariable=self.borrow_person,
                                                 state="readonly", width=12)
        self.borrow_person_combo.pack(side="left", padx=5)
        self._borrower_map = {}
        ttk.Label(row1, text="关键字:").pack(side="left", padx=(10, 0))
        self.borrow_keyword = tk.StringVar()
        ttk.Entry(row1, textvariable=self.borrow_keyword, width=20).pack(side="left", padx=5)

        row2 = ttk.Frame(filter_frame)
        row2.pack(fill="x", pady=2)
        ttk.Label(row2, text="开始日期:").pack(side="left")
        self.date_from_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.date_from_var, width=14).pack(side="left", padx=5)
        ttk.Label(row2, text="(如 2025-01-01)", foreground="#909399").pack(side="left")
        ttk.Label(row2, text="结束日期:").pack(side="left", padx=(15, 0))
        self.date_to_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.date_to_var, width=14).pack(side="left", padx=5)
        ttk.Label(row2, text="(如 2025-12-31)", foreground="#909399").pack(side="left")
        ttk.Button(row2, text="查询", command=self._refresh_borrow, width=8).pack(side="left", padx=10)
        ttk.Button(row2, text="重置", command=self._reset_borrow_filter, width=8).pack(side="left", padx=2)

        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill="x", pady=(0, 8))
        self.btn_return = ttk.Button(btn_frame, text="归还", command=self._return_part, width=10)
        self.btn_return.pack(side="left", padx=3)
        self.btn_undo_return = ttk.Button(btn_frame, text="撤销归还", command=self._undo_return_record, width=10)
        self.btn_undo_return.pack(side="left", padx=3)
        self.btn_rollback = ttk.Button(btn_frame, text="异常回滚", command=self._rollback_record, width=10)
        self.btn_rollback.pack(side="left", padx=3)
        self.btn_cancel = ttk.Button(btn_frame, text="撤销申请", command=self._cancel_record, width=10)
        self.btn_cancel.pack(side="left", padx=3)
        ttk.Separator(btn_frame, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btn_frame, text="按当前条件导出CSV", command=self._export_borrow, width=18).pack(side="left", padx=3)

        columns = ("record_no", "part_code", "part_name", "quantity", "unit", "borrower",
                   "purpose", "status", "approver", "created_at", "return_qty")
        self.borrow_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headers = [
            ("record_no", "记录编号", 155),
            ("part_code", "备件编码", 90),
            ("part_name", "备件名称", 130),
            ("quantity", "数量", 60),
            ("unit", "单位", 50),
            ("borrower", "借用人", 80),
            ("purpose", "用途", 180),
            ("status", "状态", 80),
            ("approver", "审批人", 80),
            ("created_at", "创建时间", 150),
            ("return_qty", "已归还", 70),
        ]
        for col, text, width in headers:
            self.borrow_tree.heading(col, text=text)
            self.borrow_tree.column(col, width=width, anchor="center")
        self.borrow_tree.tag_configure("pending_approval", background="#FDF6EC")
        self.borrow_tree.tag_configure("approved", background="#ECF5FF")
        self.borrow_tree.tag_configure("rejected", background="#FEF0F0")
        self.borrow_tree.tag_configure("returned", background="#F0F9EB")
        self.borrow_tree.tag_configure("rollback", background="#F4F4F5")
        self.borrow_tree.tag_configure("cancelled", background="#F4F4F5")
        self.borrow_tree.tag_configure("borrowed", background="#ECF5FF")
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.borrow_tree.yview)
        self.borrow_tree.configure(yscrollcommand=vsb.set)
        self.borrow_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _build_approval_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="审批管理")

        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill="x", pady=(0, 8))
        self.btn_approve = ttk.Button(btn_frame, text="审批通过", command=self._approve_record, width=12)
        self.btn_approve.pack(side="left", padx=3)
        self.btn_reject = ttk.Button(btn_frame, text="审批驳回", command=self._reject_record, width=12)
        self.btn_reject.pack(side="left", padx=3)
        ttk.Label(btn_frame, text="   提示: 仅主管用户可执行审批操作", foreground="#909399").pack(side="left", padx=10)

        columns = ("record_no", "part_code", "part_name", "quantity", "unit", "amount",
                   "borrower", "purpose", "created_at")
        self.approval_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headers = [
            ("record_no", "记录编号", 155),
            ("part_code", "备件编码", 90),
            ("part_name", "备件名称", 130),
            ("quantity", "数量", 60),
            ("unit", "单位", 50),
            ("amount", "总金额(元)", 90),
            ("borrower", "借用人", 80),
            ("purpose", "用途", 250),
            ("created_at", "创建时间", 150),
        ]
        for col, text, width in headers:
            self.approval_tree.heading(col, text=text)
            self.approval_tree.column(col, width=width, anchor="center")
        self.approval_tree.tag_configure("pending", background="#FDF6EC")
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.approval_tree.yview)
        self.approval_tree.configure(yscrollcommand=vsb.set)
        self.approval_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _build_history_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="库存变动历史")

        filter_frame = ttk.Frame(tab)
        filter_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_frame, text="备件:").pack(side="left")
        self.history_part = tk.StringVar()
        parts_list = ["全部"]
        self.history_part_map = {"全部": None}
        for p in get_all_parts():
            label = f"{p['part_code']} {p['part_name']}"
            parts_list.append(label)
            self.history_part_map[label] = p["id"]
        self.history_combo = ttk.Combobox(filter_frame, textvariable=self.history_part,
                                           state="readonly", values=parts_list, width=30)
        self.history_combo.current(0)
        self.history_combo.pack(side="left", padx=5)
        ttk.Button(filter_frame, text="查询", command=self._refresh_history, width=8).pack(side="left", padx=5)
        ttk.Button(filter_frame, text="导出", command=self._export_history, width=8).pack(side="left", padx=5)

        columns = ("id", "created_at", "part_code", "part_name", "operation",
                   "change", "before", "after", "operator", "remark")
        self.history_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headers = [
            ("id", "ID", 60),
            ("created_at", "时间", 150),
            ("part_code", "备件编码", 90),
            ("part_name", "备件名称", 130),
            ("operation", "操作类型", 90),
            ("change", "库存变动", 80),
            ("before", "变动前", 70),
            ("after", "变动后", 70),
            ("operator", "操作人", 80),
            ("remark", "备注", 280),
        ]
        for col, text, width in headers:
            self.history_tree.heading(col, text=text)
            self.history_tree.column(col, width=width, anchor="center")
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=vsb.set)
        self.history_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _build_logs_tab(self):
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="操作日志")

        columns = ("id", "created_at", "operator", "action", "target", "detail", "success", "error")
        self.logs_tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="browse")
        headers = [
            ("id", "ID", 60),
            ("created_at", "时间", 150),
            ("operator", "操作人", 90),
            ("action", "操作", 120),
            ("target", "目标", 80),
            ("detail", "详情", 300),
            ("success", "结果", 60),
            ("error", "错误信息", 250),
        ]
        for col, text, width in headers:
            self.logs_tree.heading(col, text=text)
            self.logs_tree.column(col, width=width, anchor="w" if col in ("detail", "error") else "center")
        self.logs_tree.tag_configure("success", background="#F0F9EB")
        self.logs_tree.tag_configure("fail", background="#FEF0F0")
        vsb = ttk.Scrollbar(tab, orient="vertical", command=self.logs_tree.yview)
        self.logs_tree.configure(yscrollcommand=vsb.set)
        self.logs_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

    def _show_login(self):
        self.withdraw()
        dlg = LoginDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self.current_user = dlg.result
            role_text = "主管" if self.current_user["role"] == "supervisor" else "操作员"
            self.user_info.configure(text=f"当前用户: {self.current_user['display_name']} ({role_text})")
            self.deiconify()
            self._update_button_permissions()
            self._refresh_init_with_restore()
        else:
            self.destroy()

    def _refresh_init_with_restore(self):
        prev_user_id = get_last_login_user_id()
        is_user_switched = (prev_user_id is not None and prev_user_id != self.current_user["id"])
        if is_user_switched:
            handle_user_switch(prev_user_id, self.current_user["id"])

        categories = get_all_categories()
        self.parts_category_combo.configure(values=[""] + categories)
        self.parts_category_combo.current(0)
        parts_list = ["全部"]
        self.history_part_map = {"全部": None}
        for p in get_all_parts():
            label = f"{p['part_code']} {p['part_name']}"
            parts_list.append(label)
            self.history_part_map[label] = p["id"]
        self.history_combo.configure(values=parts_list)
        self.history_combo.current(0)
        self._refresh_borrower_combo()
        self._wb_refresh_borrower_combo()

        self._wb_refresh_scheme_combo()
        self._refresh_scheme_combo()

        try:
            restore_result = restore_workbench_state(self.current_user["id"], self.current_user["role"])
        except Exception as e:
            restore_result = handle_corrupt_state(self.current_user["id"])

        self._apply_restored_state_to_workbench(restore_result)
        self._apply_restored_state_to_borrow_tab(restore_result)

        self._wb_refresh_records(log_query=False)
        self._refresh_borrow(log_query=False)

        self._refresh_parts()
        self._refresh_approval()
        self._refresh_history()
        self._refresh_logs()

        if restore_result.warnings:
            hint = " | ".join(restore_result.warnings)
            self.restore_hint_label.configure(text=f"恢复提示: {hint}")
        else:
            self.restore_hint_label.configure(text="")

        self._wb_save_full_state()

    def _update_button_permissions(self):
        is_supervisor = self.current_user and self.current_user["role"] == "supervisor"
        self.btn_add_part.configure(state="normal" if is_supervisor else "disabled")
        self.btn_edit_part.configure(state="normal" if is_supervisor else "disabled")
        self.btn_adjust.configure(state="normal" if is_supervisor else "disabled")
        self.btn_delete_part.configure(state="normal" if is_supervisor else "disabled")
        self.btn_approve.configure(state="normal" if is_supervisor else "disabled")
        self.btn_reject.configure(state="normal" if is_supervisor else "disabled")

    def _refresh_all(self):
        categories = get_all_categories()
        self.parts_category_combo.configure(values=[""] + categories)
        self.parts_category_combo.current(0)
        parts_list = ["全部"]
        self.history_part_map = {"全部": None}
        for p in get_all_parts():
            label = f"{p['part_code']} {p['part_name']}"
            parts_list.append(label)
            self.history_part_map[label] = p["id"]
        self.history_combo.configure(values=parts_list)
        self.history_combo.current(0)
        self._refresh_borrower_combo()
        self._wb_refresh_borrower_combo()
        self._refresh_scheme_combo()
        self._wb_refresh_scheme_combo()
        self._refresh_parts()
        self._refresh_borrow()
        self._wb_refresh_records()
        self._refresh_approval()
        self._refresh_history()
        self._refresh_logs()

    def _wb_refresh_borrower_combo(self):
        users = get_all_users()
        borrower_labels = ["全部"]
        self._wb_borrower_map = {"全部": None}
        for u in users:
            label = f"{u['display_name']} ({u['username']})"
            borrower_labels.append(label)
            self._wb_borrower_map[label] = u["id"]
        self.wb_borrower_combo.configure(values=borrower_labels)
        self.wb_borrower_combo.current(0)

    def _wb_refresh_scheme_combo(self):
        if not self.current_user:
            return
        schemes = get_available_schemes(self.current_user["id"], self.current_user["role"])
        scheme_labels = [""]
        self._wb_scheme_map = {}
        for s in schemes:
            scope_tag = "[共享]" if s["scope"] == "shared" else "[个人]"
            owner_hint = ""
            if s["scope"] == "shared" and s.get("owner_name"):
                owner_hint = f"({s['owner_name']})"
            label = f"{scope_tag} {s['name']} {owner_hint}".strip()
            scheme_labels.append(label)
            self._wb_scheme_map[label] = s["id"]
        self.wb_scheme_combo.configure(values=scheme_labels)
        active_id = get_active_scheme_id(self.current_user["id"])
        if active_id:
            found = False
            for lbl, sid in self._wb_scheme_map.items():
                if sid == active_id:
                    self.wb_scheme_var.set(lbl)
                    found = True
                    break
            if not found:
                self.wb_scheme_combo.set("")
        else:
            self.wb_scheme_combo.set("")

    def _collect_wb_filters(self):
        status_label = self.wb_status.get()
        status = self._wb_status_map.get(status_label, "")
        keyword = self.wb_keyword.get().strip()
        borrower_label = self.wb_borrower.get()
        borrower_id = self._wb_borrower_map.get(borrower_label)
        date_from = self.wb_date_from.get().strip()
        date_to = self.wb_date_to.get().strip()
        filters = {}
        if status:
            filters["status"] = status
        if keyword:
            filters["keyword"] = keyword
        if borrower_id:
            filters["borrower_id"] = borrower_id
        if date_from:
            filters["date_from"] = date_from
        if date_to:
            filters["date_to"] = date_to
        return filters

    def _apply_restored_state_to_workbench(self, restore_result):
        if not self.current_user:
            return
        self._wb_refresh_scheme_combo()
        if restore_result.scheme:
            scheme = restore_result.scheme
            self.wb_active_scheme_label.configure(text=f"{scheme['name']}", foreground="#409EFF")
            for lbl, sid in self._wb_scheme_map.items():
                if sid == scheme["id"]:
                    self.wb_scheme_var.set(lbl)
                    break
        else:
            self.wb_active_scheme_label.configure(text="未激活", foreground="#909399")
        filters = restore_result.filters or {}
        status_val = filters.get("status", "")
        matched = False
        for lbl, val in self._wb_status_map.items():
            if val == status_val:
                self.wb_status.set(lbl)
                matched = True
                break
        if not matched:
            self.wb_status_combo.current(0)
        keyword_val = filters.get("keyword", "")
        self.wb_keyword.set(keyword_val)
        borrower_id_val = filters.get("borrower_id")
        if borrower_id_val:
            matched_borrower = False
            for lbl, bid in self._wb_borrower_map.items():
                if bid == borrower_id_val:
                    self.wb_borrower.set(lbl)
                    matched_borrower = True
                    break
            if not matched_borrower:
                self.wb_borrower_combo.current(0)
        else:
            self.wb_borrower_combo.current(0)
        date_from_val = filters.get("date_from", "")
        self.wb_date_from.set(date_from_val)
        date_to_val = filters.get("date_to", "")
        self.wb_date_to.set(date_to_val)

        state = restore_result.state
        if state:
            valid_page_sizes = [10, 20, 50, 100, 200, 500]
            page_size = state.page_size or 20
            if page_size not in valid_page_sizes:
                page_size = 20
            self.wb_page_size_var.set(page_size)

            valid_sort_fields = list(self._wb_col_sort_map.values())
            sort_by = state.sort_by or "created_at"
            if sort_by not in valid_sort_fields:
                sort_by = "created_at"
            self.wb_sort_by = sort_by
            sort_order = state.sort_order or "desc"
            if sort_order not in ("asc", "desc"):
                sort_order = "desc"
            self.wb_sort_order = sort_order
            self._wb_update_sort_label()

            total_pages_before = self._wb_total_pages
            page = state.page or 1
            if page < 1:
                page = 1
            self._wb_current_page = page
            self.wb_page_var.set(page)

    def _apply_restored_state_to_borrow_tab(self, restore_result):
        if not self.current_user:
            return
        self._refresh_scheme_combo()
        if restore_result.scheme:
            scheme = restore_result.scheme
            self.active_scheme_id = scheme["id"]
            self.active_scheme_label.configure(text=f"当前方案: {scheme['name']}")
            for lbl, sid in self._scheme_map.items():
                if sid == scheme["id"]:
                    self.scheme_var.set(lbl)
                    break
        else:
            self.active_scheme_id = None
            self.active_scheme_label.configure(text="")
        filters = restore_result.filters or {}
        status_val = filters.get("status", "")
        matched = False
        for lbl, val in self._status_map.items():
            if val == status_val:
                self.borrow_status.set(lbl)
                matched = True
                break
        if not matched:
            self.borrow_status_combo.current(0)
        keyword_val = filters.get("keyword", "")
        self.borrow_keyword.set(keyword_val)
        borrower_id_val = filters.get("borrower_id")
        if borrower_id_val:
            matched_borrower = False
            for lbl, bid in self._borrower_map.items():
                if bid == borrower_id_val:
                    self.borrow_person.set(lbl)
                    matched_borrower = True
                    break
            if not matched_borrower:
                self.borrow_person_combo.current(0)
        else:
            self.borrow_person_combo.current(0)
        date_from_val = filters.get("date_from", "")
        self.date_from_var.set(date_from_val)
        date_to_val = filters.get("date_to", "")
        self.date_to_var.set(date_to_val)

    def _wb_update_sort_label(self):
        arrow = "↑" if self.wb_sort_order == "asc" else "↓"
        field_text = "创建时间"
        for ui_col, db_col in self._wb_col_sort_map.items():
            if db_col == self.wb_sort_by:
                field_text = self._wb_col_text_map.get(ui_col, db_col)
                break
        self.wb_sort_label.configure(text=f"{field_text}{arrow}")

    def _wb_on_column_click(self, col):
        db_field = self._wb_col_sort_map.get(col)
        if not db_field:
            return
        if self.wb_sort_by == db_field:
            self.wb_sort_order = "asc" if self.wb_sort_order == "desc" else "desc"
        else:
            self.wb_sort_by = db_field
            self.wb_sort_order = "desc"
        self._wb_update_sort_label()
        self._wb_current_page = 1
        self.wb_page_var.set(1)
        self._wb_refresh_records()

    def _wb_on_page_size_change(self):
        self._wb_current_page = 1
        self.wb_page_var.set(1)
        self._wb_refresh_records()

    def _wb_on_page_spin_change(self):
        try:
            new_page = int(self.wb_page_var.get())
        except (ValueError, tk.TclError):
            new_page = 1
        self._wb_goto_page(new_page, update_spinbox=False)

    def _wb_goto_page(self, page, update_spinbox=True):
        if self._wb_total_pages <= 0:
            self._wb_current_page = 1
            if update_spinbox:
                self.wb_page_var.set(1)
            return
        if page < 1:
            page = 1
        if page > self._wb_total_pages:
            page = self._wb_total_pages
        if page == self._wb_current_page and len(self.wb_tree.get_children()) > 0:
            return
        self._wb_current_page = page
        if update_spinbox:
            self.wb_page_var.set(page)
        self._wb_render_paged_records()

    def _wb_render_paged_records(self):
        for item in self.wb_tree.get_children():
            self.wb_tree.delete(item)
        page_size = self.wb_page_size_var.get()
        start = (self._wb_current_page - 1) * page_size
        end = start + page_size
        page_records = self._wb_all_records[start:end]
        for r in page_records:
            status_text, _ = STATUS_DISPLAY.get(r["status"], (r["status"], ""))
            self.wb_tree.insert("", "end", iid=str(r["id"]), values=(
                r["record_no"], r["part_code"], r["part_name"],
                r["quantity"], r["unit"], r["borrower_name"],
                status_text, r["created_at"]
            ), tags=(r["status"],))
        self._wb_update_pager_buttons()

    def _wb_update_pager_buttons(self):
        if self._wb_total_pages <= 1:
            self._wb_total_pages = 1
        self.wb_page_spinbox.configure(to=self._wb_total_pages)
        self.wb_total_pages_label.configure(text=f"页 / 共 {self._wb_total_pages} 页")
        if self._wb_current_page <= 1:
            self.wb_btn_first.configure(state="disabled")
            self.wb_btn_prev.configure(state="disabled")
        else:
            self.wb_btn_first.configure(state="normal")
            self.wb_btn_prev.configure(state="normal")
        if self._wb_current_page >= self._wb_total_pages:
            self.wb_btn_next.configure(state="disabled")
            self.wb_btn_last.configure(state="disabled")
        else:
            self.wb_btn_next.configure(state="normal")
            self.wb_btn_last.configure(state="normal")
        page_size = self.wb_page_size_var.get()
        if self._wb_total_count == 0:
            self.wb_pager_info.configure(text="(0 条记录)")
        else:
            start_idx = (self._wb_current_page - 1) * page_size + 1
            end_idx = min(self._wb_current_page * page_size, self._wb_total_count)
            self.wb_pager_info.configure(text=f"(显示 {start_idx}-{end_idx} / 共 {self._wb_total_count} 条)")

    def _wb_refresh_records(self, log_query=True):
        filters = self._collect_wb_filters()
        all_records = get_borrow_records(**filters)

        sort_key = self.wb_sort_by
        reverse = (self.wb_sort_order == "desc")
        try:
            all_records.sort(key=lambda r: (r.get(sort_key) is None, r.get(sort_key, "")),
                             reverse=reverse)
        except Exception:
            pass

        self._wb_all_records = all_records
        self._wb_total_count = len(all_records)
        page_size = self.wb_page_size_var.get()
        self._wb_total_pages = max(1, (self._wb_total_count + page_size - 1) // page_size)
        if self._wb_current_page > self._wb_total_pages:
            self._wb_current_page = self._wb_total_pages
            self.wb_page_var.set(self._wb_current_page)

        self._wb_render_paged_records()
        self.wb_count_label.configure(text=f"{self._wb_total_count} 条")
        self._wb_update_pager_buttons()
        if self.current_user and log_query:
            log_query_operation(self.current_user["id"], filters, self._wb_total_count)
        if self.current_user:
            if not _is_filter_empty(filters):
                save_last_filters(self.current_user["id"], filters)
            save_last_list_state(self.current_user["id"],
                                 sort_by=self.wb_sort_by,
                                 sort_order=self.wb_sort_order,
                                 page=self._wb_current_page,
                                 page_size=page_size)
            self._wb_save_full_state()

    def _wb_reset_filters(self):
        self.wb_status_combo.current(0)
        self.wb_keyword.set("")
        self.wb_borrower_combo.current(0)
        self.wb_date_from.set("")
        self.wb_date_to.set("")
        self.wb_sort_by = "created_at"
        self.wb_sort_order = "desc"
        self._wb_update_sort_label()
        self.wb_page_size_var.set(20)
        self._wb_current_page = 1
        self.wb_page_var.set(1)
        deactivate_scheme(self.current_user["id"])
        self.wb_active_scheme_label.configure(text="未激活", foreground="#909399")
        self.wb_scheme_combo.set("")
        self.active_scheme_id = None
        self.active_scheme_label.configure(text="")
        self.scheme_combo.set("")
        if self.current_user:
            save_last_filters(self.current_user["id"], {})
            save_last_list_state(self.current_user["id"], sort_by="created_at",
                                 sort_order="desc", page=1, page_size=20)
        self._wb_refresh_records()
        self._refresh_borrow()

    def _on_wb_scheme_selected(self, event=None):
        pass

    def _wb_activate_scheme(self):
        label = self.wb_scheme_var.get()
        scheme_id = self._wb_scheme_map.get(label)
        if not scheme_id:
            messagebox.showinfo("提示", "请先从下拉列表选择一个方案", parent=self)
            return
        try:
            scheme = activate_scheme(self.current_user["id"], scheme_id, self.current_user["role"])
            self.wb_active_scheme_label.configure(text=f"{scheme['name']}", foreground="#409EFF")
            self._apply_restored_state_to_workbench(RestoreResult(success=True, scheme=scheme, filters=scheme["filters"]))
            self._apply_restored_state_to_borrow_tab(RestoreResult(success=True, scheme=scheme, filters=scheme["filters"]))
            self._wb_refresh_records()
            self._refresh_borrow()
            messagebox.showinfo("成功", f"方案「{scheme['name']}」已激活，条件已套用", parent=self)
        except BusinessException as e:
            messagebox.showerror("激活失败", e.message, parent=self)

    def _wb_save_scheme(self):
        if not self.current_user:
            return
        filters = self._collect_wb_filters()
        if _is_filter_empty(filters):
            messagebox.showwarning("提示", "当前筛选条件全部为空，无法保存方案", parent=self)
            return
        dlg = SaveSchemeDialog(self, self.current_user, get_active_scheme_id(self.current_user["id"]))
        self.wait_window(dlg)
        if dlg.result:
            try:
                scheme_id = save_filter_scheme(
                    name=dlg.result["name"],
                    owner_id=self.current_user["id"],
                    filters=filters,
                    scope=dlg.result["scope"],
                    scheme_id=dlg.result.get("scheme_id"),
                    role=self.current_user["role"]
                )
                activate_scheme(self.current_user["id"], scheme_id, self.current_user["role"])
                self._wb_refresh_scheme_combo()
                self._refresh_scheme_combo()
                scheme = get_filter_scheme_by_id(scheme_id)
                if scheme:
                    self.wb_active_scheme_label.configure(text=f"{scheme['name']}", foreground="#409EFF")
                    self.active_scheme_id = scheme_id
                    self.active_scheme_label.configure(text=f"当前方案: {scheme['name']}")
                    for lbl, sid in self._wb_scheme_map.items():
                        if sid == scheme_id:
                            self.wb_scheme_var.set(lbl)
                            break
                    for lbl, sid in self._scheme_map.items():
                        if sid == scheme_id:
                            self.scheme_var.set(lbl)
                            break
                messagebox.showinfo("成功", f"方案「{dlg.result['name']}」已保存并激活", parent=self)
            except BusinessException as e:
                messagebox.showerror("保存失败", e.message, parent=self)

    def _wb_delete_scheme(self):
        label = self.wb_scheme_var.get()
        scheme_id = self._wb_scheme_map.get(label)
        if not scheme_id:
            messagebox.showinfo("提示", "请先从下拉列表选择一个方案", parent=self)
            return
        scheme = get_filter_scheme_by_id(scheme_id)
        if not scheme:
            messagebox.showwarning("提示", "方案已不存在", parent=self)
            self._wb_refresh_scheme_combo()
            self._wb_check_active_scheme_valid()
            return
        if not messagebox.askyesno("确认", f"确定要删除方案「{scheme['name']}」吗？", parent=self):
            return
        try:
            was_active = delete_scheme_and_cleanup(scheme_id, self.current_user["id"], self.current_user["role"])
            self._wb_refresh_scheme_combo()
            self._refresh_scheme_combo()
            if was_active:
                self.wb_active_scheme_label.configure(text="未激活", foreground="#909399")
                self.active_scheme_id = None
                self.active_scheme_label.configure(text="")
                messagebox.showinfo("提示", "方案已删除，当前视图已回退为上次使用的筛选条件", parent=self)
            else:
                messagebox.showinfo("成功", f"方案「{scheme['name']}」已删除", parent=self)
            self._wb_refresh_records()
            self._refresh_borrow()
        except BusinessException as e:
            messagebox.showerror("删除失败", e.message, parent=self)

    def _wb_rename_scheme(self):
        label = self.wb_scheme_var.get()
        scheme_id = self._wb_scheme_map.get(label)
        if not scheme_id:
            messagebox.showinfo("提示", "请先从下拉列表选择一个方案", parent=self)
            return
        scheme = get_filter_scheme_by_id(scheme_id)
        if not scheme:
            messagebox.showwarning("提示", "方案已不存在", parent=self)
            self._wb_refresh_scheme_combo()
            return
        from tkinter import simpledialog
        new_name = simpledialog.askstring(
            "重命名方案", f"请输入新的方案名称：",
            initialvalue=scheme["name"], parent=self
        )
        if not new_name or not new_name.strip():
            return
        try:
            updated = rename_scheme(scheme_id, new_name.strip(),
                                    self.current_user["id"], self.current_user["role"])
            self._wb_refresh_scheme_combo()
            self._refresh_scheme_combo()
            if get_active_scheme_id(self.current_user["id"]) == scheme_id:
                self.wb_active_scheme_label.configure(
                    text=updated["name"], foreground="#409EFF")
                self.active_scheme_label.configure(text=f"当前方案: {updated['name']}")
            for lbl, sid in self._wb_scheme_map.items():
                if sid == scheme_id:
                    self.wb_scheme_var.set(lbl)
                    break
            messagebox.showinfo("成功", f"方案已重命名为「{updated['name']}」", parent=self)
        except BusinessException as e:
            messagebox.showerror("重命名失败", e.message, parent=self)

    def _wb_show_recycle_bin(self):
        recycle_items = get_recycle_bin(self.current_user["id"])
        if not recycle_items:
            messagebox.showinfo("回收站", "回收站为空", parent=self)
            return
        dlg = tk.Toplevel(self)
        dlg.title("方案回收站")
        dlg.geometry("520x380")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        frame = ttk.Frame(dlg, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="最近删除的方案（7天内可恢复）：",
                  font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(0, 8))
        columns = ("name", "scope", "deleted_at")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("name", text="方案名称")
        tree.heading("scope", text="可见范围")
        tree.heading("deleted_at", text="删除时间")
        tree.column("name", width=180, anchor="w")
        tree.column("scope", width=100, anchor="center")
        tree.column("deleted_at", width=180, anchor="center")
        for item in recycle_items:
            scope_text = "共享" if item.scope == "shared" else "个人"
            tree.insert("", "end", iid=item.name, values=(item.name, scope_text, item.deleted_at))
        tree.pack(fill="both", expand=True, pady=(0, 10))
        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill="x")
        def _on_restore():
            sel = tree.selection()
            if not sel:
                messagebox.showinfo("提示", "请选择要恢复的方案", parent=dlg)
                return
            scheme_name = sel[0]
            try:
                restored = restore_scheme_from_recycle(
                    self.current_user["id"], scheme_name, self.current_user["role"])
                self._wb_refresh_scheme_combo()
                self._refresh_scheme_combo()
                tree.delete(scheme_name)
                messagebox.showinfo("成功", f"方案「{scheme_name}」已恢复", parent=dlg)
                if not tree.get_children():
                    dlg.destroy()
            except BusinessException as e:
                messagebox.showerror("恢复失败", e.message, parent=dlg)
        ttk.Button(btn_frame, text="恢复选中方案", command=_on_restore, width=14).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="关闭", command=dlg.destroy, width=10).pack(side="right", padx=5)

    def _wb_save_full_state(self):
        if not self.current_user:
            return
        state = WorkbenchState()
        state.filters = self._collect_wb_filters()
        state.sort_by = self.wb_sort_by
        state.sort_order = self.wb_sort_order
        state.page = self._wb_current_page
        state.page_size = self.wb_page_size_var.get()
        active_id = get_active_scheme_id(self.current_user["id"])
        if active_id:
            scheme = get_filter_scheme_by_id(active_id)
            if scheme:
                state.active_scheme_id = scheme["id"]
                state.active_scheme_name = scheme["name"]
        save_workbench_full_state(self.current_user["id"], state)

    def _wb_check_active_scheme_valid(self):
        active_id = get_active_scheme_id(self.current_user["id"])
        if active_id:
            scheme = get_filter_scheme_by_id(active_id)
            if not scheme or not _can_access_scheme(scheme, self.current_user["id"], self.current_user["role"]):
                deactivate_scheme(self.current_user["id"])
                self.wb_active_scheme_label.configure(text="未激活", foreground="#909399")
                self.active_scheme_id = None
                self.active_scheme_label.configure(text="")

    def _wb_export_csv(self):
        filters = self._collect_wb_filters()
        filename = generate_default_filename("workbench_borrow_records")
        path = filedialog.asksaveasfilename(
            parent=self, title="工作台-导出借还记录", defaultextension=".csv",
            initialfile=filename, filetypes=[("CSV文件", "*.csv")]
        )
        if not path:
            return
        try:
            count = export_borrow_records(path, **filters)
            self._wb_last_export_path = path
            self._wb_last_export_filters = dict(filters)
            scheme_id = get_active_scheme_id(self.current_user["id"]) if self.current_user else None
            if self.current_user:
                log_export_operation(self.current_user["id"], filters, count, os.path.basename(path), scheme_id)
            scheme_note = ""
            active_id = get_active_scheme_id(self.current_user["id"])
            if active_id:
                scheme = get_filter_scheme_by_id(active_id)
                if scheme:
                    scheme_note = f" (按方案「{scheme['name']}」筛选)"
            self.wb_verify_result_label.configure(text="", foreground="#67C23A")
            messagebox.showinfo("成功", f"已导出 {count} 条记录到:\n{path}{scheme_note}", parent=self)
        except Exception as e:
            messagebox.showerror("错误", f"导出失败: {e}", parent=self)

    def _wb_verify_export(self):
        if not self._wb_last_export_path or not os.path.exists(self._wb_last_export_path):
            messagebox.showinfo("提示", "请先导出CSV文件再进行一致性校验", parent=self)
            return
        filters = self._wb_last_export_filters or self._collect_wb_filters()
        result = verify_export_consistency(self._wb_last_export_path, filters)
        if result["consistent"]:
            self.wb_verify_result_label.configure(
                text=f"✓ 一致 (DB:{result['db_count']}条 / CSV:{result['csv_count']}条)",
                foreground="#67C23A"
            )
            messagebox.showinfo("校验通过",
                                f"CSV导出与数据库查询结果完全一致！\n"
                                f"数据库: {result['db_count']} 条\n"
                                f"CSV文件: {result['csv_count']} 条",
                                parent=self)
        else:
            self.wb_verify_result_label.configure(
                text=f"✗ 不一致: {result['reason']}",
                foreground="#F56C6C"
            )
            messagebox.showerror("校验失败",
                                 f"CSV导出与查询结果不一致！\n"
                                 f"原因: {result['reason']}\n"
                                 f"数据库: {result['db_count']} 条\n"
                                 f"CSV文件: {result['csv_count']} 条",
                                 parent=self)

    def _reset_parts_filter(self):
        self.parts_keyword.set("")
        self.parts_category.set("")
        self.parts_category_combo.configure(values=[""] + get_all_categories())
        self.parts_category_combo.current(0)
        self._refresh_parts()

    def _reset_borrow_filter(self):
        self.borrow_status_combo.current(0)
        self.borrow_keyword.set("")
        self.borrow_person_combo.current(0)
        self.date_from_var.set("")
        self.date_to_var.set("")
        self.active_scheme_id = None
        self.active_scheme_label.configure(text="")
        self.scheme_combo.set("")
        if self.current_user:
            set_active_scheme_id(self.current_user["id"], None)
        self._refresh_borrow()

    def _refresh_borrower_combo(self):
        users = get_all_users()
        borrower_labels = ["全部"]
        self._borrower_map = {"全部": None}
        for u in users:
            label = f"{u['display_name']} ({u['username']})"
            borrower_labels.append(label)
            self._borrower_map[label] = u["id"]
        self.borrow_person_combo.configure(values=borrower_labels)
        self.borrow_person_combo.current(0)

    def _refresh_scheme_combo(self):
        if not self.current_user:
            return
        schemes = get_filter_schemes(self.current_user["id"], self.current_user["role"])
        scheme_labels = [""]
        self._scheme_map = {}
        for s in schemes:
            scope_tag = "[共享]" if s["scope"] == "shared" else "[个人]"
            owner_hint = ""
            if s["scope"] == "shared" and s.get("owner_name"):
                owner_hint = f"({s['owner_name']})"
            label = f"{scope_tag} {s['name']} {owner_hint}".strip()
            scheme_labels.append(label)
            self._scheme_map[label] = s["id"]
        self.scheme_combo.configure(values=scheme_labels)
        if self.active_scheme_id:
            found = False
            for lbl, sid in self._scheme_map.items():
                if sid == self.active_scheme_id:
                    self.scheme_var.set(lbl)
                    found = True
                    break
            if not found:
                self.active_scheme_id = None
                self.active_scheme_label.configure(text="")
                self.scheme_combo.set("")
                set_active_scheme_id(self.current_user["id"], None)
        else:
            self.scheme_combo.set("")

    def _restore_active_scheme(self):
        if not self.current_user:
            return
        saved_id = get_active_scheme_id(self.current_user["id"])
        if not saved_id:
            return
        scheme = get_filter_scheme_by_id(saved_id)
        if not scheme:
            set_active_scheme_id(self.current_user["id"], None)
            return
        self.active_scheme_id = saved_id
        for lbl, sid in self._scheme_map.items():
            if sid == saved_id:
                self.scheme_var.set(lbl)
                break
        self.active_scheme_label.configure(text=f"当前方案: {scheme['name']}")
        self._apply_scheme_filters(scheme)

    def _on_scheme_selected(self, event=None):
        pass

    def _apply_scheme(self):
        label = self.scheme_var.get()
        scheme_id = self._scheme_map.get(label)
        if not scheme_id:
            messagebox.showinfo("提示", "请先从下拉列表选择一个方案", parent=self)
            return
        scheme = get_filter_scheme_by_id(scheme_id)
        if not scheme:
            messagebox.showwarning("提示", "方案已被删除，将重置为默认视图", parent=self)
            self._fallback_to_default()
            return
        self.active_scheme_id = scheme_id
        set_active_scheme_id(self.current_user["id"], scheme_id)
        self.active_scheme_label.configure(text=f"当前方案: {scheme['name']}")
        self._apply_scheme_filters(scheme)
        self._refresh_borrow()

    def _apply_scheme_filters(self, scheme):
        filters = scheme["filters"]
        status_val = filters.get("status", "")
        matched = False
        for lbl, val in self._status_map.items():
            if val == status_val:
                self.borrow_status.set(lbl)
                matched = True
                break
        if not matched:
            self.borrow_status_combo.current(0)
        keyword_val = filters.get("keyword", "")
        self.borrow_keyword.set(keyword_val)
        borrower_id_val = filters.get("borrower_id")
        if borrower_id_val:
            matched_borrower = False
            for lbl, bid in self._borrower_map.items():
                if bid == borrower_id_val:
                    self.borrow_person.set(lbl)
                    matched_borrower = True
                    break
            if not matched_borrower:
                self.borrow_person_combo.current(0)
        else:
            self.borrow_person_combo.current(0)
        date_from_val = filters.get("date_from", "")
        self.date_from_var.set(date_from_val)
        date_to_val = filters.get("date_to", "")
        self.date_to_var.set(date_to_val)

    def _fallback_to_default(self):
        self.active_scheme_id = None
        self.active_scheme_label.configure(text="")
        set_active_scheme_id(self.current_user["id"], None)
        self._reset_borrow_filter()

    def _save_scheme(self):
        if not self.current_user:
            return
        filters = self._collect_current_filters()
        from services import _is_filter_empty
        if _is_filter_empty(filters):
            messagebox.showwarning("提示", "当前筛选条件全部为空，无法保存方案", parent=self)
            return
        dlg = SaveSchemeDialog(self, self.current_user, self.active_scheme_id)
        self.wait_window(dlg)
        if dlg.result:
            try:
                scheme_id = save_filter_scheme(
                    name=dlg.result["name"],
                    owner_id=self.current_user["id"],
                    filters=filters,
                    scope=dlg.result["scope"],
                    scheme_id=dlg.result.get("scheme_id"),
                    role=self.current_user["role"]
                )
                self.active_scheme_id = scheme_id
                set_active_scheme_id(self.current_user["id"], scheme_id)
                self._refresh_scheme_combo()
                scheme = get_filter_scheme_by_id(scheme_id)
                if scheme:
                    self.active_scheme_label.configure(text=f"当前方案: {scheme['name']}")
                messagebox.showinfo("成功", f"方案「{dlg.result['name']}」已保存", parent=self)
            except BusinessException as e:
                messagebox.showerror("保存失败", e.message, parent=self)

    def _delete_scheme(self):
        label = self.scheme_var.get()
        scheme_id = self._scheme_map.get(label)
        if not scheme_id:
            messagebox.showinfo("提示", "请先从下拉列表选择一个方案", parent=self)
            return
        scheme = get_filter_scheme_by_id(scheme_id)
        if not scheme:
            messagebox.showwarning("提示", "方案已不存在", parent=self)
            if self.active_scheme_id == scheme_id:
                self._fallback_to_default()
            self._refresh_scheme_combo()
            return
        if not messagebox.askyesno("确认", f"确定要删除方案「{scheme['name']}」吗？", parent=self):
            return
        try:
            delete_filter_scheme(scheme_id, self.current_user["id"], self.current_user["role"])
            if self.active_scheme_id == scheme_id:
                self._fallback_to_default()
            self._refresh_scheme_combo()
            self._refresh_borrow()
            messagebox.showinfo("成功", f"方案「{scheme['name']}」已删除", parent=self)
        except BusinessException as e:
            messagebox.showerror("删除失败", e.message, parent=self)

    def _collect_current_filters(self):
        status_label = self.borrow_status.get()
        status = self._status_map.get(status_label, "")
        keyword = self.borrow_keyword.get().strip()
        borrower_label = self.borrow_person.get()
        borrower_id = self._borrower_map.get(borrower_label)
        date_from = self.date_from_var.get().strip()
        date_to = self.date_to_var.get().strip()
        filters = {}
        if status:
            filters["status"] = status
        if keyword:
            filters["keyword"] = keyword
        if borrower_id:
            filters["borrower_id"] = borrower_id
        if date_from:
            filters["date_from"] = date_from
        if date_to:
            filters["date_to"] = date_to
        return filters

    def _refresh_parts(self):
        keyword = self.parts_keyword.get().strip() or None
        category = self.parts_category.get().strip() or None
        for item in self.parts_tree.get_children():
            self.parts_tree.delete(item)
        for p in get_all_parts(keyword, category):
            approval_flag = "是" if p["requires_approval"] else "否"
            if p["available_stock"] > 0:
                status_text = "可借"
                tag = "available"
            elif p["pending_count"] > 0:
                status_text = "待审批"
                tag = "pending"
            elif p["borrowed_count"] > 0:
                status_text = "已借空"
                tag = "empty"
            else:
                status_text = "无库存"
                tag = "normal"
            self.parts_tree.insert("", "end", iid=str(p["id"]), values=(
                p["part_code"], p["part_name"], p["category"],
                p.get("specification", "") or "", p["unit"],
                f"{p['unit_price']:.2f}", approval_flag,
                p["available_stock"], p["pending_count"], p["borrowed_count"],
                p["total_stock"], status_text
            ), tags=(tag,))
        self._set_status(f"备件查询完成，共 {len(self.parts_tree.get_children())} 条记录")

    def _refresh_borrow(self, log_query=True):
        status_label = self.borrow_status.get()
        status = self._status_map.get(status_label, "")
        status = status or None
        keyword = self.borrow_keyword.get().strip() or None
        borrower_label = self.borrow_person.get()
        borrower_id = self._borrower_map.get(borrower_label)
        date_from = self.date_from_var.get().strip() or None
        date_to = self.date_to_var.get().strip() or None
        filters = {}
        if status:
            filters["status"] = status
        if keyword:
            filters["keyword"] = keyword
        if borrower_id:
            filters["borrower_id"] = borrower_id
        if date_from:
            filters["date_from"] = date_from
        if date_to:
            filters["date_to"] = date_to
        for item in self.borrow_tree.get_children():
            self.borrow_tree.delete(item)
        records = get_borrow_records(status=status, keyword=keyword, borrower_id=borrower_id,
                                     date_from=date_from, date_to=date_to)
        for r in records:
            status_text, _ = STATUS_DISPLAY.get(r["status"], (r["status"], ""))
            self.borrow_tree.insert("", "end", iid=str(r["id"]), values=(
                r["record_no"], r["part_code"], r["part_name"],
                r["quantity"], r["unit"], r["borrower_name"],
                r.get("purpose", "") or "", status_text,
                r.get("approver_name", "") or "",
                r["created_at"], r["return_quantity"]
            ), tags=(r["status"],))
        scheme_hint = ""
        if self.active_scheme_id:
            scheme = get_filter_scheme_by_id(self.active_scheme_id)
            if scheme:
                scheme_hint = f" [方案: {scheme['name']}]"
            else:
                self.active_scheme_id = None
                self.active_scheme_label.configure(text="")
        self._set_status(f"借还记录查询完成，共 {len(records)} 条记录{scheme_hint}")
        if self.current_user and log_query:
            log_query_operation(self.current_user["id"], filters, len(records))
        if not _is_filter_empty(filters) and self.current_user:
            save_last_filters(self.current_user["id"], filters)

    def _refresh_approval(self):
        for item in self.approval_tree.get_children():
            self.approval_tree.delete(item)
        records = get_borrow_records(status="pending_approval")
        for r in records:
            amount = r["quantity"] * r["unit_price"]
            self.approval_tree.insert("", "end", iid=str(r["id"]), values=(
                r["record_no"], r["part_code"], r["part_name"],
                r["quantity"], r["unit"], f"{amount:.2f}",
                r["borrower_name"], r.get("purpose", "") or "",
                r["created_at"]
            ), tags=("pending",))

    def _refresh_history(self):
        label = self.history_part.get()
        part_id = self.history_part_map.get(label)
        for item in self.history_tree.get_children():
            self.history_tree.delete(item)
        logs = get_stock_logs(part_id=part_id, limit=500)
        for log in logs:
            op_text, _ = OPERATION_DISPLAY.get(log["operation_type"], (log["operation_type"], ""))
            change = f"{log['quantity_change']:+d}"
            self.history_tree.insert("", "end", iid=str(log["id"]), values=(
                log["id"], log["created_at"], log["part_code"], log["part_name"],
                op_text, change, log["before_available"], log["after_available"],
                log["operator_name"], log.get("remark", "") or ""
            ))

    def _refresh_logs(self):
        for item in self.logs_tree.get_children():
            self.logs_tree.delete(item)
        logs = get_operation_logs(limit=300)
        for log in logs:
            result_text = "成功" if log["success"] else "失败"
            tag = "success" if log["success"] else "fail"
            self.logs_tree.insert("", "end", iid=str(log["id"]), values=(
                log["id"], log["created_at"], log["operator_name"],
                log["action"], log.get("target_type", "") or "",
                log.get("detail", "") or "", result_text,
                log.get("error_message", "") or ""
            ), tags=(tag,))

    def _get_selected_part(self):
        sel = self.parts_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条备件记录", parent=self)
            return None
        part_id = int(sel[0])
        return get_part_by_id(part_id)

    def _get_selected_borrow(self):
        sel = self.borrow_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条记录", parent=self)
            return None
        record_id = int(sel[0])
        return get_borrow_record(record_id)

    def _get_selected_approval(self):
        sel = self.approval_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一条待审批记录", parent=self)
            return None
        record_id = int(sel[0])
        return get_borrow_record(record_id)

    def _add_part(self):
        dlg = PartDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            try:
                create_part(dlg.result, self.current_user["id"])
                messagebox.showinfo("成功", "备件创建成功", parent=self)
                self._refresh_all()
            except BusinessException as e:
                messagebox.showerror("错误", e.message, parent=self)

    def _edit_part(self):
        part = self._get_selected_part()
        if not part:
            return
        dlg = PartDialog(self, part)
        self.wait_window(dlg)
        if dlg.result:
            try:
                update_part(part["id"], dlg.result, self.current_user["id"])
                messagebox.showinfo("成功", "备件更新成功", parent=self)
                self._refresh_all()
            except BusinessException as e:
                messagebox.showerror("错误", e.message, parent=self)

    def _delete_part(self):
        part = self._get_selected_part()
        if not part:
            return
        if not messagebox.askyesno("确认", f"确定要停用备件 {part['part_code']} {part['part_name']} 吗？", parent=self):
            return
        try:
            delete_part(part["id"], self.current_user["id"])
            messagebox.showinfo("成功", "备件已停用", parent=self)
            self._refresh_all()
        except BusinessException as e:
            messagebox.showerror("错误", e.message, parent=self)

    def _adjust_stock(self):
        part = self._get_selected_part()
        if not part:
            return
        dlg = StockAdjustDialog(self, part)
        self.wait_window(dlg)
        if dlg.result:
            try:
                adjust_stock(part["id"], dlg.result["quantity"],
                             self.current_user["id"], dlg.result["remark"])
                messagebox.showinfo("成功", "库存调整成功", parent=self)
                self._refresh_all()
            except BusinessException as e:
                messagebox.showerror("错误", e.message, parent=self)

    def _submit_borrow(self):
        part = self._get_selected_part()
        if not part:
            return
        if part["available_stock"] <= 0:
            messagebox.showwarning("提示", "该备件当前无可用库存", parent=self)
            return
        dlg = BorrowDialog(self, part, self.current_user)
        self.wait_window(dlg)
        if dlg.result:
            try:
                submit_borrow(part["id"], self.current_user["id"],
                              dlg.result["quantity"], dlg.result["purpose"])
                messagebox.showinfo("成功", "借用申请提交成功", parent=self)
                self._refresh_all()
            except BusinessException as e:
                messagebox.showerror("失败", e.message, parent=self)

    def _return_part(self):
        record = self._get_selected_borrow()
        if not record:
            return
        if record["status"] not in ("approved", "borrowed"):
            messagebox.showwarning("提示", f"当前记录状态为 {STATUS_DISPLAY[record['status']][0]}，无法归还", parent=self)
            return
        dlg = ReturnDialog(self, record)
        self.wait_window(dlg)
        if dlg.result:
            try:
                return_part(record["id"], self.current_user["id"],
                            dlg.result["quantity"], dlg.result["remark"])
                messagebox.showinfo("成功", "归还登记成功", parent=self)
                self._refresh_all()
            except BusinessException as e:
                messagebox.showerror("失败", e.message, parent=self)

    def _undo_return_record(self):
        record = self._get_selected_borrow()
        if not record:
            return
        if record["status"] != "returned":
            messagebox.showwarning("提示", "只有已归还的记录可以撤销归还", parent=self)
            return
        if not messagebox.askyesno("确认撤销归还",
                                   f"确定要撤销记录 {record['record_no']} 的归还？\n"
                                   f"这将扣除 {record['return_quantity']}{record['unit']} 对应库存，记录恢复为已借出状态。",
                                   parent=self):
            return
        try:
            undo_return(record["id"], self.current_user["id"], "界面撤销归还")
            messagebox.showinfo("成功", "归还已撤销，库存已回扣，记录恢复为已借出", parent=self)
            self._refresh_all()
        except BusinessException as e:
            messagebox.showerror("失败", e.message, parent=self)

    def _rollback_record(self):
        record = self._get_selected_borrow()
        if not record:
            return
        if record["status"] not in ("approved", "borrowed"):
            messagebox.showwarning("提示", "只有已借出的记录可以异常回滚", parent=self)
            return
        if record["return_quantity"] > 0:
            messagebox.showwarning("提示", "存在部分归还的记录无法整体回滚", parent=self)
            return
        if not messagebox.askyesno("确认回滚", f"确定要异常回滚记录 {record['record_no']}？\n这将恢复对应库存数量。", parent=self):
            return
        try:
            rollback_borrow(record["id"], self.current_user["id"], "界面异常回滚")
            messagebox.showinfo("成功", "回滚完成，库存已恢复", parent=self)
            self._refresh_all()
        except BusinessException as e:
            messagebox.showerror("失败", e.message, parent=self)

    def _cancel_record(self):
        record = self._get_selected_borrow()
        if not record:
            return
        if record["status"] != "pending_approval":
            messagebox.showwarning("提示", "只有待审批的记录可以撤销", parent=self)
            return
        if record["borrower_id"] != self.current_user["id"] and self.current_user["role"] != "supervisor":
            messagebox.showwarning("提示", "只能撤销自己提交的申请", parent=self)
            return
        if not messagebox.askyesno("确认", f"确定撤销申请 {record['record_no']}？", parent=self):
            return
        try:
            cancel_borrow(record["id"], self.current_user["id"])
            messagebox.showinfo("成功", "申请已撤销", parent=self)
            self._refresh_all()
        except BusinessException as e:
            messagebox.showerror("失败", e.message, parent=self)

    def _approve_record(self):
        record = self._get_selected_approval()
        if not record:
            record = self._get_selected_borrow()
            if record and record["status"] != "pending_approval":
                record = None
        if not record:
            messagebox.showinfo("提示", "请选择一条待审批记录", parent=self)
            return
        dlg = ApprovalDialog(self, record, "approve")
        self.wait_window(dlg)
        if dlg.result:
            try:
                approve_borrow(record["id"], self.current_user["id"], dlg.result["remark"])
                messagebox.showinfo("成功", "审批通过", parent=self)
                self._refresh_all()
            except BusinessException as e:
                messagebox.showerror("失败", e.message, parent=self)

    def _reject_record(self):
        record = self._get_selected_approval()
        if not record:
            record = self._get_selected_borrow()
            if record and record["status"] != "pending_approval":
                record = None
        if not record:
            messagebox.showinfo("提示", "请选择一条待审批记录", parent=self)
            return
        dlg = ApprovalDialog(self, record, "reject")
        self.wait_window(dlg)
        if dlg.result:
            try:
                reject_borrow(record["id"], self.current_user["id"], dlg.result["remark"])
                messagebox.showinfo("成功", "已驳回", parent=self)
                self._refresh_all()
            except BusinessException as e:
                messagebox.showerror("失败", e.message, parent=self)

    def _export_stock(self):
        filename = generate_default_filename("stock_details")
        path = filedialog.asksaveasfilename(
            parent=self, title="导出库存明细", defaultextension=".csv",
            initialfile=filename, filetypes=[("CSV文件", "*.csv")]
        )
        if path:
            try:
                count = export_stock_details(path)
                messagebox.showinfo("成功", f"已导出 {count} 条库存记录到:\n{path}", parent=self)
            except Exception as e:
                messagebox.showerror("错误", f"导出失败: {e}", parent=self)

    def _export_borrow(self):
        status_label = self.borrow_status.get()
        status = self._status_map.get(status_label, "")
        status = status or None
        keyword = self.borrow_keyword.get().strip() or None
        borrower_label = self.borrow_person.get()
        borrower_id = self._borrower_map.get(borrower_label)
        date_from = self.date_from_var.get().strip() or None
        date_to = self.date_to_var.get().strip() or None
        filters = {}
        if status:
            filters["status"] = status
        if keyword:
            filters["keyword"] = keyword
        if borrower_id:
            filters["borrower_id"] = borrower_id
        if date_from:
            filters["date_from"] = date_from
        if date_to:
            filters["date_to"] = date_to
        filename = generate_default_filename("borrow_records")
        path = filedialog.asksaveasfilename(
            parent=self, title="导出借还记录", defaultextension=".csv",
            initialfile=filename, filetypes=[("CSV文件", "*.csv")]
        )
        if path:
            try:
                count = export_borrow_records(path, status=status, borrower_id=borrower_id,
                                              keyword=keyword, date_from=date_from, date_to=date_to)
                scheme_id = self.active_scheme_id if hasattr(self, 'active_scheme_id') else None
                if self.current_user:
                    log_export_operation(self.current_user["id"], filters, count,
                                         os.path.basename(path), scheme_id)
                scheme_note = ""
                if self.active_scheme_id:
                    scheme = get_filter_scheme_by_id(self.active_scheme_id)
                    if scheme:
                        scheme_note = f" (按方案「{scheme['name']}」筛选)"
                messagebox.showinfo("成功", f"已导出 {count} 条记录到:\n{path}{scheme_note}", parent=self)
            except Exception as e:
                messagebox.showerror("错误", f"导出失败: {e}", parent=self)

    def _export_history(self):
        label = self.history_part.get()
        part_id = self.history_part_map.get(label)
        filename = generate_default_filename("stock_logs")
        path = filedialog.asksaveasfilename(
            parent=self, title="导出库存变动历史", defaultextension=".csv",
            initialfile=filename, filetypes=[("CSV文件", "*.csv")]
        )
        if path:
            try:
                count = export_stock_logs(path, part_id)
                messagebox.showinfo("成功", f"已导出 {count} 条变动记录到:\n{path}", parent=self)
            except Exception as e:
                messagebox.showerror("错误", f"导出失败: {e}", parent=self)

    def _set_status(self, text):
        self.status_label.configure(text=text)


def main():
    init_db()
    seed_sample_data()
    app = MainApp()
    app.mainloop()


if __name__ == "__main__":
    main()
