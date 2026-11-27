from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from financeiro import (
    CATEGORIES, DB_PATH, SUBCATEGORIES, category_variation, db_connection,
    detect_recurrences, extract_pdf, file_checksum, forecast_month,
    guess_category, learn_from_correction, load_transactions, monthly_summary,
    normalize_table_rows, outliers, parse_invoice_text, pending_review,
    register_import, save_transactions, simulate_purchase,
)

st.set_page_config(page_title="Grana em Dia | Radar", page_icon="💸", layout="wide")

st.markdown("""<style>
    html, body, [class*="css"] { font-family: Inter, ui-sans-serif, system-ui, sans-serif; }
    .stApp { background: #F6F7F2; }
    [data-testid="stAppViewContainer"] > .main .block-container { max-width: 1320px; padding-top: 1.7rem; }
    [data-testid="stSidebar"] { background: #173A35; }
    [data-testid="stSidebar"] * { color: #F6F7F2; }
    .hero { background: linear-gradient(115deg, #173A35 0%, #24584D 70%, #397564 100%);
        padding: 1.55rem 1.8rem; border-radius: 24px; color: #fff; margin-bottom: 1.3rem; }
    .hero h1 { color: #fff; font-size: 2rem; margin: .3rem 0 .2rem 0; }
    .hero p { color: #DCEAE4; margin: 0; }
    .hero-kicker { color: #F9B78E; font-size: .76rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
    div[data-testid="stMetric"] { background: #fff; border: 1px solid #E5EAE2; border-radius: 18px; padding: 1rem; }
</style>""", unsafe_allow_html=True)


def brl(v):
    return f"R$ {float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hero(title, subtitle, kicker="RADAR INTELIGENTE"):
    st.markdown(f'<div class="hero"><div class="hero-kicker">{kicker}</div>'
                f'<h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


conn = db_connection()

with st.sidebar:
    st.markdown("### 💸 Grana em Dia")
    st.divider()
    page = st.radio("MENU", [
        "🏡 Visão geral", "📥 Importar fatura", "🧾 Lançamentos",
        "🏷️ Categorias e regras", "📡 Radar", "🌱 Planejamento",
    ], label_visibility="collapsed")
    st.divider()
    st.caption(f"Banco: {DB_PATH.name}")

df_all = load_transactions(conn)

# --- Importar ---
if page == "📥 Importar fatura":
    hero("Importar fatura", "Confira antes de salvar. O sistema evita duplicidades por checksum.")
    card = st.text_input("Cartão", placeholder="Ex.: Cartão principal • final 6825")
    tab_pdf, tab_text, tab_table = st.tabs(["📄 PDF", "✍️ Texto", "📊 CSV/Excel"])

    def handle_preview(rows, diagnostics, source_name, checksum):
        st.session_state["preview"] = {"rows": rows, "diagnostics": diagnostics,
                                       "source": source_name, "checksum": checksum}

    with tab_pdf:
        pdf_file = st.file_uploader("Fatura PDF", type=["pdf"])
        if pdf_file and st.button("Ler PDF", type="primary"):
            try:
                content = pdf_file.getvalue()
                rows, diag, _ = extract_pdf(content)
                handle_preview(rows, diag, pdf_file.name, file_checksum(content))
            except Exception as e:
                st.error(f"Erro ao abrir PDF: {e}")

    with tab_text:
        txt = st.text_area("Cole o texto da fatura", height=220)
        if st.button("Analisar texto", type="primary"):
            rows, diag = parse_invoice_text(txt)
            handle_preview(rows, diag, "Fatura colada", file_checksum(txt.encode()))

    with tab_table:
        table_file = st.file_uploader("CSV/Excel", type=["csv", "xlsx"])
        if table_file:
            try:
                raw = (pd.read_excel(table_file) if table_file.name.endswith("xlsx")
                       else pd.read_csv(table_file, sep=None, engine="python", encoding="utf-8-sig"))
                cols = list(raw.columns)
                c1, c2, c3 = st.columns(3)
                dc = c1.selectbox("Data", cols, 0)
                ec = c2.selectbox("Descrição", cols, min(1, len(cols) - 1))
                vc = c3.selectbox("Valor", cols, len(cols) - 1)
                if st.button("Analisar planilha", type="primary"):
                    rows, diag = normalize_table_rows(raw, dc, ec, vc)
                    handle_preview(rows, diag, table_file.name, file_checksum(table_file.getvalue()))
            except Exception as e:
                st.error(f"Erro na planilha: {e}")

    preview = st.session_state.get("preview")
    if preview and preview["rows"]:
        st.divider()
        st.subheader("Confira antes de salvar 👀")
        df = pd.DataFrame(preview["rows"])
        results = df["Descrição"].map(lambda d: guess_category(d, conn))
        df["Categoria"] = [r[0] for r in results]
        df["Subcategoria"] = [r[1] for r in results]
        df["Data"] = pd.to_datetime(df["Data"]).dt.date

        m1, m2, m3 = st.columns(3)
        m1.metric("Lançamentos", len(df))
        m2.metric("Total", brl(df["Valor"].sum()))
        m3.metric("Ignorados", preview["diagnostics"].get("ignored_credits", 0))
        if preview["diagnostics"].get("invoice_total"):
            diff = abs(df["Valor"].sum() - preview["diagnostics"]["invoice_total"])
            if diff <= 0.05:
                st.success("Total bate com a fatura.")
            else:
                st.warning(f"Diferença de {brl(diff)} vs. total da fatura.")

        edited = st.data_editor(
            df, use_container_width=True, hide_index=True, num_rows="dynamic",
            column_config={
                "Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "Valor": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
                "Categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIES),
                "Subcategoria": st.column_config.TextColumn("Subcategoria"),
            },
            key=f"editor_{hash(str(preview['rows']))}",
        )
        if st.button("Salvar lançamentos", type="primary", use_container_width=True):
            import_id, duplicate_import = register_import(
                conn, preview["source"], "importador", preview["checksum"])
            if duplicate_import:
                st.warning("Este arquivo já foi importado (mesmo checksum). Deduplicação continua ativa.")
            saved, dups, invalid = save_transactions(
                conn, edited.to_dict("records"), preview["source"], card, import_id)
            st.session_state.pop("preview", None)
            st.success(f"{saved} salvos, {dups} duplicados, {invalid} inválidos.")
            st.rerun()

# --- Lançamentos ---
elif page == "🧾 Lançamentos":
    hero("Seus lançamentos", "Fila de revisão e aprendizado por correção.")
    if df_all.empty:
        st.info("Sem lançamentos. Importe uma fatura.")
    else:
        review = pending_review(conn)
        if not review.empty:
            st.subheader("🔍 Fila de revisão")
            st.caption("Corrija e marque como revisado. O sistema cria uma regra reutilizável.")
            edited = st.data_editor(
                review, use_container_width=True, hide_index=True,
                column_config={
                    "Categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIES),
                    "Subcategoria": st.column_config.TextColumn("Subcategoria"),
                    "Revisado": st.column_config.CheckboxColumn("Revisado", default=True),
                },
                key="review_editor",
            )
            if st.button("Aplicar correções e aprender", type="primary"):
                for _, r in edited.iterrows():
                    cat = r["Categoria"]
                    sub = r.get("Subcategoria") or None
                    conn.execute(
                        "UPDATE transactions SET category=?, subcategory=?, reviewed=? WHERE id=?",
                        (cat, sub, int(r.get("Revisado", 1)), int(r["id"])))
                    if cat != "Outros":
                        learn_from_correction(conn, r["Descrição"], cat, sub)
                conn.commit()
                st.success("Correções aplicadas e regras aprendidas.")
                st.rerun()
            st.divider()

        df_all["Data"] = pd.to_datetime(df_all["Data"])
        f1, f2, f3 = st.columns([1, 1, 2])
        periods = sorted(df_all["Data"].dt.to_period("M").astype(str).unique(), reverse=True)
        sel_period = f1.selectbox("Mês", ["Todos"] + periods)
        sel_cat = f2.selectbox("Categoria", ["Todas"] + CATEGORIES)
        search = f3.text_input("Buscar", placeholder="Uber, farmácia...")
        filtered = df_all.copy()
        if sel_period != "Todos":
            filtered = filtered[filtered["Data"].dt.to_period("M").astype(str) == sel_period]
        if sel_cat != "Todas":
            filtered = filtered[filtered["Categoria"] == sel_cat]
        if search.strip():
            filtered = filtered[filtered["Descrição"].str.contains(search, case=False, na=False)]

        k1, k2 = st.columns(2)
        k1.metric("Total filtrado", brl(filtered["Valor"].sum()))
        k2.metric("Lançamentos", len(filtered))

        edited = st.data_editor(
            filtered.drop(columns=["id"]), use_container_width=True, hide_index=True,
            column_config={
                "Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "Valor": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
                "Categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIES),
            },
            disabled=["Data", "Descrição", "Estabelecimento", "Valor", "Arquivo", "Cartão", "Tipo"],
            key="tx_editor",
        )
        if st.button("Salvar alterações", type="primary"):
            for pos, (_, row) in enumerate(edited.iterrows()):
                tid = int(filtered.iloc[pos]["id"])
                conn.execute("UPDATE transactions SET category=?, subcategory=? WHERE id=?",
                             (row["Categoria"], row.get("Subcategoria"), tid))
            conn.commit()
            st.success("Atualizado.")
            st.rerun()

        st.download_button("⬇️ Exportar CSV",
                           filtered.drop(columns=["id"]).to_csv(index=False).encode("utf-8-sig"),
                           "lancamentos.csv", "text/csv")

# --- Categorias e regras ---
elif page == "🏷️ Categorias e regras":
    hero("Categorias e regras", "Regras com prioridade, subcategorias e aprendizado por correção.")
    rules = pd.read_sql_query(
        "SELECT id, keyword AS Palavra, category AS Categoria, subcategory AS Subcategoria, "
        "priority AS Prioridade, active AS Ativa, source AS Origem FROM category_rules "
        "ORDER BY priority DESC, keyword", conn)
    st.subheader("Regras cadastradas")
    if not rules.empty:
        st.dataframe(rules.drop(columns=["id"]), use_container_width=True, hide_index=True)
        rid = st.selectbox("Regra para ativar/desativar", rules["Palavra"].tolist())
        c1, c2 = st.columns(2)
        if c1.button("Alternar ativa/inativa"):
            row = rules[rules["Palavra"] == rid].iloc[0]
            conn.execute("UPDATE category_rules SET active=? WHERE id=?",
                         (0 if row["Ativa"] else 1, int(row["id"])))
            conn.commit()
            st.rerun()
        if c2.button("Excluir regra"):
            conn.execute("DELETE FROM category_rules WHERE keyword=?", (rid,))
            conn.commit()
            st.rerun()
    else:
        st.caption("Nenhuma regra personalizada ainda.")

    st.divider()
    st.subheader("Nova regra")
    with st.form("new_rule"):
        c1, c2, c3, c4 = st.columns(4)
        kw = c1.text_input("Palavra/trecho")
        cat = c2.selectbox("Categoria", CATEGORIES)
        sub = c3.selectbox("Subcategoria (opcional)", [""] + sum(SUBCATEGORIES.values(), []))
        prio = c4.number_input("Prioridade", 0, 100, 10)
        if st.form_submit_button("Adicionar"):
            if kw.strip():
                conn.execute(
                    "INSERT OR REPLACE INTO category_rules (keyword, category, subcategory, priority, active, source) "
                    "VALUES (?, ?, ?, ?, 1, 'manual')", (kw.strip(), cat, sub or None, prio))
                conn.commit()
                st.success("Regra adicionada.")
                st.rerun()

# --- Radar ---
elif page == "📡 Radar":
    hero("Radar", "Para onde vai o dinheiro, o que mudou e o que está fora do padrão.")
    if df_all.empty:
        st.info("Importe dados para ativar o radar.")
    else:
        summary = monthly_summary(conn)
        c1, c2, c3 = st.columns(3)
        c1.metric("Total do mês", brl(summary["total"]))
        c2.metric("Categorias no top 5", len(summary["top_categories"]))
        c3.metric("Recorrências detectadas", summary["recurrences"])

        st.subheader("Variação por categoria")
        variation = category_variation(conn)
        if not variation.empty and "var_abs" in variation.columns:
            st.dataframe(
                variation.reset_index()[["Categoria", "var_abs", "var_pct"]]
                .rename(columns={"var_abs": "Variação R$", "var_pct": "Variação %"})
                .style.format({"Variação R$": "R$ {:,.2f}", "Variação %": "{:.1f}%"}),
                use_container_width=True, hide_index=True)
        else:
            st.caption("Histórico insuficiente para comparar meses.")

        st.subheader("Recorrências")
        if st.button("Detectar recorrências agora"):
            rec = detect_recurrences(conn)
            if rec.empty:
                st.info("Nenhuma recorrência com confiança suficiente.")
            else:
                st.dataframe(rec, use_container_width=True, hide_index=True)

        st.subheader("Despesas fora do padrão")
        out = outliers(conn)
        if out.empty:
            st.caption("Nenhum lançamento muito acima do habitual.")
        else:
            st.dataframe(
                out[["transaction_date", "description", "amount", "category"]]
                .rename(columns={"transaction_date": "Data", "description": "Descrição",
                                 "amount": "Valor", "category": "Categoria"}),
                use_container_width=True, hide_index=True)

# --- Planejamento ---
elif page == "🌱 Planejamento":
    hero("Planejamento", "Previsão transparente com faixa de incerteza e simulador.")
    if df_all.empty:
        st.info("Importe dados para ver previsões.")
    else:
        fc = forecast_month(conn)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Gasto até hoje", brl(fc["spent"]))
        c2.metric("Previsão", brl(fc["projected"]))
        c3.metric("Faixa (baixa)", brl(fc["low"]))
        c4.metric("Faixa (alta)", brl(fc["high"]))
        st.caption(f"Premissas: {fc['assumptions']} | "
                   f"Dia {fc['days_elapsed']} de {fc['days_in_month']} | "
                   f"Histórico: {fc['history_months']} meses.")
        if fc["history_months"] < 3:
            st.warning("Pouco histórico: previsão com faixa ampla. Use como estimativa.")

        st.divider()
        st.subheader("Simulador de compra")
        c1, c2 = st.columns(2)
        amt = c1.number_input("Valor da compra (R$)", min_value=1.0, step=10.0, format="%.2f")
        cat_sim = c2.selectbox("Categoria", CATEGORIES)
        if st.button("Simular", type="primary"):
            sim = simulate_purchase(conn, amt, cat_sim)
            a, b, c = st.columns(3)
            a.metric("Previsão atual", brl(sim["before"]))
            b.metric("Após a compra", brl(sim["after"]), delta=f"+{brl(sim['delta'])}")
            c.metric("Faixa", f"{brl(sim['forecast_low'])} – {brl(sim['forecast_high'])}")
            if sim["over_budget"]:
                st.error(f"Orçamento estourado (limite: {brl(sim['budget_limit'])}).")
            elif sim["budget_limit"]:
                st.success(f"Dentro do orçamento (limite: {brl(sim['budget_limit'])}).")

# --- Visão geral ---
else:
    hero("Seu dinheiro, mais organizado.", "Painel de decisão: total, mudanças e previsão.")
    if df_all.empty:
        st.info("Importe uma fatura para começar.")
    else:
        fc = forecast_month(conn)
        c1, c2, c3 = st.columns(3)
        c1.metric("Total histórico", brl(df_all["Valor"].sum()))
        c2.metric("Gasto no mês", brl(fc["spent"]))
        c3.metric("Previsão de fechamento", brl(fc["projected"]))

        col1, col2 = st.columns(2)
        by_cat = df_all.groupby("Categoria", as_index=False)["Valor"].sum().sort_values("Valor", ascending=False)
        col1.subheader("Para onde vai o dinheiro")
        col1.plotly_chart(px.pie(by_cat, names="Categoria", values="Valor", hole=.58, template="plotly_white"),
                          use_container_width=True)
        monthly = df_all.groupby(df_all["Data"].dt.to_period("M").astype(str))["Valor"].sum().rename_axis("Mês").reset_index()
        col2.subheader("Ritmo dos gastos")
        col2.plotly_chart(px.bar(monthly, x="Mês", y="Valor", template="plotly_white"), use_container_width=True)

        variation = category_variation(conn)
        if not variation.empty and "var_abs" in variation.columns:
            for cat in variation.head(3).index:
                delta = variation.loc[cat, "var_pct"]
                if abs(delta) > 5:
                    direction = "acima" if delta > 0 else "abaixo"
                    st.info(f"**{cat}** está {abs(delta):.1f}% {direction} do mês anterior.")

conn.close()