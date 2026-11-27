"""Relatório somente-leitura sobre o banco atual."""
import os
from financeiro import (
    db_connection, category_variation, detect_recurrences,
    forecast_month, outliers, pending_review,
)

def main():
    print(f"Banco: {os.environ.get('FINANCEIRO_DB', 'data/financeiro.db')}")
    conn = db_connection()
    print("\n=== Previsão do mês ===")
    print(forecast_month(conn))
    print("\n=== Variação por categoria ===")
    print(category_variation(conn))
    print("\n=== Recorrências ===")
    print(detect_recurrences(conn))
    print("\n=== Fora do padrão ===")
    print(outliers(conn))
    print("\n=== Fila de revisão (top 10) ===")
    print(pending_review(conn).head(10))
    conn.close()

if __name__ == "__main__":
    main()