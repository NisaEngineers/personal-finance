import os
import sqlite3
import sys
from datetime import datetime, date
from typing import Optional, List, Dict

import requests
from dateutil.parser import parse as parse_date

from PySide6.QtCore import Qt, QDate, QPropertyAnimation, QEasingCurve, QRect
from PySide6.QtGui import QAction, QFontDatabase, QIcon, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QDateEdit, QCheckBox, QMessageBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QFrame, QGridLayout, QGraphicsDropShadowEffect
)

# Matplotlib for charts
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

DB_PATH = os.path.join(os.path.expanduser("~"), ".finx.db")
SUPPORTED_CURRENCIES = ["BDT", "USD"]
ACCOUNT_TYPES = ["fixed", "liquid"]

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
ICON_DIR = os.path.join(ASSETS_DIR, "icons")

# --------------------- Database Layer ---------------------
class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self):
        c = self.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            currency TEXT NOT NULL CHECK(currency IN ('BDT','USD')),
            balance REAL NOT NULL DEFAULT 0.0,
            type TEXT NOT NULL CHECK(type IN ('fixed','liquid')),
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL CHECK(kind IN ('expense','income')),
            is_default INTEGER NOT NULL DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS expenses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL CHECK(currency IN ('BDT','USD')),
            account_id INTEGER NOT NULL,
            category_id INTEGER,
            is_upcoming INTEGER NOT NULL DEFAULT 0,
            date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(id),
            FOREIGN KEY(category_id) REFERENCES categories(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS incomes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            description TEXT,
            amount REAL NOT NULL,
            currency TEXT NOT NULL CHECK(currency IN ('BDT','USD')),
            is_upcoming INTEGER NOT NULL DEFAULT 0,
            expected_date TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        self.conn.commit()
        self.seed_defaults()

    def seed_defaults(self):
        c = self.conn.cursor()
        defaults = [
            ("Groceries", "expense", 1),
            ("Rent", "expense", 1),
            ("Utilities", "expense", 1),
            ("Transport", "expense", 1),
            ("Dining", "expense", 1),
            ("Healthcare", "expense", 1),
            ("Entertainment", "expense", 1),
            ("Salary", "income", 1),
            ("Freelance", "income", 1),
            ("Investment", "income", 1),
        ]
        for name, kind, is_default in defaults:
            c.execute("SELECT 1 FROM categories WHERE name=? AND kind=?", (name, kind))
            if not c.fetchone():
                c.execute("INSERT INTO categories(name, kind, is_default) VALUES(?,?,?)",
                          (name, kind, is_default))
        c.execute("SELECT 1 FROM settings WHERE key='display_currency'")
        if not c.fetchone():
            c.execute("INSERT INTO settings(key,value) VALUES('display_currency','BDT')")
        self.conn.commit()

    def query(self, sql: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def execute(self, sql: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

# --------------------- Currency Service ---------------------
class CurrencyService:
    def __init__(self, db: DB):
        self.db = db
        self.cache = {}
        self.fallback_rate = {
            ("USD", "BDT"): 120.0,
            ("BDT", "USD"): 1/120.0
        }

    def get_display_currency(self) -> str:
        row = self.db.query("SELECT value FROM settings WHERE key='display_currency'")
        return row[0]["value"] if row else "BDT"

    def set_display_currency(self, cur: str):
        if cur not in SUPPORTED_CURRENCIES:
            raise ValueError("Unsupported display currency")
        self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('display_currency',?)", (cur,))

    def fetch_rate(self, base: str, target: str) -> float:
        if base == target:
            return 1.0
        key = (base, target)
        if key in self.cache:
            return self.cache[key]
        try:
            url = f"https://api.exchangerate.host/latest?base={base}&symbols={target}"
            resp = requests.get(url, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                rate = float(data["rates"][target])
                self.cache[key] = rate
                return rate
        except Exception:
            pass
        return self.fallback_rate.get(key, 1.0)

    def convert(self, amount: float, base: str, target: str) -> float:
        return amount * self.fetch_rate(base, target)

# --------------------- Finance Service ---------------------
class FinanceService:
    def __init__(self, db: DB, fx: CurrencyService):
        self.db = db
        self.fx = fx

    def add_account(self, name: str, currency: str, balance: float, acc_type: str):
        if currency not in SUPPORTED_CURRENCIES:
            raise ValueError("Unsupported currency")
        if acc_type not in ACCOUNT_TYPES:
            raise ValueError("Unsupported account type")
        now = datetime.utcnow().isoformat()
        return self.db.execute(
            "INSERT INTO accounts(name,currency,balance,type,created_at) VALUES(?,?,?,?,?)",
            (name, currency, balance, acc_type, now)
        )

    def list_accounts(self) -> List[sqlite3.Row]:
        return self.db.query("SELECT * FROM accounts ORDER BY created_at DESC")

    def update_balance(self, account_id: int, new_balance: float):
        self.db.execute("UPDATE accounts SET balance=? WHERE id=?", (new_balance, account_id))

    def list_categories(self, kind: str) -> List[sqlite3.Row]:
        return self.db.query("SELECT * FROM categories WHERE kind=? ORDER BY is_default DESC, name ASC", (kind,))

    def add_category(self, name: str, kind: str):
        if kind not in ("expense", "income"):
            raise ValueError("kind must be 'expense' or 'income'")
        self.db.execute("INSERT INTO categories(name,kind,is_default) VALUES(?,?,0)", (name, kind))

    def add_expense(self, name: str, amount: float, currency: str, account_id: int,
                    category_id: Optional[int], is_upcoming: bool, when: date):
        now = datetime.utcnow().isoformat()
        self.db.execute("""INSERT INTO expenses
            (name, amount, currency, account_id, category_id, is_upcoming, date, created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (name, amount, currency, account_id, category_id, 1 if is_upcoming else 0,
             when.isoformat(), now)
        )
        if not is_upcoming:
            acc = self.db.query("SELECT balance, currency FROM accounts WHERE id=?", (account_id,))
            if acc:
                acc_cur = acc[0]["currency"]
                debit = self.fx.convert(amount, currency, acc_cur) if currency != acc_cur else amount
                self.update_balance(account_id, acc[0]["balance"] - debit)

    def list_expenses(self) -> List[sqlite3.Row]:
        return self.db.query("SELECT * FROM expenses ORDER BY date DESC, created_at DESC")

    def add_income(self, source: str, description: str, amount: float, currency: str,
                   is_upcoming: bool, expected_date: date):
        now = datetime.utcnow().isoformat()
        self.db.execute("""INSERT INTO incomes
            (source, description, amount, currency, is_upcoming, expected_date, created_at)
            VALUES(?,?,?,?,?,?,?)""",
            (source, description, amount, currency, 1 if is_upcoming else 0,
             expected_date.isoformat(), now)
        )
        if not is_upcoming:
            accounts = self.list_accounts()
            target_acc = None
            for a in accounts:
                if a["type"] == "liquid" and a["currency"] == currency:
                    target_acc = a
                    break
            if target_acc is None and accounts:
                target_acc = accounts[0]
            if target_acc:
                acc_cur = target_acc["currency"]
                credit = self.fx.convert(amount, currency, acc_cur) if currency != acc_cur else amount
                self.update_balance(target_acc["id"], target_acc["balance"] + credit)

    def list_incomes(self) -> List[sqlite3.Row]:
        return self.db.query("SELECT * FROM incomes ORDER BY expected_date DESC, created_at DESC")

    def totals(self) -> Dict[str, float]:
        display_cur = self.fx.get_display_currency()
        accounts = self.list_accounts()
        total_fixed = 0.0
        total_liquid = 0.0
        for a in accounts:
            amt = a["balance"] if a["currency"] == display_cur else self.fx.convert(a["balance"], a["currency"], display_cur)
            if a["type"] == "fixed":
                total_fixed += amt
            else:
                total_liquid += amt
        return {
            "display_currency": display_cur,
            "total_fixed": round(total_fixed, 2),
            "total_liquid": round(total_liquid, 2),
            "total_all": round(total_fixed + total_liquid, 2)
        }

    def upcoming_totals(self) -> Dict[str, float]:
        display_cur = self.fx.get_display_currency()
        exp = self.db.query("SELECT amount, currency FROM expenses WHERE is_upcoming=1")
        inc = self.db.query("SELECT amount, currency FROM incomes WHERE is_upcoming=1")
        up_exp = sum(self.fx.convert(e["amount"], e["currency"], display_cur) for e in exp)
        up_inc = sum(self.fx.convert(i["amount"], i["currency"], display_cur) for i in inc)
        return {
            "display_currency": display_cur,
            "upcoming_expenses": round(up_exp, 2),
            "upcoming_incomes": round(up_inc, 2),
            "projected_total": round(self.totals()["total_all"] - up_exp + up_inc, 2)
        }

    def period_summary(self, period: str) -> Dict[str, float]:
        display_cur = self.fx.get_display_currency()
        today = date.today()
        if period == "daily":
            start = today
        elif period == "weekly":
            start = date.fromisocalendar(today.isocalendar()[0], today.isocalendar()[1], 1)
        elif period == "monthly":
            start = date(today.year, today.month, 1)
        else:
            raise ValueError("period must be daily/weekly/monthly")

        exp = self.db.query("SELECT amount, currency FROM expenses WHERE date>=?", (start.isoformat(),))
        inc = self.db.query("SELECT amount, currency FROM incomes WHERE expected_date>=?", (start.isoformat(),))
        total_exp = sum(self.fx.convert(e["amount"], e["currency"], display_cur) for e in exp)
        total_inc = sum(self.fx.convert(i["amount"], i["currency"], display_cur) for i in inc)
        return {
            "display_currency": display_cur,
            "period": period,
            "expenses": round(total_exp, 2),
            "income": round(total_inc, 2),
            "net": round(total_inc - total_exp, 2)
        }

# --------------------- UI helpers ---------------------
from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel, QGraphicsDropShadowEffect
from PySide6.QtGui import QFont, QColor

def card(title: str, value: str, accent_class: str) -> QFrame:
    """
    Create a styled dashboard card with title + value.
    accent_class should match a QSS style (e.g. 'card-blue', 'card-green').
    """
    f = QFrame()
    f.setObjectName(accent_class)
    f.setProperty("class", accent_class)

    # Uniform sizing for alignment
    f.setMinimumSize(220, 120)
    f.setMaximumHeight(150)

    # Layout
    layout = QVBoxLayout(f)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(6)

    # Title label
    title_lbl = QLabel(title)
    title_lbl.setStyleSheet("color: #cfd2ff;")
    title_lbl.setFont(QFont("Inter", 11, QFont.Bold))

    # Value label
    value_lbl = QLabel(value)
    value_lbl.setFont(QFont("Inter", 22, QFont.Bold))
    value_lbl.setStyleSheet("color: white;")

    layout.addWidget(title_lbl)
    layout.addWidget(value_lbl)

    # Drop shadow effect
    shadow = QGraphicsDropShadowEffect()
    shadow.setBlurRadius(24)
    shadow.setOffset(0, 6)
    shadow.setColor(QColor(0, 0, 0, 160))
    f.setGraphicsEffect(shadow)

    return f


class ChartWidget(QWidget):
    def __init__(self, title="Chart"):
        super().__init__()
        v = QVBoxLayout(self)
        self.fig = Figure(figsize=(4, 2.4), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet("color:#aeb3ff; font-weight:bold;")
        v.addWidget(self.title_lbl)
        v.addWidget(self.canvas)

    def plot_cashflow(self, days, incomes, expenses):
        self.fig.clear()                       # just clear
        ax = self.fig.add_subplot(111)         # then add subplot
        ax.plot(days, incomes, label="Income", color="#4CAF50")
        ax.plot(days, expenses, label="Expenses", color="#F44336")
        ax.fill_between(days, incomes, expenses, color="#2196F3", alpha=0.15)
        ax.grid(alpha=0.2)
        ax.legend()
        self.canvas.draw()


# --------------------- Pages ---------------------
class AccountsPage(QWidget):
    def __init__(self, finance: FinanceService):
        super().__init__()
        self.finance = finance
        self.setLayout(QVBoxLayout())
        header = QLabel("Accounts")
        header.setStyleSheet("color:#aeb3ff; font-weight:bold; font-size:14pt;")
        self.layout().addWidget(header)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Currency", "Balance", "Type", "Created"])
        self.layout().addWidget(self.table)
        self.layout().addWidget(self._form_group())
        self.refresh()

    def _form_group(self):
        box = QGroupBox("Add account")
        form = QFormLayout()
        self.name_in = QLineEdit()
        self.currency_in = QComboBox(); self.currency_in.addItems(SUPPORTED_CURRENCIES)
        self.balance_in = QDoubleSpinBox(); self.balance_in.setMaximum(1_000_000_000); self.balance_in.setDecimals(2)
        self.type_in = QComboBox(); self.type_in.addItems(ACCOUNT_TYPES)
        add_btn = QPushButton("Create")
        add_btn.clicked.connect(self._add)
        form.addRow("Name", self.name_in)
        form.addRow("Currency", self.currency_in)
        form.addRow("Balance", self.balance_in)
        form.addRow("Type", self.type_in)
        form.addRow(add_btn)
        box.setLayout(form)
        return box

    def _add(self):
        try:
            self.finance.add_account(
                self.name_in.text().strip(),
                self.currency_in.currentText(),
                float(self.balance_in.value()),
                self.type_in.currentText()
            )
            self.refresh()
            QMessageBox.information(self, "Success", "Account added.")
            self.name_in.clear()
            self.balance_in.setValue(0.0)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def refresh(self):
        rows = self.finance.list_accounts()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j, val in enumerate([r["name"], r["currency"], f'{r["balance"]:.2f}', r["type"], r["created_at"]]):
                self.table.setItem(i, j, QTableWidgetItem(str(val)))

class ExpensesPage(QWidget):
    def __init__(self, finance: FinanceService):
        super().__init__()
        self.finance = finance
        self.setLayout(QVBoxLayout())
        header = QLabel("Expenses")
        header.setStyleSheet("color:#aeb3ff; font-weight:bold; font-size:14pt;")
        self.layout().addWidget(header)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Name","Amount","Currency","From Account","Category","Upcoming","Date"])
        self.layout().addWidget(self.table)
        self.layout().addWidget(self._form_group())
        self.refresh()

    def _form_group(self):
        box = QGroupBox("Add expense")
        form = QFormLayout()
        self.name_in = QLineEdit()
        self.amount_in = QDoubleSpinBox(); self.amount_in.setMaximum(1_000_000_000); self.amount_in.setDecimals(2)
        self.currency_in = QComboBox(); self.currency_in.addItems(SUPPORTED_CURRENCIES)
        self.account_in = QComboBox()
        self.category_in = QComboBox()
        self.upcoming_in = QCheckBox("Upcoming")
        self.date_in = QDateEdit(); self.date_in.setDate(QDate.currentDate()); self.date_in.setCalendarPopup(True)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)
        cats = self.finance.list_categories("expense")
        self.category_in.addItem("(None)", userData=None)
        for c in cats:
            self.category_in.addItem(c["name"], userData=c["id"])
        self._reload_accounts()

        form.addRow("Name", self.name_in)
        form.addRow("Amount", self.amount_in)
        form.addRow("Currency", self.currency_in)
        form.addRow("From account", self.account_in)
        form.addRow("Category", self.category_in)
        form.addRow(self.upcoming_in)
        form.addRow("Date", self.date_in)
        form.addRow(add_btn)
        box.setLayout(form)
        return box

    def _reload_accounts(self):
        self.account_in.clear()
        for a in self.finance.list_accounts():
            self.account_in.addItem(f'{a["name"]} ({a["currency"]}/{a["type"]})', userData=a["id"])

    def _add(self):
        try:
            when = self.date_in.date().toPython()
            self.finance.add_expense(
                self.name_in.text().strip(),
                float(self.amount_in.value()),
                self.currency_in.currentText(),
                self.account_in.currentData(),
                self.category_in.currentData(),
                self.upcoming_in.isChecked(),
                when
            )
            self.refresh()
            QMessageBox.information(self, "Success", "Expense added.")
            self.name_in.clear(); self.amount_in.setValue(0.0)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def refresh(self):
        rows = self.finance.list_expenses()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            display = [r["name"], f'{r["amount"]:.2f}', r["currency"], r["account_id"],
                       r["category_id"] if r["category_id"] else "-", "Yes" if r["is_upcoming"] else "No", r["date"]]
            for j, val in enumerate(display):
                self.table.setItem(i, j, QTableWidgetItem(str(val)))

class IncomePage(QWidget):
    def __init__(self, finance: FinanceService):
        super().__init__()
        self.finance = finance
        self.setLayout(QVBoxLayout())
        header = QLabel("Income")
        header.setStyleSheet("color:#aeb3ff; font-weight:bold; font-size:14pt;")
        self.layout().addWidget(header)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Source","Description","Amount","Currency","Upcoming","Expected date"])
        self.layout().addWidget(self.table)
        self.layout().addWidget(self._form_group())
        self.refresh()

    def _form_group(self):
        box = QGroupBox("Add income")
        form = QFormLayout()
        self.source_in = QLineEdit()
        self.desc_in = QLineEdit()
        self.amount_in = QDoubleSpinBox(); self.amount_in.setMaximum(1_000_000_000); self.amount_in.setDecimals(2)
        self.currency_in = QComboBox(); self.currency_in.addItems(SUPPORTED_CURRENCIES)
        self.upcoming_in = QCheckBox("Upcoming")
        self.date_in = QDateEdit(); self.date_in.setDate(QDate.currentDate()); self.date_in.setCalendarPopup(True)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add)

        form.addRow("Source", self.source_in)
        form.addRow("Description", self.desc_in)
        form.addRow("Amount", self.amount_in)
        form.addRow("Currency", self.currency_in)
        form.addRow(self.upcoming_in)
        form.addRow("Expected date", self.date_in)
        form.addRow(add_btn)
        box.setLayout(form)
        return box

    def _add(self):
        try:
            when = self.date_in.date().toPython()
            self.finance.add_income(
                self.source_in.text().strip(),
                self.desc_in.text().strip(),
                float(self.amount_in.value()),
                self.currency_in.currentText(),
                self.upcoming_in.isChecked(),
                when
            )
            self.refresh()
            QMessageBox.information(self, "Success", "Income added.")
            self.source_in.clear(); self.desc_in.clear(); self.amount_in.setValue(0.0)
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def refresh(self):
        rows = self.finance.list_incomes()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            display = [r["source"], r["description"] or "-", f'{r["amount"]:.2f}', r["currency"],
                       "Yes" if r["is_upcoming"] else "No", r["expected_date"]]
            for j, val in enumerate(display):
                self.table.setItem(i, j, QTableWidgetItem(str(val)))

class HomePage(QWidget):
    def __init__(self, finance: FinanceService):
        super().__init__()
        self.finance = finance
        self.setLayout(QVBoxLayout())
        header = QLabel("Dashboard")
        header.setStyleSheet("color:#aeb3ff; font-weight:bold; font-size:16pt;")
        self.layout().addWidget(header)

        # Cards grid
        self.cards_grid = QGridLayout()
        self.cards_grid.setSpacing(20)
        self.cards_grid.setContentsMargins(0, 0, 0, 0)
        self.cards_grid.setColumnStretch(0, 1)
        self.cards_grid.setColumnStretch(1, 1)
        self.cards_grid.setRowStretch(0, 1)
        self.cards_grid.setRowStretch(1, 1)

        self.layout().addLayout(self.cards_grid)

        # Charts
        self.chart = ChartWidget("Cashflow (last 10 periods)")
        self.layout().addWidget(self.chart)

        # Floating add button
        self.fab = QPushButton("＋ Add")
        self.fab.setObjectName("fab")
        self.fab.clicked.connect(self._fab_action)
        self.layout().addWidget(self.fab, alignment=Qt.AlignRight)

        self.refresh()
        self._animate_cards()

    def _animate_cards(self):
        # Simple entrance animations
        for i in range(self.cards_grid.count()):
            item = self.cards_grid.itemAt(i).widget()
            if not item:
                continue
            anim = QPropertyAnimation(item, b"geometry", self)
            rect = item.geometry()
            anim.setDuration(300 + i*80)
            anim.setStartValue(QRect(rect.x(), rect.y()-20, rect.width(), rect.height()))
            anim.setEndValue(rect)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.start()
            
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()   # re‑arrange cards when window size changes


    def _fab_action(self):
        QMessageBox.information(self, "Quick Add", "Use the Expenses or Income tab to add transactions.")

    def refresh(self):
        totals = self.finance.totals()
        upcoming = self.finance.upcoming_totals()
        daily = self.finance.period_summary("daily")
        weekly = self.finance.period_summary("weekly")
        monthly = self.finance.period_summary("monthly")

        # --- Build cards ---
        c1 = card(f"Total ({totals['display_currency']})",
                  f"{totals['total_all']:.2f}", "card-blue")
        c2 = card("Upcoming incomes",
                  f"{upcoming['upcoming_incomes']:.2f}", "card-green")
        c3 = card("Upcoming expenses",
                  f"{upcoming['upcoming_expenses']:.2f}", "card-red")
        c4 = card("Projected total",
                  f"{upcoming['projected_total']:.2f}", "card-gold")

        cards = [c1, c2, c3, c4]

        # --- Clear old cards from the grid ---
        while self.cards_grid.count():
            item = self.cards_grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)

        # --- Decide how many per row based on window width ---
        width = self.width()
        if width > 1000:
            per_row = 4   # all in one row
        elif width > 700:
            per_row = 2   # 2x2 grid
        else:
            per_row = 1   # stacked vertically

        # --- Add cards accordingly ---
        row, col = 0, 0
        for card_widget in cards:
            self.cards_grid.addWidget(card_widget, row, col, alignment=Qt.AlignTop)
            col += 1
            if col >= per_row:
                col = 0
                row += 1

        # --- Cashflow chart (synthetic from period summaries) ---
        # --- Cashflow chart (synthetic from period summaries) ---
        days = ["D", "W", "M"]
        incomes = [daily["income"], weekly["income"], monthly["income"]]
        expenses = [daily["expenses"], weekly["expenses"], monthly["expenses"]]
        self.chart.plot_cashflow(days, incomes, expenses)

class SettingsPage(QWidget):
    def __init__(self, fx: CurrencyService, finance: FinanceService):
        super().__init__()
        self.fx = fx
        self.finance = finance
        self.setLayout(QVBoxLayout())
        header = QLabel("Settings")
        header.setStyleSheet("color:#aeb3ff; font-weight:bold; font-size:14pt;")
        self.layout().addWidget(header)
        self.layout().addWidget(self._display_currency_group())
        self.layout().addWidget(self._category_group())

    def _display_currency_group(self):
        box = QGroupBox("Display currency")
        h = QHBoxLayout()
        self.dcur = QComboBox(); self.dcur.addItems(SUPPORTED_CURRENCIES)
        self.dcur.setCurrentText(self.fx.get_display_currency())
        btn = QPushButton("Save")
        btn.clicked.connect(self._save_cur)
        h.addWidget(QLabel("Currency shown on Home:"))
        h.addWidget(self.dcur)
        h.addStretch()
        h.addWidget(btn)
        box.setLayout(h)
        return box

    def _save_cur(self):
        try:
            self.fx.set_display_currency(self.dcur.currentText())
            QMessageBox.information(self, "Saved", "Display currency updated.")
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def _category_group(self):
        box = QGroupBox("Custom categories")
        form = QFormLayout()
        self.cat_name = QLineEdit()
        self.cat_kind = QComboBox(); self.cat_kind.addItems(["expense","income"])
        btn = QPushButton("Add category")
        btn.clicked.connect(self._add_cat)
        form.addRow("Name", self.cat_name)
        form.addRow("Kind", self.cat_kind)
        form.addRow(btn)
        box.setLayout(form)
        return box

    def _add_cat(self):
        try:
            self.finance.add_category(self.cat_name.text().strip(), self.cat_kind.currentText())
            QMessageBox.information(self, "Success", "Category added.")
            self.cat_name.clear()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FinX — Finance Manager")
        self.resize(1100, 720)

        # Fonts
        font_path = os.path.join(ASSETS_DIR, "fonts", "Inter-Regular.ttf")
        if os.path.exists(font_path):
            QFontDatabase.addApplicationFont(font_path)

        # Icons (optional)
        self.setWindowIcon(QIcon(os.path.join(ICON_DIR, "wallet.svg")) if os.path.exists(ICON_DIR) else QIcon())

        self.db = DB(DB_PATH)
        self.fx = CurrencyService(self.db)
        self.finance = FinanceService(self.db, self.fx)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.home = HomePage(self.finance)
        self.accounts = AccountsPage(self.finance)
        self.expenses = ExpensesPage(self.finance)
        self.income = IncomePage(self.finance)
        self.settings = SettingsPage(self.fx, self.finance)

        self.tabs.addTab(self.home, "Home")
        self.tabs.addTab(self.accounts, "Accounts")
        self.tabs.addTab(self.expenses, "Expenses")
        self.tabs.addTab(self.income, "Income")
        self.tabs.addTab(self.settings, "Settings")
        self._menu()

    def _menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("File")
        refresh_action = QAction(QIcon(os.path.join(ICON_DIR, "refresh.svg")), "Refresh", self)
        refresh_action.triggered.connect(self._refresh_all)
        file_menu.addAction(refresh_action)

    def _refresh_all(self):
        self.home.refresh()
        self.accounts.refresh()
        self.expenses.refresh()
        self.income.refresh()

def main():
    app = QApplication(sys.argv)
    # Apply QSS theme
    theme_path = os.path.join(ASSETS_DIR, "theme.qss")
    if os.path.exists(theme_path):
        with open(theme_path, "r", encoding="utf-8") as f:
            app.setStyleSheet(f.read())

    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
