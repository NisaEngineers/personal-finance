import os, sys, sqlite3, csv, requests
from datetime import datetime, date
from PySide6.QtCore import Qt, QDate
from PySide6.QtGui import QFont, QColor
from PySide6.QtCore import Qt, QDate, QPropertyAnimation, QEasingCurve, QRect
from PySide6.QtGui import QAction, QFontDatabase, QIcon, QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QTableWidget, QTableWidgetItem,
    QDateEdit, QCheckBox, QMessageBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QFrame, QGridLayout, QGraphicsDropShadowEffect
)


from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure

DB_PATH = os.path.join(os.path.expanduser("~"), ".finx.db")
SUPPORTED_CURRENCIES = ["BDT", "USD"]
ACCOUNT_TYPES = ["fixed", "liquid"]

# ---------------- DB ----------------
class DB:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def init_schema(self):
        c = self.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS accounts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, currency TEXT, balance REAL, type TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, kind TEXT, is_default INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS expenses(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, amount REAL, currency TEXT, account_id INTEGER,
            category_id INTEGER, is_upcoming INTEGER, date TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS incomes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, description TEXT, amount REAL, currency TEXT,
            account_id INTEGER, is_upcoming INTEGER, expected_date TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS transfers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_account INTEGER, to_account INTEGER, amount REAL, currency TEXT, date TEXT)""")
        self.conn.commit()

    def query(self, sql, params=()):
        cur = self.conn.cursor(); cur.execute(sql, params); return cur.fetchall()
    def execute(self, sql, params=()):
        cur = self.conn.cursor(); cur.execute(sql, params); self.conn.commit(); return cur.lastrowid

# ---------------- Currency ----------------
class CurrencyService:
    def __init__(self): self.cache = {}
    def fetch_rate(self, base, target):
        if base == target: return 1.0
        key = (base, target)
        if key in self.cache: return self.cache[key]
        try:
            r = requests.get(f"https://api.exchangerate.host/latest?base={base}&symbols={target}", timeout=3)
            rate = r.json()["rates"][target]; self.cache[key] = rate; return rate
        except: return 120.0 if (base, target)==("USD","BDT") else 1/120.0
    def convert(self, amt, base, target): return amt*self.fetch_rate(base,target)

# ---------------- Finance ----------------
class FinanceService:
    def __init__(self, db, fx): self.db, self.fx = db, fx
    def add_account(self, name,currency,balance,atype):
        return self.db.execute("INSERT INTO accounts VALUES(NULL,?,?,?,?,?)",
            (name,currency,balance,atype,datetime.utcnow().isoformat()))
    def list_accounts(self): return self.db.query("SELECT * FROM accounts")
    def update_balance(self, acc_id, new_bal): self.db.execute("UPDATE accounts SET balance=? WHERE id=?",(new_bal,acc_id))
    def add_expense(self,name,amt,currency,acc_id,cat_id,is_upcoming,when):
        self.db.execute("INSERT INTO expenses VALUES(NULL,?,?,?,?,?,?,?,?)",
            (name,amt,currency,acc_id,cat_id,1 if is_upcoming else 0,when.isoformat(),datetime.utcnow().isoformat()))
        if not is_upcoming:
            acc=self.db.query("SELECT * FROM accounts WHERE id=?",(acc_id,))[0]
            debit=self.fx.convert(amt,currency,acc["currency"]) if currency!=acc["currency"] else amt
            self.update_balance(acc_id, acc["balance"]-debit)
    def list_expenses(self): return self.db.query("SELECT * FROM expenses ORDER BY date DESC")
    def add_income(self,src,desc,amt,currency,acc_id,is_upcoming,when):
        self.db.execute("INSERT INTO incomes VALUES(NULL,?,?,?,?,?,?,?,?)",
            (src,desc,amt,currency,acc_id,1 if is_upcoming else 0,when.isoformat(),datetime.utcnow().isoformat()))
        if not is_upcoming:
            acc=self.db.query("SELECT * FROM accounts WHERE id=?",(acc_id,))[0]
            credit=self.fx.convert(amt,currency,acc["currency"]) if currency!=acc["currency"] else amt
            self.update_balance(acc_id, acc["balance"]+credit)
    def list_incomes(self): return self.db.query("SELECT * FROM incomes ORDER BY expected_date DESC")
    def transfer(self,from_id,to_id,amt,currency):
        f=self.db.query("SELECT * FROM accounts WHERE id=?",(from_id,))[0]
        t=self.db.query("SELECT * FROM accounts WHERE id=?",(to_id,))[0]
        debit=self.fx.convert(amt,currency,f["currency"]) if currency!=f["currency"] else amt
        credit=self.fx.convert(amt,currency,t["currency"]) if currency!=t["currency"] else amt
        self.update_balance(from_id,f["balance"]-debit)
        self.update_balance(to_id,t["balance"]+credit)
        self.db.execute("INSERT INTO transfers VALUES(NULL,?,?,?,?,?)",(from_id,to_id,amt,currency,datetime.utcnow().isoformat()))

# ---------------- UI helpers ----------------
def card(title,value,color="#2a3163"):
    f=QFrame(); f.setMinimumSize(220,120); f.setMaximumHeight(150)
    layout=QVBoxLayout(f); layout.setContentsMargins(16,12,16,12)
    t=QLabel(title); t.setFont(QFont("Segoe UI",10, QFont.Bold)); t.setStyleSheet("color:#cfd2ff;")
    v=QLabel(value); v.setFont(QFont("Segoe UI",22,QFont.Bold)); v.setStyleSheet("color:white;")
    layout.addWidget(t); layout.addWidget(v)
    shadow=QGraphicsDropShadowEffect(); shadow.setBlurRadius(20); shadow.setOffset(0,6); shadow.setColor(QColor(0,0,0,160))
    f.setGraphicsEffect(shadow); f.setStyleSheet(f"background-color:{color}; border-radius:12px;")
    return f

class ChartWidget(QWidget):
    def __init__(self): super().__init__(); v=QVBoxLayout(self); self.fig=Figure(figsize=(4,2.4)); self.canvas=FigureCanvas(self.fig); v.addWidget(self.canvas)
    def plot(self,days,incomes,expenses):
        self.fig.clear(); ax=self.fig.add_subplot(111)
        ax.plot(days,incomes,label="Income",color="green"); ax.plot(days,expenses,label="Expenses",color="red")
        ax.legend(); self.canvas.draw()

# ---------------- Pages ----------------
class HomePage(QWidget):
    def __init__(self, finance: FinanceService):
        super().__init__()
        self.finance = finance
        self.setLayout(QVBoxLayout())

        # Dashboard cards grid
        self.cards_grid = QGridLayout()
        self.layout().addLayout(self.cards_grid)

        # Cashflow chart
        self.chart = ChartWidget()
        self.layout().addWidget(self.chart)

        self.refresh()

    def refresh(self):
        # Totals
        accounts = self.finance.list_accounts()
        expenses = self.finance.list_expenses()
        incomes = self.finance.list_incomes()

        total_balance = sum(a["balance"] for a in accounts)
        c1 = card("Total Balance", f"{total_balance:.2f}", "#2a3163")
        c2 = card("Accounts", str(len(accounts)), "#1e7a63")
        c3 = card("Expenses", str(len(expenses)), "#7a1e3a")
        c4 = card("Incomes", str(len(incomes)), "#1e4a7a")

        cards = [c1, c2, c3, c4]

        # Clear old cards
        while self.cards_grid.count():
            item = self.cards_grid.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)

        # Responsive layout
        width = self.width()
        if width > 1000:
            per_row = 4
        elif width > 700:
            per_row = 2
        else:
            per_row = 1

        row, col = 0, 0
        for c in cards:
            self.cards_grid.addWidget(c, row, col, alignment=Qt.AlignTop)
            col += 1
            if col >= per_row:
                col = 0
                row += 1

        # Simple synthetic chart (replace with real summaries if you like)
        days = ["D", "W", "M"]
        incomes_vals = [sum(i["amount"] for i in incomes if not i["is_upcoming"])] * 3
        expenses_vals = [sum(e["amount"] for e in expenses if not e["is_upcoming"])] * 3
        self.chart.plot(days, incomes_vals, expenses_vals)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.refresh()
        
        
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

        self.db = DB()
        self.fx = CurrencyService()
        self.finance = FinanceService(self.db, self.fx)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        self.home = HomePage(self.finance)
        self.accounts = AccountsPage(self.finance)   # you already have this
        self.expenses = ExpensesPage(self.finance)   # you already have this
        self.income = IncomePage(self.finance)       # you already have this
        self.settings = SettingsPage(self.fx, self.finance)  # you already have this

        self.tabs.addTab(self.home, "Home")
        self.tabs.addTab(self.accounts, "Accounts")
        self.tabs.addTab(self.expenses, "Expenses")
        self.tabs.addTab(self.income, "Income")
        self.tabs.addTab(self.settings, "Settings")

    def refresh_all(self):
        self.home.refresh()
        self.accounts.refresh()
        self.expenses.refresh()
        self.income.refresh()
def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
