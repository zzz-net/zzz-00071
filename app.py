import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
from database import init_db, seed_sample_data
from services import (
    get_all_users, get_user_by_username, get_all_parts, get_all_categories,
    get_part_by_id, create_part, update_part, adjust_stock, delete_part,
    submit_borrow, approve_borrow, reject_borrow, return_part, rollback_borrow,
    cancel_borrow, get_borrow_records, get_borrow_record, get_stock_logs,
    get_operation_logs, STATUS_DISPLAY, OPERATION_DISPLAY, BusinessException
)
from exporter import (
    export_stock_details, export_borrow_records, export_stock_logs,
    generate_default_filename
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

        self._build_parts_tab()
        self._build_borrow_tab()
        self._build_approval_tab()
        self._build_history_tab()
        self._build_logs_tab()

        self.status = ttk.Frame(self, padding=(15, 5), relief="sunken")
        self.status.pack(fill="x")
        self.status_label = ttk.Label(self.status, text="就绪", style="Status.TLabel")
        self.status_label.pack(side="left")

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

        filter_frame = ttk.Frame(tab)
        filter_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(filter_frame, text="状态:").pack(side="left")
        self.borrow_status = tk.StringVar()
        statuses = [("全部", ""), ("待审批", "pending_approval"), ("已借出", "approved"),
                    ("已归还", "returned"), ("已驳回", "rejected"), ("已回滚", "rollback"),
                    ("已撤销", "cancelled")]
        self.borrow_status_combo = ttk.Combobox(
            filter_frame, textvariable=self.borrow_status, state="readonly", width=12,
            values=[s[0] for s in statuses]
        )
        self.borrow_status_combo.current(0)
        self.borrow_status_combo.pack(side="left", padx=5)
        self._status_map = {s[0]: s[1] for s in statuses}
        ttk.Label(filter_frame, text="搜索:").pack(side="left", padx=(15, 0))
        self.borrow_keyword = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.borrow_keyword, width=25).pack(side="left", padx=5)
        ttk.Button(filter_frame, text="查询", command=self._refresh_borrow, width=8).pack(side="left", padx=5)

        btn_frame = ttk.Frame(tab)
        btn_frame.pack(fill="x", pady=(0, 8))
        self.btn_return = ttk.Button(btn_frame, text="归还", command=self._return_part, width=10)
        self.btn_return.pack(side="left", padx=3)
        self.btn_rollback = ttk.Button(btn_frame, text="异常回滚", command=self._rollback_record, width=10)
        self.btn_rollback.pack(side="left", padx=3)
        self.btn_cancel = ttk.Button(btn_frame, text="撤销申请", command=self._cancel_record, width=10)
        self.btn_cancel.pack(side="left", padx=3)
        ttk.Separator(btn_frame, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(btn_frame, text="导出借还记录", command=self._export_borrow, width=14).pack(side="left", padx=3)

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
            self._refresh_all()
        else:
            self.destroy()

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
        self._refresh_parts()
        self._refresh_borrow()
        self._refresh_approval()
        self._refresh_history()
        self._refresh_logs()

    def _reset_parts_filter(self):
        self.parts_keyword.set("")
        self.parts_category.set("")
        self.parts_category_combo.configure(values=[""] + get_all_categories())
        self.parts_category_combo.current(0)
        self._refresh_parts()

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

    def _refresh_borrow(self):
        status_label = self.borrow_status.get()
        status = self._status_map.get(status_label, "")
        status = status or None
        keyword = self.borrow_keyword.get().strip() or None
        for item in self.borrow_tree.get_children():
            self.borrow_tree.delete(item)
        records = get_borrow_records(status=status, keyword=keyword)
        for r in records:
            status_text, _ = STATUS_DISPLAY.get(r["status"], (r["status"], ""))
            self.borrow_tree.insert("", "end", iid=str(r["id"]), values=(
                r["record_no"], r["part_code"], r["part_name"],
                r["quantity"], r["unit"], r["borrower_name"],
                r.get("purpose", "") or "", status_text,
                r.get("approver_name", "") or "",
                r["created_at"], r["return_quantity"]
            ), tags=(r["status"],))
        self._set_status(f"借还记录查询完成，共 {len(records)} 条记录")

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
        filename = generate_default_filename("borrow_records")
        path = filedialog.asksaveasfilename(
            parent=self, title="导出借还记录", defaultextension=".csv",
            initialfile=filename, filetypes=[("CSV文件", "*.csv")]
        )
        if path:
            try:
                count = export_borrow_records(path, status)
                messagebox.showinfo("成功", f"已导出 {count} 条记录到:\n{path}", parent=self)
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
