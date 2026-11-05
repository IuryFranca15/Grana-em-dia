"""Funções de dados, classificação e importação de faturas do Controle Financeiro."""

import hashlib
import io
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pdfplumber

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "data" / "financeiro.db"

CATEGORIES = [
    "Alimentação",
    "Transporte",
    "Farmácia",
    "Saúde",
    "Lazer",
    "Moradia",
    "Compras pessoais",
    "Assinaturas e serviços",
    "Educação",
    "Tarifas e encargos",
    "Outros",
]

# Palavras-chave observadas na fatura enviada, além das regras que já existiam.
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
    "Lazer": [
        "CINEMA", "SPOTIFY", "NETFLIX", "STEAM", "PLAYSTATION", "SHOW",
    ],
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

# Itens que não representam uma nova compra e não devem inflar os gastos.
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


def normalize_text(value):
    """Remove acentos e padroniza espaços/pontuação para facilitar comparações."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.upper()
    text = re.sub(r"[^A-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def db_connection():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE,
            transaction_date TEXT,
            description TEXT,
            amount REAL NOT NULL,
            category TEXT NOT NULL DEFAULT 'Outros',
            source_file TEXT,
            card TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS category_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE,
            category TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def fingerprint(transaction_date, description, amount):
    raw = f"{transaction_date}|{description.strip().upper()}|{amount:.2f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def guess_category(description, conn=None):
    text = normalize_text(description)

    # Regras cadastradas pelo usuário têm prioridade. As mais específicas vêm primeiro.
    if conn is not None:
        rules = conn.execute(
            "SELECT keyword, category FROM category_rules"
        ).fetchall()
        rules = sorted(rules, key=lambda item: len(normalize_text(item[0])), reverse=True)
        for keyword, category in rules:
            normalized_keyword = normalize_text(keyword)
            if normalized_keyword and normalized_keyword in text:
                return category

    # Também prioriza termos mais específicos dentro dos padrões automáticos.
    ordered_patterns = []
    for category, terms in DEFAULT_PATTERNS.items():
        for term in terms:
            ordered_patterns.append((normalize_text(term), category))
    ordered_patterns.sort(key=lambda item: len(item[0]), reverse=True)

    for term, category in ordered_patterns:
        if term and term in text:
            return category
    return "Outros"


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
        # Faturas podem atravessar dezembro/janeiro. Evita datas futuras improváveis.
        if candidate > reference_date + timedelta(days=45):
            year -= 1
    return date(year, month, day)


def parse_brazilian_amount(value):
    """Converte valores como 'R$ 1.234,56' ou números de planilhas em float."""
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
        # Datas de parcelamento (ex.: PARC 06/12) não são datas de compra.
        prefix = line[max(0, match.start() - 14):match.start()]
        if re.search(r"\bPARC(?:ELA)?\s*$", prefix, re.IGNORECASE):
            continue
        # Evita capturar números de paginação do tipo 02/04.
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
    text = re.sub(r"\s+", " ", text).strip(" -|\t")
    return text


def parse_generic_invoice_text(text, reference_date=None):
    """Extrai despesas de texto de fatura (layout DD/MM, ex.: Banco do Brasil) e devolve linhas + diagnóstico.

    O leitor aceita uma compra por linha, descrições quebradas em linhas e,
    quando possível, mais de uma compra na mesma linha (PDFs em colunas).
    Pagamentos, créditos e saldo anterior são ignorados intencionalmente.
    """
    reference_date = reference_date or date.today()
    rows = []
    diagnostics = {
        "ignored_credits": 0,
        "unmatched_dates": 0,
        "ignored_lines": 0,
        "parsed_lines": 0,
    }
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
        rows.append({
            "Data": parsed_date.isoformat(),
            "Descrição": description,
            "Valor": round(float(amount), 2),
        })
        diagnostics["parsed_lines"] += 1

    def finish_pending():
        nonlocal pending
        if pending is None:
            return
        amount_matches = _amount_matches(pending["text"])
        if amount_matches:
            # Quando a descrição foi quebrada em várias linhas, o primeiro valor
            # monetário costuma ser o valor da compra.
            add_record(pending["date"], pending["text"], amount_matches[0][1])
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
                appended_amounts = _amount_matches(line)
                if appended_amounts:
                    finish_pending()
            continue

        # Uma data nova encerra qualquer descrição que tenha ficado pendente.
        if pending is not None:
            finish_pending()

        if len(date_matches) == 1:
            match = date_matches[0]
            description = line[match.end():]
            if amounts:
                # Se houver valor na mesma linha, usa o primeiro; valores adicionais
                # podem ser subtotais ou colunas adjacentes e ficam para revisão.
                add_record(match.group(1), description, amounts[0][1])
            else:
                pending = {"date": match.group(1), "text": description}
            continue

        # Vários lançamentos na mesma linha: tenta parear datas e valores por ordem.
        if len(amounts) >= len(date_matches):
            for index, match in enumerate(date_matches):
                end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(line)
                description = line[match.end():end]
                add_record(match.group(1), description, amounts[index][1])
        else:
            # Se a linha contém vários lançamentos mas não há um valor por data,
            # aproveita valores que estejam dentro de cada trecho e sinaliza o resto.
            for index, match in enumerate(date_matches):
                end = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(line)
                description = line[match.end():end]
                segment_amounts = _amount_matches(description)
                if segment_amounts:
                    add_record(match.group(1), description, segment_amounts[0][1])
                elif index == len(date_matches) - 1:
                    pending = {"date": match.group(1), "text": description}
                else:
                    diagnostics["unmatched_dates"] += 1

    finish_pending()

    # Remove duplicidades idênticas geradas por extração repetida de linhas/colunas.
    unique_rows = []
    seen = set()
    for row in rows:
        key = (row["Data"], normalize_text(row["Descrição"]), row["Valor"])
        if key not in seen:
            seen.add(key)
            unique_rows.append(row)

    return unique_rows, diagnostics


# ---------------------------------------------------------------------------
# Nubank
# ---------------------------------------------------------------------------
NUBANK_MONTHS = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}
_NU_MONTHS_RE = "|".join(NUBANK_MONTHS)

# Ex.: "16 SET •••• 8921 Dl*Uberrides R$ 13,96"  ou  "29 SET Pagamento em 29 SET −R$ 683,99"
NUBANK_LINE_RE = re.compile(
    rf"^(?P<day>\d{{1,2}})\s+(?P<month>{_NU_MONTHS_RE})\b\s+"
    r"(?:[•·*●.]{2,}\s*(?P<card>\d{4})\s+)?"
    r"(?P<desc>.+?)\s+"
    r"(?P<neg>[-−–]\s*)?R\$\s*(?P<amount>\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})\s*$",
    re.IGNORECASE,
)
# Linha com data e descrição, mas sem valor (o valor aparece mais abaixo).
NUBANK_HEADER_RE = re.compile(
    rf"^(?P<day>\d{{1,2}})\s+(?P<month>{_NU_MONTHS_RE})\b\s+"
    r"(?:[•·*●.]{2,}\s*\d{4}\s+)?"
    r"(?P<desc>[^R\d].*?)\s*$",
    re.IGNORECASE,
)
# Linha que contém apenas um valor: "R$ 25,74" ou "−R$ 683,99".
NUBANK_AMOUNT_ONLY_RE = re.compile(
    r"^(?P<neg>[-−–]\s*)?R\$\s*(?P<amount>\d{1,3}(?:\.\d{3})*,\d{2}|\d+,\d{2})$"
)
NUBANK_ISSUE_RE = re.compile(
    rf"EMISS[ÃA]O\s+E\s+ENVIO\s+(\d{{1,2}})\s+({_NU_MONTHS_RE})\s+(\d{{4}})", re.IGNORECASE
)
NUBANK_DUE_RE = re.compile(
    rf"FATURA\s+(\d{{1,2}})\s+({_NU_MONTHS_RE})\s+(\d{{4}})", re.IGNORECASE
)
NUBANK_TOTAL_RE = re.compile(r"Total\s+a\s+pagar\s+R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})", re.IGNORECASE)

# Lançamentos do Nubank que NÃO são gasto novo (movimentam saldo, mas se anulam
# ou apenas informam a dívida em atraso que já foi contada na fatura anterior).
NUBANK_IGNORED_TERMS = [
    "PAGAMENTO EM", "PAGAMENTO RECEBIDO", "SALDO EM ATRASO", "CREDITO DE ATRASO",
    "ENCERRAMENTO DE DIVIDA", "JUROS DE DIVIDA ENCERRADA", "ESTORNO",
]


def is_nubank_text(text):
    normalized = normalize_text(str(text or "")[:6000])
    if "NUBANK" in normalized or "NU PAGAMENTOS" in normalized:
        return True
    # Fallback para texto colado sem cabeçalho: várias linhas começando com "DD MMM".
    hits = sum(
        1 for line in str(text or "").splitlines()
        if re.match(rf"^\s*\d{{1,2}}\s+(?:{_NU_MONTHS_RE})\b", line, re.IGNORECASE)
    )
    return hits >= 3


def _nubank_year_resolver(text, reference_date):
    """Devolve função (dia, mês) -> date, inferindo o ano pelo cabeçalho da fatura.

    As transações do Nubank vêm sem ano ("16 SET"). Usa a data de emissão (ou o
    vencimento) como âncora; meses posteriores à âncora pertencem ao ano anterior
    (ex.: compra de DEZ numa fatura emitida em JAN).
    """
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
    """Extrai despesas de uma fatura do Nubank (PDF ou texto copiado)."""
    reference_date = reference_date or date.today()
    text = str(text or "").replace("\r", "\n")
    resolve = _nubank_year_resolver(text, reference_date)
    diagnostics = {"ignored_credits": 0, "unmatched_dates": 0, "ignored_lines": 0, "parsed_lines": 0}

    rows = []
    pending = None  # lançamento cuja linha de valor veio separada (após notas explicativas)

    def emit(day, month, description, negative, amount_text):
        description = description.strip(" -|")
        normalized = normalize_text(description)
        amount = parse_brazilian_amount(amount_text)
        if negative or amount <= 0 or any(term in normalized for term in NUBANK_IGNORED_TERMS):
            diagnostics["ignored_credits"] += 1
            return
        try:
            parsed_date = resolve(int(day), NUBANK_MONTHS[month.upper()])
        except ValueError:
            diagnostics["ignored_lines"] += 1
            return
        rows.append({"Data": parsed_date.isoformat(), "Descrição": description, "Valor": round(amount, 2)})
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

        # Alguns PDFs trazem o valor numa linha própria, depois das notas do lançamento
        # (ex.: "Total a pagar ... juros)." seguido de "R$ 25,74").
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
    """Detecta o emissor da fatura e usa o leitor adequado (Nubank ou layout DD/MM)."""
    if is_nubank_text(text):
        rows, diagnostics = parse_nubank_text(text, reference_date)
        if rows:
            return rows, diagnostics
    rows, diagnostics = parse_generic_invoice_text(text, reference_date)
    diagnostics.setdefault("bank", "Banco do Brasil / genérico")
    bb_total = re.search(r"Total\s+da\s+Fatura\s+R\$\s*(\d{1,3}(?:\.\d{3})*,\d{2})", str(text or ""), re.IGNORECASE)
    if bb_total:
        diagnostics["invoice_total"] = parse_brazilian_amount(bb_total.group(1))
    return rows, diagnostics


def extract_pdf(file_bytes):
    """Extrai o PDF em dois modos e escolhe o texto com melhor leitura."""
    standard_pages = []
    layout_pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            standard_pages.append(page.extract_text(layout=False) or "")
            layout_pages.append(page.extract_text(layout=True) or "")

    standard_text = "\n".join(standard_pages)
    layout_text = "\n".join(layout_pages)
    standard_rows, standard_diag = parse_invoice_text(standard_text)
    layout_rows, layout_diag = parse_invoice_text(layout_text)

    standard_score = len(standard_rows) * 2 - standard_diag["unmatched_dates"]
    layout_score = len(layout_rows) * 2 - layout_diag["unmatched_dates"]
    if layout_score > standard_score:
        return layout_rows, layout_diag, layout_text
    return standard_rows, standard_diag, standard_text


def load_transactions(conn):
    return pd.read_sql_query(
        """SELECT id,
                  transaction_date AS Data,
                  description AS Descrição,
                  amount AS Valor,
                  category AS Categoria,
                  source_file AS Arquivo,
                  card AS Cartão
           FROM transactions
           ORDER BY transaction_date DESC, id DESC""",
        conn,
    )


def normalize_table_rows(dataframe, date_column, description_column, amount_column):
    """Normaliza uma tabela CSV/XLSX para o formato usado pelo importador."""
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
            rows.append({"Data": parsed_date.isoformat(), "Descrição": description, "Valor": round(amount, 2)})
            diagnostics["parsed_lines"] += 1
        except Exception:
            diagnostics["unmatched_dates"] += 1
    return rows, diagnostics
