import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

# isola o banco de teste ANTES de importar financeiro
# atribuição forçada: os testes apagam tabelas e nunca podem tocar no banco real
os.environ["FINANCEIRO_DB"] = os.path.join(tempfile.mkdtemp(), "radar_testes.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import financeiro  # noqa: E402
from financeiro import (  # noqa: E402
    CATEGORIES, category_variation, db_connection, detect_recurrences,
    forecast_month, guess_category, learn_from_correction, outliers,
    parse_invoice_text, pending_review, save_transactions, simulate_purchase,
)


class ParserTests(unittest.TestCase):
    def test_categories_from_sample_invoice(self):
        expected = {
            "IFD*55454286 FRANCISCO": "Alimentação",
            "TOCAPUB": "Alimentação",
            "GLACAI": "Alimentação",
            "IFD*RAIMUNDO NONATO XA": "Alimentação",
            "DL*99 RIDE": "Transporte",
            "PG *99 L2L 99 RIDE": "Transporte",
            "DL*UberRides": "Transporte",
            "NELFARMA COMERCIO DE P": "Farmácia",
            "CAIO FARMA": "Farmácia",
            "Redepharma": "Farmácia",
            "PAGUE MENOS": "Farmácia",
            "DL*GOOGLE G1SK00": "Assinaturas e serviços",
            "Amazon Ad free for Prim": "Assinaturas e serviços",
            "DL*GOOGLE YouTub": "Assinaturas e serviços",
            "AMAZON PRIME": "Assinaturas e serviços",
            "MULTA POR ATRASO": "Tarifas e encargos",
            "JUROS DE MORA": "Tarifas e encargos",
            "ENCARGOS FINANC ROTATIVO": "Tarifas e encargos",
        }
        for description, category in expected.items():
            with self.subTest(description=description):
                self.assertEqual(guess_category(description)[0], category)

    def test_payment_is_ignored_and_purchases_are_parsed(self):
        text = """\
        16/10 PGTO. COBRANCA R$ -571,50
        03/10 DL*99 RIDE SAO PAULO BR R$ 12,70
        04/10 DL*UberRides SAO PAULO BR R$ 20,70
        01/05 AMAZON PRIME PARC 06/12 SAO PAULO BR R$ 13,90
        """
        rows, diagnostics = parse_invoice_text(text)
        self.assertEqual(len(rows), 3)
        self.assertEqual(diagnostics["ignored_credits"], 1)
        self.assertEqual(rows[0]["Descrição"], "DL*99 RIDE")
        self.assertEqual(rows[2]["Descrição"], "AMAZON PRIME PARC 06/12")

    def test_two_transactions_on_same_line(self):
        text = "08/10 IFD*55454286 FRANCISCO JOAO PESSOA 11/10 TOCAPUB JOAO PESSOA BR R$ 46,38 R$ 27,00"
        rows, _ = parse_invoice_text(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["Valor"] for row in rows], [46.38, 27.00])
        self.assertEqual([row["Descrição"] for row in rows],
                         ["IFD*55454286 FRANCISCO", "TOCAPUB"])


NUBANK_SAMPLE = """\
FATURA 22 OUT 2026 EMISSÃO E ENVIO 15 OUT 2026
Total a pagar R$ 684,23
Iury Franca R$ 615,99
16 SET •••• 8921 Dl*Uberrides R$ 13,96
17 SET •••• 8921 Pague Menos R$ 318,99
14 OUT •••• 8921 Ifd*Ifood Club R$ 5,95
Pagamentos e Financiamentos -R$ 615,75
29 SET Pagamento em 29 SET −R$ 683,99
22 SET Iury Mendonca Freire de Franca R$ 25,74
Total a pagar: R$ 25,73 (valor da transação de R$ 23,18 + R$ 0,15 de IOF + R$
2,41 de juros).
22 SET Saldo em atraso R$ 683,99
22 SET Crédito de atraso −R$ 683,99
29 SET IOF de atraso R$ 3,00
Referente ao valor em aberto de R$ 683,99 de 25/09/2026 (Valor original:
R$ 686,59)
29 SET Juros de atraso R$ 25,76
29 SET Multa de atraso R$ 13,73
29 SET Juros de dívida encerrada R$ 42,50
29 SET Encerramento de dívida −R$ 42,50
"""


class NubankParserTests(unittest.TestCase):
    def test_nubank_purchases_and_charges(self):
        rows, diagnostics = parse_invoice_text(NUBANK_SAMPLE)
        self.assertEqual(diagnostics["bank"], "Nubank")
        self.assertEqual(
            [(r["Data"], r["Descrição"], r["Valor"]) for r in rows],
            [
                ("2026-09-16", "Dl*Uberrides", 13.96),
                ("2026-09-17", "Pague Menos", 318.99),
                ("2026-10-14", "Ifd*Ifood Club", 5.95),
                ("2026-09-22", "Iury Mendonca Freire de Franca", 25.74),
                ("2026-09-29", "IOF de atraso", 3.00),
                ("2026-09-29", "Juros de atraso", 25.76),
                ("2026-09-29", "Multa de atraso", 13.73),
            ],
        )

    def test_nubank_ignores_payments_credits_and_cancelled_debt(self):
        rows, diagnostics = parse_invoice_text(NUBANK_SAMPLE)
        descriptions = " | ".join(r["Descrição"] for r in rows).upper()
        for term in ("PAGAMENTO", "SALDO EM ATRASO", "CRÉDITO", "ENCERRAD"):
            self.assertNotIn(term, descriptions)
        self.assertEqual(diagnostics["ignored_credits"], 5)
        self.assertEqual(diagnostics["invoice_total"], 684.23)

    def test_nubank_year_rollover(self):
        text = (
            "Nubank\nFATURA 22 JAN 2027 EMISSÃO E ENVIO 15 JAN 2027\n"
            "20 DEZ •••• 8921 Uber R$ 10,00\n05 JAN •••• 8921 Uber R$ 12,00\n"
        )
        rows, _ = parse_invoice_text(text)
        self.assertEqual([r["Data"] for r in rows], ["2026-12-20", "2027-01-05"])

    def test_nubank_amount_on_separate_line(self):
        text = """\
FATURA 22 OUT 2026 EMISSÃO E ENVIO 15 OUT 2026
Total a pagar R$ 684,23
16 SET •••• 8921 Dl*Uberrides R$ 13,96
22 SET Iury Mendonca Freire de Franca
Total a pagar: R$ 25,73 (valor da transação de R$ 23,18 + R$ 0,15 de IOF + R$
2,41 de juros).
R$ 25,74
22 SET Saldo em atraso
• Saldo em aberto de R$ 686,59. Valor total acumulado de encargos (juros e
• Valor original: R$ 686,59
R$ 683,99
29 SET IOF de atraso
Referente ao valor em aberto de R$ 683,99 de 25/09/2026 (Valor original:
R$ 686,59)
R$ 3,00
29 SET Juros de dívida encerrada R$ 42,50
"""
        rows, _ = parse_invoice_text(text)
        self.assertEqual(
            [(r["Descrição"], r["Valor"]) for r in rows],
            [("Dl*Uberrides", 13.96),
             ("Iury Mendonca Freire de Franca", 25.74),
             ("IOF de atraso", 3.00)],
        )

    def test_late_fee_categories(self):
        for description in ("IOF de atraso", "Juros de atraso", "Multa de atraso"):
            self.assertEqual(guess_category(description)[0], "Tarifas e encargos")

    def test_bb_layout_still_uses_generic_parser(self):
        rows, diagnostics = parse_invoice_text("03/10 DL*99 RIDE SAO PAULO BR R$ 12,70")
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(diagnostics.get("bank"), "Nubank")


class RadarTests(unittest.TestCase):
    def setUp(self):
        self.conn = db_connection()
        self.conn.execute("DELETE FROM transactions")
        self.conn.execute("DELETE FROM category_rules")
        self.conn.execute("DELETE FROM recurrences")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_guess_returns_category_and_subcategory(self):
        self.assertEqual(guess_category("DL*UberRides", self.conn), ("Transporte", "Aplicativo"))
        self.assertEqual(guess_category("IFD*IFOOD", self.conn), ("Alimentação", "Restaurante"))
        self.assertEqual(guess_category("SUPERMERCADO XYZ", self.conn), ("Alimentação", "Mercado"))

    def test_learn_from_correction_creates_rule(self):
        learn_from_correction(self.conn, "PADARIA CENTRAL LTDA", "Alimentação", "Mercado")
        cat, sub = guess_category("PADARIA CENTRAL LTDA 2", self.conn)
        self.assertEqual((cat, sub), ("Alimentação", "Mercado"))

    def test_category_variation(self):
        save_transactions(self.conn, [
            {"Data": "2026-09-10", "Descrição": "IFOOD", "Valor": 100.0},
            {"Data": "2026-10-10", "Descrição": "IFOOD", "Valor": 150.0},
        ], "teste", "")
        var = category_variation(self.conn)
        self.assertGreater(var.loc["Alimentação", "var_pct"], 40)

    def test_detect_recurrences(self):
        rows = [
            {"Data": f"2026-0{m}-05", "Descrição": "NETFLIX", "Valor": 39.90}
            for m in (7, 8, 9, 10)
        ]
        save_transactions(self.conn, rows, "teste", "")
        rec = detect_recurrences(self.conn)
        self.assertTrue(any("NETFLIX" in m for m in rec["merchant"].tolist()))

    def test_forecast_returns_band(self):
        save_transactions(self.conn, [
            {"Data": date.today().replace(day=1).isoformat(),
             "Descrição": "IFOOD", "Valor": 100.0},
        ], "teste", "")
        fc = forecast_month(self.conn)
        self.assertGreater(fc["projected"], 0)
        self.assertLessEqual(fc["low"], fc["high"])
        self.assertIn("assumptions", fc)

    def test_simulate_purchase(self):
        save_transactions(self.conn, [
            {"Data": date.today().replace(day=1).isoformat(),
             "Descrição": "IFOOD", "Valor": 100.0},
        ], "teste", "")
        sim = simulate_purchase(self.conn, 50.0)
        self.assertAlmostEqual(sim["after"], sim["before"] + 50.0, places=2)

    def test_pending_review(self):
        self.conn.execute(
            "INSERT INTO transactions (fingerprint, transaction_date, description, amount, category, reviewed) "
            "VALUES ('x', '2026-10-10', 'DESCONHECIDO', 10.0, 'Outros', 0)")
        self.conn.commit()
        review = pending_review(self.conn)
        self.assertEqual(len(review), 1)

    def test_outliers_needs_min_sample(self):
        # Adicionamos 4 transações normais e 1 muito acima para garantir o desvio padrão baixo
        self.conn.execute("DELETE FROM transactions")
        self.conn.commit()
        save_transactions(self.conn, [
            {"Data": "2026-10-01", "Descrição": "IFOOD", "Valor": 10.0},
            {"Data": "2026-10-02", "Descrição": "IFOOD", "Valor": 12.0},
            {"Data": "2026-10-03", "Descrição": "IFOOD", "Valor": 11.0},
            {"Data": "2026-10-04", "Descrição": "IFOOD", "Valor": 10.0},
            {"Data": "2026-10-05", "Descrição": "IFOOD", "Valor": 1000.0},
        ], "teste", "")
        
        out = outliers(self.conn)
        
        self.assertFalse(out.empty)
        self.assertEqual(out.iloc[0]["amount"], 1000.0) # O maior deve ser o outlier


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "banco.db"

    def connect(self):
        conn = financeiro.db_connection(self.path)
        self.addCleanup(conn.close)
        return conn

    def test_old_database_is_migrated_with_backup(self):
        old = sqlite3.connect(self.path)
        old.executescript("""
            CREATE TABLE transactions (id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE, transaction_date TEXT, description TEXT,
                amount REAL NOT NULL, category TEXT NOT NULL DEFAULT 'Outros',
                source_file TEXT, card TEXT DEFAULT '', created_at TEXT);
            CREATE TABLE category_rules (id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE, category TEXT NOT NULL);
            INSERT INTO category_rules (keyword, category) VALUES ('PRECO REAL', 'Alimentação');
        """)
        old.execute("INSERT INTO transactions (fingerprint, transaction_date, description, amount) "
                    "VALUES (?, '2026-10-01', 'UBER', 10.0)",
                    (financeiro.fingerprint("2026-10-01", "UBER", 10.0),))
        old.commit()
        old.close()
        conn = self.connect()
        self.assertEqual(guess_category("Preco Real", conn)[0], "Alimentação")
        row = {"Data": "2026-10-01", "Descrição": "UBER", "Valor": 10.0}
        self.assertEqual(save_transactions(conn, [row], "x", ""), (0, 1, 0))
        self.assertEqual(len(list(self.path.parent.glob("banco.backup-*.db"))), 1)

    def test_categories_are_not_duplicated(self):
        first = self.connect().execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        second = self.connect().execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        self.assertEqual(first, second)

    def test_identical_purchases_are_kept_but_reimport_is_ignored(self):
        conn = self.connect()
        rows, diagnostics = parse_invoice_text("03/10 UBER R$ 10,00\n03/10 UBER R$ 10,00")
        self.assertEqual(diagnostics["repeated"], 1)
        self.assertEqual(save_transactions(conn, rows, "x", ""), (2, 0, 0))
        self.assertEqual(save_transactions(conn, rows, "x", ""), (0, 2, 0))

    def test_learned_rule_matches_its_own_description(self):
        conn = self.connect()
        learn_from_correction(conn, "Mariana da Nobrega Ma", "Saúde", None)
        self.assertEqual(guess_category("Mariana da Nobrega Ma", conn)[0], "Saúde")

    def test_short_terms_need_whole_word(self):
        self.assertEqual(guess_category("PRAIA BAR")[0], "Outros")
        self.assertEqual(guess_category("DROGA RAIA")[0], "Farmácia")

    def test_correction_in_preview_is_learned(self):
        conn = self.connect()
        save_transactions(conn, [{"Data": "2026-10-01", "Descrição": "Preco Real", "Valor": 5.0,
                                  "Categoria": "Alimentação", "Subcategoria": "Mercado"}], "x", "")
        self.assertEqual(guess_category("Preco Real", conn), ("Alimentação", "Mercado"))

    def test_blank_editor_row_is_invalid(self):
        conn = self.connect()
        blank = {"Data": None, "Descrição": None, "Valor": float("nan")}
        self.assertEqual(save_transactions(conn, [blank], "x", ""), (0, 0, 1))

    def test_table_import_reads_iso_dates_and_rejects_empty_amount(self):
        frame = pd.DataFrame({"d": ["2026-10-03", "04/10/2026", "2026-10-05"],
                              "t": ["A", "B", "C"], "v": ["1,00", "2,00", float("nan")]})
        rows, _ = financeiro.normalize_table_rows(frame, "d", "t", "v")
        self.assertEqual([r["Data"] for r in rows], ["2026-10-03", "2026-10-04"])

    def test_frequent_rides_are_not_recurrences(self):
        conn = self.connect()
        save_transactions(conn, [{"Data": f"2026-10-{d:02d}", "Descrição": "UBER", "Valor": 12.0}
                                 for d in (1, 2, 3, 15, 17)], "x", "")
        self.assertTrue(detect_recurrences(conn).empty)

    def test_installments_and_forecast(self):
        conn = self.connect()
        save_transactions(conn, [{"Data": "2026-05-01", "Descrição": "LOJA PARC 06/08",
                                  "Valor": 50.0}], "x", "")
        future = financeiro.installment_commitments(conn)
        self.assertEqual(future["month"].tolist(), ["2026-11", "2026-12"])
        forecast = forecast_month(conn, today=date(2026, 11, 20))
        self.assertEqual(forecast["installment_estimate"], 50.0)
        self.assertFalse(forecast["has_current"])

    def test_budget_applies_to_simulation(self):
        conn = self.connect()
        financeiro.set_budget(conn, date.today().strftime("%Y-%m"), 100, "Lazer")
        simulation = simulate_purchase(conn, 300, "Lazer", installments=2)
        self.assertEqual(simulation["delta"], 150)
        self.assertTrue(simulation["over_category"])

    def test_manual_entries_may_repeat(self):
        conn = self.connect()
        for _ in range(2):
            self.assertTrue(financeiro.add_manual_transaction(conn, date.today(), "Pão", 5))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)

    def test_readonly_connection_leaves_file_untouched(self):
        self.connect().close()
        before = self.path.read_bytes()
        conn = financeiro.readonly_connection(self.path)
        detect_recurrences(conn)
        conn.close()
        self.assertEqual(before, self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()