"""Relatório somente-leitura sobre o banco atual (analisa uma cópia em memória)."""
from financeiro import (
    DB_PATH, category_variation, detect_recurrences, forecast_month,
    outliers, pending_review, readonly_connection,
)


def main():
    print(f"Banco: {DB_PATH}")
    conn = readonly_connection()
    print("\n=== Previsão do mês ===")
    for key, value in forecast_month(conn).items():
        print(f"{key}: {value}")
    print("\n=== Variação por categoria ===")
    print(category_variation(conn))
    print("\n=== Recorrências ===")
    print(detect_recurrences(conn, persist=False))
    print("\n=== Fora do padrão ===")
    print(outliers(conn))
    print("\n=== Fila de revisão (top 10) ===")
    print(pending_review(conn).head(10))
    conn.close()


if __name__ == "__main__":
    main()
