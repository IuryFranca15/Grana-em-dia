"""Funções de dados, classificação, importação e análises do Radar de Gastos."""

import hashlib
import io
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pdfplumber

APP_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("FINANCEIRO_DB", APP_DIR / "data" / "financeiro.db"))

CATEGORIES = [
    "Alimentação", "Transporte", "Farmácia", "Saúde", "Lazer", "Moradia",
    "Compras pessoais", "Assinaturas e serviços", "Educação",
    "Tarifas e encargos", "Outros",
]

SUBCATEGORIES = {
    "Alimentação": ["Mercado", "Restaurante"],
    "Transporte": ["Aplicativo", "Combustível"],
}

DEFAULT_PATTERNS = {
    "Alimentação": [
        "RESTAURANTE", "LANCHONETE", "IFOOD", "IFD", "SUPERMERCADO",
        "PADARIA", "PIZZA", "HAMBURGUER", "TOCAPUB", "GLACAI",
    ],
    "Transporte": [
        "UBER", "UBERRIDES", "UBER RIDE", "99APP", "99 RIDE",
        "99 L2L", "DL 99", "PG 99",
    ],
    "Farmácia": [
        "DROGARIA", "FARMACIA", "PAGUE MENOS", "RAIA", "NELFARMA",
        "CAIO FARMA", "REDEPHARMA", "FARMA",
    ],
    "Lazer": ["CINEMA", "SPOTIFY", "NETFLIX", "STEAM", "PLAYSTATION", "SHOW"],
    "Assinaturas e serviços": [
        "GOOGLE", "APPLE.COM", "AMAZON PRIME", "AMAZON AD FREE",
        "YOUTUBE PREMIUM", "YOUTUBE",
    ],
    "Tarifas e encargos": [
        "MULTA POR ATRASO", "JUROS DE MORA", "ENCARGOS FINANC ROTATIVO",
        "JUROS DE ATRASO", "MULTA DE ATRASO", "IOF DE ATRASO",
        "ENCARGOS FINANCEIROS", "IOF", "ANUIDADE", "TARIFA",
    ],
}

IGNORED_DESCRIPTION_TERMS = [
    "PGTO", "PAGAMENTO", "SALDO FATURA", "FATURA ANTERIOR",
    "CREDITO", "ESTORNO", "TOTAL DA FATURA", "TOTAL FATURA",
    "PAGAMENTO MINIMO", "LIMITE DISPONIVEL", "VENCIMENTO",
]

DATE_RE = re.compile(r"(?<!\d)(\d{2}/\d{2}(?:/\d{2,4})?)(?!\d)")
MONEY_RE = re.compile(
    r"(?<![\d/])(?:R\$\s*)?(-?\s*(?:\d{1,3}(?:\.\d{3})+|\d+),\d{2})(?!\d)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def normalize_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(transaction_date, description, amount):
    raw = f"{transaction_date}|{description.strip().upper()}|{amount:.2f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_checksum(file_bytes):
    return hashlib.sha256(file_bytes).hexdigest()


def _merchant_key(description):
    text = normalize_text(description)
    tokens = [t for t in text.split() if len(t) >= 4 and not t.isdigit()]
    return " ".join(tokens[:2]) if tokens else ""


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
def db_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
            UNIQUE(name, parent_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT,
            source TEXT,
            imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
            checksum TEXT UNIQUE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE,
            transaction_date TEXT,
            description TEXT,
            merchant TEXT,
            amount REAL NOT NULL,
            category TEXT NOT NULL DEFAULT 'Outros',
            subcategory TEXT,
            transaction_kind TEXT NOT NULL DEFAULT 'purchase',
            reviewed INTEGER NOT NULL DEFAULT 0,
            source_file TEXT,
            card TEXT DEFAULT '',
            import_id INTEGER REFERENCES imports(id),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS category_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE,
            category TEXT NOT NULL,
            subcategory TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            source TEXT NOT NULL DEFAULT 'manual'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS recurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant TEXT,
            expected_amount REAL,
            frequency TEXT,
            last_seen TEXT,
            confidence REAL,
            UNIQUE(merchant, expected_amount, frequency)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER REFERENCES categories(id),
            month TEXT NOT NULL,
            limit_amount REAL NOT NULL,
            UNIQUE(category_id, month)
        )
    """)
    conn.commit()

    _migrate(conn)
    _seed_categories(conn)
    return conn


def _migrate(conn):
    """Adiciona colunas novas em bancos antigos sem perder dados."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(transactions)")}
    additions = {
        "merchant": "TEXT",
        "subcategory": "TEXT",
        "transaction_kind": "TEXT NOT NULL DEFAULT 'purchase'",
        "reviewed": "INTEGER NOT NULL DEFAULT 0",
        "import_id": "INTEGER",
    }
    for col, decl in additions.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass  # coluna já existe
    conn.commit()


def _seed_categories(conn):
    for name in CATEGORIES:
        conn.execute("INSERT OR IGNORE INTO categories (name, parent_id) VALUES (?, NULL)", (name,))
        parent = conn.execute(
            "SELECT id FROM categories WHERE name=? AND parent_id IS NULL", (name,)
        ).fetchone()
        if parent:
            for child in SUBCATEGORIES.get(name, []):
                conn.execute(
                    "INSERT OR IGNORE INTO categories (name, parent_id) VALUES (?, ?)",
                    (child, parent[0]),
                )
    conn.commit()


def register_import(conn, filename, source, checksum):
    existing = conn.execute("SELECT id FROM imports WHERE checksum=?", (checksum,)).fetchone()
    if existing:
        return existing[0], True
    cursor = conn.execute(
        "INSERT INTO imports (filename, source, checksum) VALUES (?, ?, ?)",
        (filename, source, checksum),
    )
    conn.commit()
    return cursor.lastrowid, False


# ---------------------------------------------------------------------------
# Classificação
# ---------------------------------------------------------------------------
def guess_category(description, conn=None):
    """Retorna (categoria, subcategoria|None)."""
    text = normalize_text(description)

    if conn is not None:
        rules = conn.execute(
            "SELECT keyword, category, subcategory FROM category_rules "
            "WHERE active=1 ORDER BY priority DESC, LENGTH(keyword) DESC"
        ).fetchall()
        for keyword, category, subcategory in rules:
            normalized_keyword = normalize_text(keyword)
            if normalized_keyword and normalized_keyword in text:
                return category, subcategory

    ordered = []
    for category, terms in DEFAULT_PATTERNS.items():
        for term in terms:
            ordered.append((normalize_text(term), category))
    ordered.sort(key=lambda item: len(item[0]), reverse=True)
    for term, category in ordered:
        if term and term in text:
            return category, _guess_subcategory(category, text)
    return "Outros", None


def _guess_subcategory(category, normalized_text):
    if category == "Alimentação":
        if any(t in normalized_text for t in ("SUPERMERCADO", "MERCADO", "PADARIA")):
            return "Mercado"
        if any(t in normalized_text for t in ("RESTAURANTE", "LANCHONETE", "IFOOD", "IFD", "PIZZA")):
            return "Restaurante"
    if category == "Transporte":
        if any(t in normalized_text for t in ("UBER", "99", "RIDE")):
            return "Aplicativo"
        if any(t in normalized_text for t in ("POSTO", "COMBUSTIVEL", "GASOLINA")):
            return "Combustível"
    return None


def learn_from_correction(conn, description, category, subcategory):
    keyword = _merchant_key(description)
    if not keyword:
        return
    conn.execute(
        """INSERT INTO category_rules (keyword, category, subcategory, priority, active, source)
           VALUES (?, ?, ?, 10, 1, 'correction')
           ON CONFLICT(keyword) DO UPDATE SET
             category=excluded.category,
             subcategory=excluded.subcategory,
             priority=excluded.priority,
             active=1,
             source='correction'""",
        (keyword, category, subcategory),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Parsers (BB genérico + Nubank)
# ---------------------------------------------------------------------------
def parse_statement_date(value, reference_date=None):
    reference_date = reference_date or date.today()
    parts = value.split("/")
    day, month = int(parts[0]), int(parts[1])
    if len(parts) == 3:
        year = int(parts[2])
        if year < 100:
            year += 2000
    else:
        year = reference_date.year
        candidate = date(year, month, day)
        if candidate > reference_date + timedelta(days=45):
            year -= 1
    return date(year, month, day)


def parse_brazilian_amount(value):
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    raw = str(value or "").strip().replace("R$", "").replace(" ", "")
    if not raw:
        raise ValueError("Valor vazio")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return float(raw)


def _amount_matches(text):
    matches = []
    for match in MONEY_RE.finditer(text):
        raw = match.group(1).replace(" ", "")
        try:
            value = float(raw.replace(".", "").replace(",", "."))
            matches.append((match, value))
        except ValueError:
            continue
    return matches


def _valid_date_matches(line):
    matches = []
    for match in DATE_RE.finditer(line):
        prefix = line[max(0, match.start() - 14):match.start()]
        if re.search(r"\bPARC(?:ELA)?\s*$", prefix, re.IGNORECASE):
            continue
        if re.search(r"PAGINA\s*$", prefix, re.IGNORECASE):
            continue
        try:
            parse_statement_date(match.group(1))
        except ValueError:
            continue
        matches.append(match)
    return matches


def _clean_description(value):
    text = MONEY_RE.sub(" ", value)
    text = re.sub(
        r"\b(?:JOAO\s+PESSOA|SAO\s+PAULO|BR|PB|PAGINA\s+\d+\s*/\s*\d+)\b",
        " ", text, flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:RESTAURANTES|SAUDE|TRANSPORTE|OUTROS\s+LANCAMENTOS|"
        r"COMPRAS\s+PARCELADAS|PAGAMENTOS\s*/\s*CREDITOS)\b",
        " ", text, flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip(" -|\t")


def parse_generic_invoice_text(text, reference_date=None):
    reference_date = reference_date or date.today()
    rows = []
    diagnostics = {"ignored_credits": 0, "unmatched_dates": 0, "ignored_lines": 0, "parsed_lines": 0}
    pending = None

    def add_record(date_token, description, amount):
        description = _clean_description(description)
        normalized = normalize_text(description)
        if not description or len(normalized) < 2:
            diagnostics["ignored_lines"] += 1
            return
        if any(normalize_text(term) in normalized for term in IGNORED_DESCRIPTION_TERMS):
            diagnostics["ignored_credits"] += 1
            return
        if amount <= 0:
            diagnostics["ignored_credits"] += 1
            return
        try:
            parsed_date = parse_statement_date(date_token, reference_date)
        except ValueError:
            diagnostics["ignored_lines"] += 1
            return
        rows.append({"Data": parsed_date.isoformat(), "Descrição": description,
                     "Valor": round(float(amount), 2), "transaction_kind": "purchase"})
        diagnostics["parsed_lines"] += 1

    def finish_pending():
        nonlocal pending
        if pending is None:
            return
        amounts = _amount_matches(pending["text"])
        if amounts:
            add_record(pending["date"], pending["text"], amounts[0][1])
        else:
            diagnostics["unmatched_dates"] += 1
        pending = None

    for raw_line in str(text or "").replace("\r", "\n").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if re.fullmatch(r"(?:PAGINA\s*)?\d+\s*/\s*\d+", normalize_text(line)):
            continue
        date_matches = _valid_date_matches(line)
        amounts = _amount_matches(line)
        if not date_matches:
            if pending is not None:
                pending["text"] += " " + line
                if _amount_matches(line):
                    finish_pending()
            continue
        if pending is not None:
            finish_pending()
        if len(date_matches) == 1:
            match = date_matches[0]
            description = line[match.end():]
            if amounts:
                add_record(match.group(1), description, amounts[0][1])
            else:
                pending = {"date": match.group(1), "text": description}
            continue
        if len(amounts) >= len(date_matches):
            for index, match in enumerate(date_matches):
                end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(line)
                add_record(match.group(1), line[match.end():end], amounts[index][1])
        else:
            for index, match in enumerate(date_matches):
                end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(line)
                segment = line[match.end():end]
                seg_amounts = _amount_matches(segment)
                if seg_amounts:
                    add_record(match.group(1), segment, seg_amounts[0][1])
                elif index == len(date_matches) - 1:
                    pending = {"date": match.group(1), "text": segment}
                else:
                    diagnostics["unmatched_dates"] += 1
    finish_pending()

    unique_rows, seen = [], set()
    for row in rows:
        key = (row["Data"], normalize_text(row["Descrição"]), row["Valor"])
        if key not in seen:
            seen.add(key)
            unique_rows.append(row)
    return unique_rows, diagnostics


NUBANK_MONTHS = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
                 "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}
_NU_MONTHS_RE = "|".join(NUBANK_MONTHS)

NUBANK_LINE_RE = re.compile(
    rf"^(?P<day>\d{{1,2}})\s+(?P<month>{_NU_MONTHS_RE})\b\s+"
    r"(?:[•·*●.]{2,}\s*(?P<card>\d{4})\s+)?"
    r"(?P<desc>.+?)\s+(?P<neg>[-−–]\s*)?R\$\s*(?P<amount>\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})\s*$",
    re.IGNORECASE,
)
NUBANK_HEADER_RE = re.compile(
    rf"^(?P<day>\d{{1,2}})\s+(?P<month>{_NU_MONTHS_RE})\b\s+"
    r"(?:[•·*●.]{2,}\s*\d{4}\s+)?(?P<desc>[^R\d].*?)\s*$", re.IGNORECASE,
)
NUBANK_AMOUNT_ONLY_RE = re.compile(
    r"^(?P<neg>[-−–]\s*)?R\$\s*(?P<amount>\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})$"
)
NUBANK_ISSUE_RE = re.compile(
    rf"EMISS[ÃA]O\s+E\s+ENVIO\s+(\d{{1,2}})\s+({_NU_MONTHS_RE})\s+(\d{{4}})", re.IGNORECASE)
NUBANK_DUE_RE = re.compile(
    rf"FATURA\s+(\d{{1,2}})\s+({_NU_MONTHS_RE})\s+(\d{{4}})", re.IGNORECASE)
NUBANK_TOTAL_RE = re.compile(r"Total\s+a\s+pagar\s+R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})", re.IGNORECASE)

NUBANK_IGNORED_TERMS = [
    "PAGAMENTO EM", "PAGAMENTO RECEBIDO", "SALDO EM ATRASO", "CREDITO DE ATRASO",
    "ENCERRAMENTO DE DIVIDA", "JUROS DE DIVIDA ENCERRADA", "ESTORNO",
]


def is_nubank_text(text):
    normalized = normalize_text(str(text or "")[:6000])
    if "NUBANK" in normalized or "NU PAGAMENTOS" in normalized:
        return True
    hits = sum(1 for line in str(text or "").splitlines()
               if re.match(rf"^\s*\d{{1,2}}\s+(?:{_NU_MONTHS_RE})\b", line, re.IGNORECASE))
    return hits >= 3


def _nubank_year_resolver(text, reference_date):
    anchor = NUBANK_ISSUE_RE.search(text) or NUBANK_DUE_RE.search(text)
    if anchor:
        anchor_month = NUBANK_MONTHS[anchor.group(2).upper()]
        anchor_year = int(anchor.group(3))
    else:
        anchor_month, anchor_year = reference_date.month, reference_date.year

    def resolve(day, month):
        year = anchor_year - 1 if month > anchor_month else anchor_year
        return date(year, month, day)
    return resolve


def parse_nubank_text(text, reference_date=None):
    reference_date = reference_date or date.today()
    text = str(text or "").replace("\r", "\n")
    resolve = _nubank_year_resolver(text, reference_date)
    diagnostics = {"ignored_credits": 0, "unmatched_dates": 0, "ignored_lines": 0, "parsed_lines": 0}
    rows, pending = [], None

    def emit(day, month, description, negative, amount_text):
        description = description.strip(" -|")
        normalized = normalize_text(description)
        amount = parse_brazilian_amount(amount_text)
        if negative or amount <= 0 or any(t in normalized for t in NUBANK_IGNORED_TERMS):
            diagnostics["ignored_credits"] += 1
            return
        try:
            parsed_date = resolve(int(day), NUBANK_MONTHS[month.upper()])
        except ValueError:
            diagnostics["ignored_lines"] += 1
            return
        rows.append({"Data": parsed_date.isoformat(), "Descrição": description,
                     "Valor": round(amount, 2), "transaction_kind": "purchase"})
        diagnostics["parsed_lines"] += 1

    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        match = NUBANK_LINE_RE.match(line)
        if match:
            if pending:
                diagnostics["unmatched_dates"] += 1
                pending = None
            emit(match.group("day"), match.group("month"), match.group("desc"),
                 bool(match.group("neg")), match.group("amount"))
            continue
        header = NUBANK_HEADER_RE.match(line)
        if header:
            if pending:
                diagnostics["unmatched_dates"] += 1
            pending = {"day": header.group("day"), "month": header.group("month"), "desc": header.group("desc")}
            continue
        lone = NUBANK_AMOUNT_ONLY_RE.match(line)
        if lone and pending:
            emit(pending["day"], pending["month"], pending["desc"], bool(lone.group("neg")), lone.group("amount"))
            pending = None
    if pending:
        diagnostics["unmatched_dates"] += 1
    total = NUBANK_TOTAL_RE.search(text)
    if total:
        diagnostics["invoice_total"] = parse_brazilian_amount(total.group(1))
    diagnostics["bank"] = "Nubank"
    return rows, diagnostics


def parse_invoice_text(text, reference_date=None):
    if is_nubank_text(text):
        rows, diagnostics = parse_nubank_text(text, reference_date)
        if rows:
            return rows, diagnostics
    rows, diagnostics = parse_generic_invoice_text(text, reference_date)
    diagnostics.setdefault("bank", "Banco do Brasil / genérico")
    bb_total = re.search(r"Total\s+da\s+Fatura\s+R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})",
                         str(text or ""), re.IGNORECASE)
    if bb_total:
        diagnostics["invoice_total"] = parse_brazilian_amount(bb_total.group(1))
    return rows, diagnostics


def extract_pdf(file_bytes):
    standard_pages, layout_pages = [], []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            standard_pages.append(page.extract_text(layout=False) or "")
            layout_pages.append(page.extract_text(layout=True) or "")
    standard_text = "\n".join(standard_pages)
    layout_text = "\n".join(layout_pages)
    standard_rows, standard_diag = parse_invoice_text(standard_text)
    layout_rows, layout_diag = parse_invoice_text(layout_text)
    if len(layout_rows) * 2 - layout_diag["unmatched_dates"] > \
       len(standard_rows) * 2 - standard_diag["unmatched_dates"]:
        return layout_rows, layout_diag, layout_text
    return standard_rows, standard_diag, standard_text


# ---------------------------------------------------------------------------
# Carregamento
# ---------------------------------------------------------------------------
def load_transactions(conn):
    return pd.read_sql_query(
        """SELECT id,
                  transaction_date AS Data,
                  description AS Descrição,
                  merchant AS Estabelecimento,
                  amount AS Valor,
                  category AS Categoria,
                  subcategory AS Subcategoria,
                  transaction_kind AS Tipo,
                  reviewed AS Revisado,
                  source_file AS Arquivo,
                  card AS Cartão
           FROM transactions
           ORDER BY transaction_date DESC, id DESC""",
        conn,
    )


def pending_review(conn, confidence_threshold=0.6):
    return pd.read_sql_query(
        """SELECT id, transaction_date AS Data, description AS Descrição,
                  amount AS Valor, category AS Categoria
           FROM transactions
           WHERE reviewed=0 AND (category='Outros' OR category IS NULL)
           ORDER BY transaction_date DESC""",
        conn,
    )


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------
def category_variation(conn, month=None):
    df = pd.read_sql_query(
        """SELECT strftime('%Y-%m', transaction_date) AS mes,
                  category AS Categoria, SUM(amount) AS total
           FROM transactions
           WHERE transaction_kind='purchase'
           GROUP BY mes, category""",
        conn,
    )
    if df.empty:
        return df
    df["mes"] = pd.to_datetime(df["mes"] + "-01")
    pivot = df.pivot_table(index="Categoria", columns="mes", values="total", fill_value=0)
    if len(pivot.columns) < 2:
        pivot["var_abs"] = 0.0
        pivot["var_pct"] = 0.0
        return pivot
    last, prev = pivot.columns[-1], pivot.columns[-2]
    pivot["var_abs"] = pivot[last] - pivot[prev]
    pivot["var_pct"] = pivot.apply(
        lambda r: (r[last] / r[prev] - 1) * 100 if r[prev] else 0.0, axis=1
    )
    return pivot.sort_values("var_abs", key=abs, ascending=False)


def detect_recurrences(conn, min_occurrences=3, amount_tolerance=0.10, day_tolerance=5):
    df = pd.read_sql_query(
        """SELECT description, transaction_date, amount FROM transactions
           WHERE transaction_kind='purchase'""",
        conn,
    )
    if df.empty:
        return pd.DataFrame(columns=["merchant", "expected_amount", "frequency",
                                     "last_seen", "confidence", "occurrences"])
    df["merchant_key"] = df["description"].apply(_merchant_key)
    df["transaction_date"] = pd.to_datetime(df["transaction_date"])
    results = []
    for merchant, group in df.groupby("merchant_key"):
        if not merchant or len(group) < min_occurrences:
            continue
        group = group.sort_values("transaction_date")
        median_amount = group["amount"].median()
        within = group[(group["amount"] >= median_amount * (1 - amount_tolerance)) &
                       (group["amount"] <= median_amount * (1 + amount_tolerance))]
        if len(within) < min_occurrences:
            continue
        days = within["transaction_date"].dt.day.values
        if days.max() - days.min() > day_tolerance * 2:
            continue
        intervals = within["transaction_date"].diff().dt.days.dropna()
        median_interval = intervals.median() if not intervals.empty else 30
        freq = ("mensal" if 25 <= median_interval <= 35 else
                "semanal" if 5 <= median_interval <= 9 else
                "anual" if 350 <= median_interval <= 380 else f"~{int(median_interval)} dias")
        confidence = min(1.0, len(within) / (min_occurrences + 2))
        results.append({
            "merchant": merchant,
            "expected_amount": round(float(median_amount), 2),
            "frequency": freq,
            "last_seen": within["transaction_date"].max().date().isoformat(),
            "confidence": round(confidence, 2),
            "occurrences": len(within),
        })
    result = pd.DataFrame(results)
    if not result.empty:
        for _, row in result.iterrows():
            conn.execute(
                """INSERT INTO recurrences (merchant, expected_amount, frequency, last_seen, confidence)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(merchant, expected_amount, frequency) DO UPDATE SET
                     last_seen=excluded.last_seen, confidence=excluded.confidence""",
                (row["merchant"], row["expected_amount"], row["frequency"], row["last_seen"], row["confidence"]),
            )
        conn.commit()
    return result


def outliers(conn, factor=2.0):
    df = pd.read_sql_query(
        """SELECT id, transaction_date, description, amount, category FROM transactions
           WHERE transaction_kind='purchase'""",
        conn,
    )
    if df.empty:
        return df
        
    stats = df.groupby("category")["amount"].agg(["mean", "count"]).reset_index()
    stats.columns = ["category", "mean", "count"]
    
    merged = df.merge(stats, on="category", how="left")
    
    # Só considera categorias com pelo menos 3 lançamentos
    merged = merged[merged["count"] >= 3]
    
    # Regra simples: o valor precisa ser maior que a média + 2 vezes a média (3x a média)
    merged["threshold"] = merged["mean"] * (1 + factor)
    
    return merged[merged["amount"] > merged["threshold"]].sort_values("amount", ascending=False)


def monthly_summary(conn, month=None):
    month = month or date.today().strftime("%Y-%m")
    df = pd.read_sql_query(
        """SELECT * FROM transactions
           WHERE transaction_kind='purchase' AND strftime('%Y-%m', transaction_date)=?""",
        conn, params=(month,),
    )
    if df.empty:
        return {"month": month, "total": 0, "top_categories": [], "top_transactions": [], "recurrences": 0}
    by_cat = df.groupby("category")["amount"].sum().sort_values(ascending=False).head(5)
    top_tx = df.nlargest(5, "amount")[["transaction_date", "description", "amount", "category"]]
    rec = conn.execute("SELECT COUNT(*) FROM recurrences").fetchone()[0]
    return {
        "month": month,
        "total": round(df["amount"].sum(), 2),
        "top_categories": by_cat.to_dict(),
        "top_transactions": top_tx.to_dict("records"),
        "recurrences": rec,
    }


# ---------------------------------------------------------------------------
# Previsão
# ---------------------------------------------------------------------------
def forecast_month(conn, reference_day=None):
    today = date.today()
    reference_day = reference_day or today.day
    next_month = today.replace(day=28) + timedelta(days=4)
    days_in_month = (next_month.replace(day=1) - timedelta(days=1)).day
    month_start = today.replace(day=1).isoformat()

    current = pd.read_sql_query(
        """SELECT amount FROM transactions
           WHERE transaction_kind='purchase' AND transaction_date >= ?""",
        conn, params=(month_start,),
    )
    spent = current["amount"].sum() if not current.empty else 0.0

    history = pd.read_sql_query(
        """SELECT strftime('%Y-%m', transaction_date) AS mes, SUM(amount) AS total
           FROM transactions WHERE transaction_kind='purchase'
           GROUP BY mes ORDER BY mes DESC LIMIT 6""",
        conn,
    )
    history_months = len(history)
    pace_projection = spent / reference_day * days_in_month if reference_day else 0.0

    recurrences = pd.read_sql_query("SELECT * FROM recurrences", conn)
    pending_recurrences = 0.0
    for _, r in recurrences.iterrows():
        seen = conn.execute(
            """SELECT 1 FROM transactions
               WHERE transaction_kind='purchase'
                 AND description LIKE ? AND strftime('%Y-%m', transaction_date)=?""",
            (f"%{r['merchant']}%", today.strftime("%Y-%m")),
        ).fetchone()
        if not seen:
            pending_recurrences += float(r["expected_amount"])

    installments = pd.read_sql_query(
        """SELECT amount FROM transactions
           WHERE description LIKE '%PARC%' AND transaction_kind='purchase'
             AND strftime('%Y-%m', transaction_date)=?""",
        conn, params=(today.strftime("%Y-%m"),),
    )
    installment_estimate = installments["amount"].sum() if not installments.empty else 0.0

    projected = pace_projection + pending_recurrences
    if history_months >= 3:
        std = history["total"].std() if history_months > 1 else 0
        low = max(spent, projected - std * 0.5)
        high = projected + std * 0.5
        assumptions = f"Ritmo diário + recorrências pendentes; histórico de {history_months} meses."
    else:
        low = projected * 0.8
        high = projected * 1.2
        assumptions = "Pouco histórico: faixa ampla (±20%). Use como estimativa grosseira."

    return {
        "projected": round(projected, 2),
        "low": round(low, 2),
        "high": round(high, 2),
        "spent": round(spent, 2),
        "days_elapsed": reference_day,
        "days_in_month": days_in_month,
        "pending_recurrences": round(pending_recurrences, 2),
        "installment_estimate": round(installment_estimate, 2),
        "history_months": history_months,
        "assumptions": assumptions,
    }


def simulate_purchase(conn, amount, category="Outros"):
    forecast = forecast_month(conn)
    after = forecast["projected"] + float(amount)
    budget = conn.execute(
        "SELECT limit_amount FROM budgets WHERE month=? AND category_id IS NULL",
        (date.today().strftime("%Y-%m"),),
    ).fetchone()
    budget_limit = budget[0] if budget else None
    return {
        "before": forecast["projected"],
        "after": round(after, 2),
        "delta": round(float(amount), 2),
        "budget_limit": budget_limit,
        "over_budget": budget_limit is not None and after > budget_limit,
        "forecast_low": forecast["low"],
        "forecast_high": forecast["high"],
    }


# ---------------------------------------------------------------------------
# Tabela → linhas normalizadas
# ---------------------------------------------------------------------------
def normalize_table_rows(dataframe, date_column, description_column, amount_column):
    rows = []
    diagnostics = {"ignored_credits": 0, "unmatched_dates": 0, "ignored_lines": 0, "parsed_lines": 0}
    for _, row in dataframe.iterrows():
        description = str(row.get(description_column, "") or "").strip()
        if not description or description.lower() == "nan":
            diagnostics["ignored_lines"] += 1
            continue
        normalized = normalize_text(description)
        if any(normalize_text(term) in normalized for term in IGNORED_DESCRIPTION_TERMS):
            diagnostics["ignored_credits"] += 1
            continue
        try:
            raw_date = row.get(date_column)
            if pd.isna(raw_date):
                raise ValueError("Data vazia")
            if isinstance(raw_date, (datetime, date, pd.Timestamp)):
                parsed_date = pd.to_datetime(raw_date).date()
            else:
                parsed_date = pd.to_datetime(str(raw_date), dayfirst=True, errors="raise").date()
            amount = parse_brazilian_amount(row.get(amount_column))
            if amount <= 0:
                diagnostics["ignored_credits"] += 1
                continue
            rows.append({"Data": parsed_date.isoformat(), "Descrição": description,
                         "Valor": round(amount, 2), "transaction_kind": "purchase"})
            diagnostics["parsed_lines"] += 1
        except Exception:
            diagnostics["unmatched_dates"] += 1
    return rows, diagnostics


def save_transactions(conn, rows, source_name, card, import_id=None):
    saved = duplicates = invalid = 0
    for row in rows:
        try:
            parsed_date = pd.to_datetime(row["Data"]).date().isoformat()
            description = str(row["Descrição"]).strip()
            amount = abs(float(row["Valor"]))
            if not description or amount <= 0:
                invalid += 1
                continue
            category, subcategory = guess_category(description, conn)
            if row.get("Categoria") and row["Categoria"] in CATEGORIES:
                category = row["Categoria"]
            if row.get("Subcategoria"):
                subcategory = row["Subcategoria"]
            fp = fingerprint(parsed_date, description, amount)
            cursor = conn.execute(
                """INSERT OR IGNORE INTO transactions
                   (fingerprint, transaction_date, description, merchant, amount,
                    category, subcategory, transaction_kind, source_file, card, import_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fp, parsed_date, description, _merchant_key(description), amount,
                 category, subcategory, row.get("transaction_kind", "purchase"),
                 source_name, card, import_id),
            )
            if cursor.rowcount:
                saved += 1
            else:
                duplicates += 1
        except Exception:
            invalid += 1
    conn.commit()
    return saved, duplicates, invalid