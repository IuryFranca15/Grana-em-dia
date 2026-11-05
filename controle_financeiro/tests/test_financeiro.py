import unittest

from financeiro import guess_category, parse_invoice_text


class FinanceiroParserTests(unittest.TestCase):
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
                self.assertEqual(guess_category(description), category)

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
        self.assertEqual([row["Descrição"] for row in rows], ["IFD*55454286 FRANCISCO", "TOCAPUB"])


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
        # Layout em que o valor vem depois das notas do lançamento.
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
            [("Dl*Uberrides", 13.96), ("Iury Mendonca Freire de Franca", 25.74), ("IOF de atraso", 3.00)],
        )

    def test_late_fee_categories(self):
        for description in ("IOF de atraso", "Juros de atraso", "Multa de atraso"):
            self.assertEqual(guess_category(description), "Tarifas e encargos")

    def test_bb_layout_still_uses_generic_parser(self):
        rows, diagnostics = parse_invoice_text("03/10 DL*99 RIDE SAO PAULO BR R$ 12,70")
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(diagnostics.get("bank"), "Nubank")


if __name__ == "__main__":
    unittest.main()
