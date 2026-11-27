"""Funções de dados, classificação, importação e análises do Radar de Gastos."""

import hashlib
import io
import math
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
        "99 L2L", "DL 99", "PG 99", "POSTO", "COMBUSTIVEL", "GASOLINA",
    ],
    "Farmácia": [
        "DROGARIA", "FARMACIA", "PAGUE MENOS", "RAIA", "NELFARMA",
        "CAIO FARMA", "REDEPHARMA", "FARMA", "DROGARAIA",
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

SCHEMA_VERSION = 2
# termos curtos só valem como palavra inteira ("RAIA" não pode casar com "PRAIA")
WHOLE_WORD_TERMS = {"POSTO"}
GATEWAY_PREFIXES = {"DL", "PG", "MP", "IFD", "PAG", "EC"}
INSTALLMENT_RE = re.compile(r"\bPARC(?:ELA)?\.?\s*(\d{1,2})\s*(?:/|DE)\s*(\d{1,2})", re.IGNORECASE)

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


def fingerprint(transaction_date, description, amount, occurrence=0):
    """Identidade de um lançamento. `occurrence` distingue compras idênticas no mesmo dia.

    A ocorrência 0 usa a mesma fórmula das versões anteriores, para que
    reimportações continuem sendo reconhecidas em bancos antigos.
    """
    raw = f"{transaction_date}|{description.strip().upper()}|{amount:.2f}"
    if occurrence:
        raw += f"|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_checksum(file_bytes):
    return hashlib.sha256(file_bytes).hexdigest()


def _merchant_key(description):
    """Trecho contínuo do nome do estabelecimento, usado em regras e recorrências.

    Precisa ser um pedaço literal da descrição normalizada; caso contrário a
    regra aprendida não reconhece nem o lançamento que a originou.
    """
    text = INSTALLMENT_RE.sub(" ", str(description or ""))
    tokens = normalize_text(text).split()
    while tokens and (tokens[0] in GATEWAY_PREFIXES
                      or (tokens[0].isdigit() and len(tokens[0]) >= 4)):
        tokens.pop(0)
    selected, strong = [], 0
    for token in tokens[:5]:
        selected.append(token)
        if len(token) >= 4 and not token.isdigit():
            strong += 1
        if strong == 2:
            break
    key = " ".join(selected)
    return key if len(key) >= 3 else ""


def _term_matches(term, text):
    if len(term) <= 4 or term in WHOLE_WORD_TERMS:
        return f" {term} " in f" {text} "
    return term in text


def valid_subcategory(category, subcategory):
    """Devolve a subcategoria apenas se ela pertencer à categoria."""
    return subcategory if subcategory in SUBCATEGORIES.get(category, []) else None


def _clean_str(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Banco de dados
# ---------------------------------------------------------------------------
def db_connection(path=None):
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists() and path.stat().st_size > 0
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if existed and version < SCHEMA_VERSION:
        _backup(conn, path)
    _create_tables(conn)
    if version < SCHEMA_VERSION:
        _migrate(conn)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    _seed_categories(conn)
    return conn


def readonly_connection(path=None):
    """Cópia em memória do banco: dá para analisar sem alterar o arquivo."""
    path = Path(path or DB_PATH)
    memory = sqlite3.connect(":memory:")
    if path.exists():
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            source.backup(memory)
        finally:
            source.close()
    _create_tables(memory)
    _migrate(memory)
    _seed_categories(memory)
    return memory


def _backup(conn, path):
    """Cópia de segurança feita uma única vez, antes de atualizar o esquema."""
    target = path.with_name(f"{path.stem}.backup-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
    copy = sqlite3.connect(target)
    try:
        conn.backup(copy)
    finally:
        copy.close()


def _create_tables(conn):
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


def _migrate(conn):
    """Adiciona colunas novas em bancos antigos sem perder dados."""
    tables = {
        "transactions": {
            "merchant": "TEXT",
            "subcategory": "TEXT",
            "transaction_kind": "TEXT NOT NULL DEFAULT 'purchase'",
            "reviewed": "INTEGER NOT NULL DEFAULT 0",
            "source_file": "TEXT",
            "card": "TEXT DEFAULT ''",
            "import_id": "INTEGER",
        },
        "category_rules": {
            "subcategory": "TEXT",
            "priority": "INTEGER NOT NULL DEFAULT 0",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "source": "TEXT NOT NULL DEFAULT 'manual'",
        },
    }
    for table, additions in tables.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in additions.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    for tx_id, description, category, subcategory in conn.execute(
            "SELECT id, description, category, subcategory FROM transactions").fetchall():
        if not subcategory:
            subcategory = _guess_subcategory(category, normalize_text(description))
        conn.execute("UPDATE transactions SET merchant=?, subcategory=? WHERE id=?",
                     (_merchant_key(description), subcategory, tx_id))

    _dedupe_categories(conn)
    conn.commit()


def _dedupe_categories(conn):
    """Remove categorias repetidas (UNIQUE não vale para parent_id nulo no SQLite)."""
    keep = {}
    rows = conn.execute("SELECT id, name FROM categories WHERE parent_id IS NULL ORDER BY id").fetchall()
    for cat_id, name in rows:
        if name not in keep:
            keep[name] = cat_id
            continue
        conn.execute("UPDATE OR IGNORE budgets SET category_id=? WHERE category_id=?", (keep[name], cat_id))
        conn.execute("UPDATE OR IGNORE categories SET parent_id=? WHERE parent_id=?", (keep[name], cat_id))
        conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))


def _seed_categories(conn):
    def ensure(name, parent_id):
        row = conn.execute("SELECT id FROM categories WHERE name=? AND parent_id IS ?",
                           (name, parent_id)).fetchone()
        if row:
            return row[0]
        return conn.execute("INSERT INTO categories (name, parent_id) VALUES (?, ?)",
                            (name, parent_id)).lastrowid

    for name in CATEGORIES:
        parent_id = ensure(name, None)
        for child in SUBCATEGORIES.get(name, []):
            ensure(child, parent_id)
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
            if normalized_keyword and _term_matches(normalized_keyword, text):
                return category, subcategory

    ordered = []
    for category, terms in DEFAULT_PATTERNS.items():
        for term in terms:
            ordered.append((normalize_text(term), category))
    ordered.sort(key=lambda item: len(item[0]), reverse=True)
    for term, category in ordered:
        if term and _term_matches(term, text):
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
    """Cria/atualiza a regra e aplica aos pendentes parecidos. Retorna quantos mudaram."""
    keyword = _merchant_key(description)
    if not keyword:
        return 0
    subcategory = valid_subcategory(category, subcategory)
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
    changed = 0
    for tx_id, other in conn.execute(
            "SELECT id, description FROM transactions WHERE reviewed=0 AND category='Outros'").fetchall():
        if _term_matches(keyword, normalize_text(other)):
            update_transaction(conn, tx_id, category, subcategory, reviewed=False, commit=False)
            changed += 1
    conn.commit()
    return changed


def reclassify_pending(conn):
    """Reaplica regras e padrões aos lançamentos em "Outros" não revisados."""
    changed = 0
    for tx_id, description in conn.execute(
            "SELECT id, description FROM transactions WHERE reviewed=0 AND category='Outros'").fetchall():
        category, subcategory = guess_category(description, conn)
        if category != "Outros":
            update_transaction(conn, tx_id, category, subcategory, reviewed=False, commit=False)
            changed += 1
    conn.commit()
    return changed


def update_transaction(conn, tx_id, category, subcategory=None, reviewed=True, commit=True):
    conn.execute(
        "UPDATE transactions SET category=?, subcategory=?, reviewed=? WHERE id=?",
        (category, valid_subcategory(category, subcategory), int(reviewed), int(tx_id)),
    )
    if commit:
        conn.commit()


def delete_transactions(conn, ids):
    conn.executemany("DELETE FROM transactions WHERE id=?", [(int(i),) for i in ids])
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
        return date(year, month, day)
    limit = reference_date + timedelta(days=45)
    # sem ano: usa o mais recente possível (cobre virada de ano e 29/02)
    for year in range(reference_date.year, reference_date.year - 5, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= limit:
            return candidate
    raise ValueError(f"Data inválida: {value}")


def parse_brazilian_amount(value):
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raw = str(value or "").strip().replace("R$", "").replace("−", "-")
        raw = re.sub(r"\s", "", raw)
        if not raw:
            raise ValueError("Valor vazio")
        if "," in raw:
            raw = raw.replace(".", "").replace(",", ".")
        number = float(raw)
    if not math.isfinite(number):
        raise ValueError("Valor inválido")
    return number


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

    # compras idênticas no mesmo dia são mantidas; a tela de conferência avisa
    keys = [(r["Data"], normalize_text(r["Descrição"]), r["Valor"]) for r in rows]
    diagnostics["repeated"] = len(keys) - len(set(keys))
    return rows, diagnostics


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


def pending_review(conn):
    return pd.read_sql_query(
        """SELECT id, transaction_date AS Data, description AS Descrição,
                  amount AS Valor, category AS Categoria, subcategory AS Subcategoria
           FROM transactions
           WHERE reviewed=0 AND (category='Outros' OR category IS NULL)
           ORDER BY transaction_date DESC, id DESC""",
        conn,
    )


# ---------------------------------------------------------------------------
# Diagnóstico
# ---------------------------------------------------------------------------
def _purchases(conn):
    df = pd.read_sql_query(
        """SELECT id, transaction_date, description, amount, category FROM transactions
           WHERE transaction_kind='purchase'""",
        conn,
    )
    df["transaction_date"] = pd.to_datetime(df["transaction_date"])
    df["month"] = df["transaction_date"].dt.strftime("%Y-%m")
    df["merchant_key"] = df["description"].map(_merchant_key)
    return df


def _is_installment(df):
    return df["description"].map(lambda d: bool(INSTALLMENT_RE.search(d))).astype(bool)


def shift_month(month, delta):
    year, number = map(int, month.split("-"))
    total = year * 12 + number - 1 + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def available_months(conn):
    return [row[0] for row in conn.execute(
        """SELECT DISTINCT strftime('%Y-%m', transaction_date) FROM transactions
           WHERE transaction_date IS NOT NULL ORDER BY 1 DESC""")]


def category_variation(conn, month=None, up_to_day=None):
    """Compara um mês com o mês de calendário anterior.

    `up_to_day` limita os dois meses ao mesmo dia (útil para o mês em andamento).
    Sem dados no mês anterior, devolve tabela vazia. Categoria nova fica sem %.
    """
    df = _purchases(conn)
    if df.empty:
        return pd.DataFrame()
    month = month or df["month"].max()
    previous = shift_month(month, -1)
    if up_to_day:
        df = df[df["transaction_date"].dt.day <= up_to_day]
    df = df[df["month"].isin([month, previous])]
    if set(df["month"]) != {month, previous}:
        return pd.DataFrame()
    pivot = df.pivot_table(index="category", columns="month", values="amount",
                           aggfunc="sum", fill_value=0)[[previous, month]]
    pivot.index.name = "Categoria"
    pivot.columns = [pd.Timestamp(previous + "-01"), pd.Timestamp(month + "-01")]
    prev_values, last_values = pivot.iloc[:, 0].copy(), pivot.iloc[:, 1].copy()
    pivot["var_abs"] = last_values - prev_values
    pivot["var_pct"] = (last_values / prev_values.where(prev_values > 0) - 1) * 100
    return pivot.sort_values("var_abs", key=abs, ascending=False)


RECURRENCE_COLUMNS = ["merchant", "expected_amount", "frequency",
                      "last_seen", "confidence", "occurrences"]
FREQUENCIES = {"semanal": (5, 9), "mensal": (25, 35), "anual": (350, 380)}


def detect_recurrences(conn, min_occurrences=3, amount_tolerance=0.10, persist=True):
    """Cobranças de valor parecido em intervalos regulares.

    Compras frequentes e irregulares (corridas, delivery) não contam, nem
    parcelas ou encargos.
    """
    df = _purchases(conn)
    df = df[(df["category"] != "Tarifas e encargos")
            & ~_is_installment(df)]
    results = []
    for merchant, group in df.groupby("merchant_key"):
        if not merchant or len(group) < min_occurrences:
            continue
        median_amount = group["amount"].median()
        within = group[(group["amount"] - median_amount).abs()
                       <= median_amount * amount_tolerance].sort_values("transaction_date")
        if len(within) < min_occurrences:
            continue
        intervals = within["transaction_date"].diff().dt.days.dropna()
        frequency = next(
            (name for name, (low, high) in FREQUENCIES.items()
             if intervals.between(low, high).mean() >= 0.75), None)
        if not frequency:
            continue
        results.append({
            "merchant": merchant,
            "expected_amount": round(float(median_amount), 2),
            "frequency": frequency,
            "last_seen": within["transaction_date"].max().date().isoformat(),
            "confidence": round(min(1.0, len(within) / (min_occurrences + 2)), 2),
            "occurrences": len(within),
        })
    result = pd.DataFrame(results, columns=RECURRENCE_COLUMNS)
    if persist:
        conn.execute("DELETE FROM recurrences")
        conn.executemany(
            """INSERT INTO recurrences (merchant, expected_amount, frequency, last_seen, confidence)
               VALUES (?, ?, ?, ?, ?)""",
            [(r["merchant"], r["expected_amount"], r["frequency"], r["last_seen"], r["confidence"])
             for r in results],
        )
        conn.commit()
    return result


def outliers(conn, factor=2.0, min_sample=4):
    """Valores muito acima do típico da categoria (mediana, resistente a extremos)."""
    df = _purchases(conn)
    columns = ["id", "transaction_date", "description", "amount", "category", "typical", "threshold"]
    found = []
    for _, group in df.groupby("category"):
        if len(group) < min_sample:
            continue
        median = group["amount"].median()
        mad = (group["amount"] - median).abs().median()
        threshold = max(median * (1 + factor), median + 3.5 * 1.4826 * mad)
        high = group[group["amount"] > threshold]
        if not high.empty:
            found.append(high.assign(typical=median, threshold=threshold))
    if not found:
        return pd.DataFrame(columns=columns)
    result = pd.concat(found)[columns].sort_values("amount", ascending=False)
    result["transaction_date"] = result["transaction_date"].dt.strftime("%Y-%m-%d")
    return result


def installment_commitments(conn):
    """Parcelas que ainda vão cair, estimadas a partir de "PARC x/y".

    Premissa: quando há um único registro da compra, a data é a da compra
    original (padrão BB), então a parcela x cai x-1 meses depois.
    """
    df = _purchases(conn)
    series = {}
    for row in df.itertuples():
        match = INSTALLMENT_RE.search(row.description)
        if not match:
            continue
        number, total = int(match.group(1)), int(match.group(2))
        if not 1 <= number <= total:
            continue
        key = (row.merchant_key, total, round(row.amount, 2))
        series.setdefault(key, []).append((number, row.month, row.description))
    results = []
    for (merchant, total, amount), items in series.items():
        number, month, description = max(items)
        billed = month if len({m for _, m, _ in items}) > 1 else shift_month(month, number - 1)
        for future in range(number + 1, total + 1):
            results.append({
                "month": shift_month(billed, future - number),
                "description": INSTALLMENT_RE.sub("", description).strip(),
                "installment": f"{future}/{total}",
                "amount": amount,
            })
    return pd.DataFrame(results, columns=["month", "description", "installment", "amount"])


def monthly_summary(conn, month=None):
    month = month or date.today().strftime("%Y-%m")
    df = _purchases(conn)
    df = df[df["month"] == month]
    recurrences = len(detect_recurrences(conn, persist=False))
    if df.empty:
        return {"month": month, "total": 0, "count": 0, "top_categories": {},
                "top_transactions": [], "recurrences": recurrences}
    by_cat = df.groupby("category")["amount"].sum().sort_values(ascending=False).head(5)
    top_tx = df.nlargest(5, "amount")[["transaction_date", "description", "amount", "category"]]
    return {
        "month": month,
        "total": round(float(df["amount"].sum()), 2),
        "count": len(df),
        "top_categories": by_cat.to_dict(),
        "top_transactions": top_tx.to_dict("records"),
        "recurrences": recurrences,
    }


# ---------------------------------------------------------------------------
# Previsão e orçamento
# ---------------------------------------------------------------------------
def forecast_month(conn, reference_day=None, today=None):
    """Fechamento do mês = já gasto + gasto variável restante + fixos pendentes."""
    today = today or date.today()
    month = today.strftime("%Y-%m")
    days_in_month = (pd.Timestamp(today) + pd.offsets.MonthEnd(0)).day
    day = min(max(reference_day or today.day, 1), days_in_month)

    df = _purchases(conn)
    recurrences = detect_recurrences(conn, persist=False)
    fixed = (_is_installment(df)
             | df["merchant_key"].isin(recurrences["merchant"])
             | (df["category"] == "Tarifas e encargos"))
    df["variable"] = df["amount"].where(~fixed, 0.0)

    current = df[df["month"] == month]
    spent = float(current["amount"].sum())
    has_current = not current.empty

    # meses anteriores; os quase vazios (ex.: só uma parcela antiga) ficam de fora
    history = df[df["month"] < month].groupby("month")[["amount", "variable"]].sum()
    if not history.empty:
        history = history[history["amount"] >= history["amount"].max() * 0.10].tail(6)
    history_months = len(history)

    # recorrências ativas que ainda não apareceram neste mês
    pending_recurrences = 0.0
    seen = set(current["merchant_key"])
    max_age = {"semanal": 21, "mensal": 45, "anual": 400}
    for r in recurrences.itertuples():
        last = date.fromisoformat(r.last_seen)
        if r.merchant in seen or (today - last).days > max_age[r.frequency]:
            continue
        if r.frequency == "anual" and (today - last).days < 335:
            continue
        times = 1 + (days_in_month - day) // 7 if r.frequency == "semanal" else 1
        pending_recurrences += r.expected_amount * times

    commitments = installment_commitments(conn)
    pending_installments = float(commitments.loc[commitments["month"] == month, "amount"].sum())

    remaining_share = (days_in_month - day) / days_in_month
    pace = float(current["variable"].sum()) / day * (days_in_month - day)
    if not has_current:
        remaining = float(history["variable"].median()) if history_months else 0.0
        method = "Nenhum lançamento neste mês: estimativa baseada só no histórico."
    elif history_months:
        weight = day / days_in_month
        typical = float(history["variable"].median()) * remaining_share
        remaining = weight * pace + (1 - weight) * typical
        method = "Combina o ritmo deste mês com a mediana dos meses anteriores."
    else:
        remaining = pace
        method = "Sem meses anteriores: apenas o ritmo diário deste mês."

    projected = spent + remaining + pending_recurrences + pending_installments
    uncertain = projected - spent
    if history_months >= 3:
        share = remaining_share if has_current else 1.0
        spread = max(float(history["variable"].std()) * share, uncertain * 0.10)
        low, high = max(spent, projected - spread), projected + spread
    else:
        low, high = max(spent, projected - uncertain * 0.3), projected + uncertain * 0.3
        method += " Pouco histórico: faixa de ±30% sobre o que falta."

    last_date = df["transaction_date"].max()
    return {
        "projected": round(projected, 2),
        "low": round(low, 2),
        "high": round(high, 2),
        "spent": round(spent, 2),
        "remaining_variable": round(remaining, 2),
        "days_elapsed": day,
        "days_in_month": days_in_month,
        "pending_recurrences": round(pending_recurrences, 2),
        "installment_estimate": round(pending_installments, 2),
        "history_months": history_months,
        "has_current": has_current,
        "last_date": None if pd.isna(last_date) else last_date.date().isoformat(),
        "assumptions": method,
    }


def set_budget(conn, month, limit_amount, category=None):
    """Grava o limite (0 remove). `category=None` é o orçamento total."""
    category_id = None
    if category:
        category_id = conn.execute(
            "SELECT id FROM categories WHERE name=? AND parent_id IS NULL", (category,)
        ).fetchone()[0]
    conn.execute("DELETE FROM budgets WHERE month=? AND category_id IS ?", (month, category_id))
    conn.execute("INSERT INTO budgets (category_id, month, limit_amount) VALUES (?, ?, ?)",
                 (category_id, month, float(limit_amount)))
    conn.commit()


def get_budgets(conn, month):
    """{categoria ou None: limite}. Um limite vale até ser alterado em mês posterior."""
    result = {}
    for name, limit in conn.execute(
            """SELECT c.name, b.limit_amount FROM budgets b
               LEFT JOIN categories c ON c.id = b.category_id
               WHERE b.month <= ? ORDER BY b.month, b.id""", (month,)):
        result[name] = limit
    return {name: limit for name, limit in result.items() if limit > 0}


def simulate_purchase(conn, amount, category="Outros", installments=1):
    """Efeito no mês atual: só a primeira parcela pesa agora."""
    forecast = forecast_month(conn)
    impact = round(float(amount) / max(int(installments), 1), 2)
    after = round(forecast["projected"] + impact, 2)
    month = date.today().strftime("%Y-%m")
    budgets = get_budgets(conn, month)
    budget_limit = budgets.get(None)
    category_spent = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) FROM transactions
           WHERE transaction_kind='purchase' AND category=?
             AND strftime('%Y-%m', transaction_date)=?""", (category, month)).fetchone()[0]
    category_limit = budgets.get(category)
    category_after = round(category_spent + impact, 2)
    return {
        "before": forecast["projected"],
        "after": after,
        "delta": impact,
        "budget_limit": budget_limit,
        "over_budget": budget_limit is not None and after > budget_limit,
        "category_limit": category_limit,
        "category_after": category_after,
        "over_category": category_limit is not None and category_after > category_limit,
        "forecast_low": round(forecast["low"] + impact, 2),
        "forecast_high": round(forecast["high"] + impact, 2),
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
                text_date = str(raw_date).strip()
                # datas ISO (2026-10-03) não podem ser lidas como dia-primeiro
                iso = re.match(r"\d{4}-\d{2}-\d{2}", text_date)
                parsed_date = pd.to_datetime(
                    iso.group(0) if iso else text_date, dayfirst=not iso, errors="raise").date()
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
    seen, corrections = {}, []
    # palpites calculados antes de aprender qualquer regra nova deste lote
    prepared = [(row, guess_category(_clean_str(row.get("Descrição")), conn)) for row in rows]
    for row, (guessed, guessed_sub) in prepared:
        try:
            parsed_date = pd.to_datetime(row["Data"]).date().isoformat()
            description = _clean_str(row["Descrição"])
            amount = round(float(row["Valor"]), 2)
            if not description or not math.isfinite(amount) or amount <= 0:
                invalid += 1
                continue
            chosen = _clean_str(row.get("Categoria"))
            category = chosen if chosen in CATEGORIES else guessed
            subcategory = valid_subcategory(category, _clean_str(row.get("Subcategoria")))
            corrected = category != guessed or (
                subcategory is not None and subcategory != guessed_sub)
            if subcategory is None and category == guessed:
                subcategory = guessed_sub
            key = (parsed_date, description.upper(), amount)
            occurrence = seen.get(key, 0)
            seen[key] = occurrence + 1
            cursor = conn.execute(
                """INSERT OR IGNORE INTO transactions
                   (fingerprint, transaction_date, description, merchant, amount,
                    category, subcategory, transaction_kind, reviewed, source_file, card, import_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fingerprint(parsed_date, description, amount, occurrence), parsed_date,
                 description, _merchant_key(description), amount, category, subcategory,
                 row.get("transaction_kind") if isinstance(row.get("transaction_kind"), str)
                 else "purchase", int(corrected), source_name, card, import_id),
            )
            if cursor.rowcount:
                saved += 1
                if corrected and category != "Outros":
                    corrections.append((description, category, subcategory))
            else:
                duplicates += 1
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            invalid += 1
    conn.commit()
    for correction in corrections:
        learn_from_correction(conn, *correction)
    return saved, duplicates, invalid


def add_manual_transaction(conn, day, description, amount, category=None, subcategory=None):
    """Lançamento digitado (Pix, dinheiro). Nunca é descartado como duplicado."""
    description = _clean_str(description)
    amount = round(float(amount), 2)
    if not description or not math.isfinite(amount) or amount <= 0:
        return False
    guessed, guessed_sub = guess_category(description, conn)
    category = category if category in CATEGORIES else guessed
    subcategory = valid_subcategory(category, subcategory) or (
        guessed_sub if category == guessed else None)
    iso = pd.to_datetime(day).date().isoformat()
    occurrence = 0
    while conn.execute("SELECT 1 FROM transactions WHERE fingerprint=?",
                       (fingerprint(iso, description, amount, occurrence),)).fetchone():
        occurrence += 1
    conn.execute(
        """INSERT INTO transactions (fingerprint, transaction_date, description, merchant,
               amount, category, subcategory, reviewed, source_file, card)
           VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'Manual', '')""",
        (fingerprint(iso, description, amount, occurrence), iso, description,
         _merchant_key(description), amount, category, subcategory),
    )
    conn.commit()
    return True
