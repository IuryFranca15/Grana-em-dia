import io
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from financeiro import (
    CATEGORIES, DB_PATH, SUBCATEGORIES, add_manual_transaction, available_months,
    category_variation, db_connection, delete_transactions, detect_recurrences,
    extract_pdf, file_checksum, forecast_month, get_budgets, guess_category,
    installment_commitments, learn_from_correction, load_transactions,
    normalize_table_rows, outliers, parse_invoice_text, pending_review,
    reclassify_pending, register_import, save_transactions, set_budget, shift_month,
    simulate_purchase, update_transaction, valid_subcategory,
)

PRIMARY = "#5B3FD6"
PRIMARY_SOFT = "#B9ACEF"
INK = "#1E1A33"
ALL_SUBCATEGORIES = [sub for subs in SUBCATEGORIES.values() for sub in subs]
MONTH_NAMES = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
               "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

st.set_page_config(page_title="Grana em Dia | Radar", page_icon="💸", layout="wide")

# as cores gerais ficam em .streamlit/config.toml; aqui só o que o tema não cobre
st.markdown("""<style>
    .block-container { max-width: 1320px; padding-top: 3.5rem; }
    .hero { background: linear-gradient(120deg, #1E1746 0%, #4A32B8 62%, #8B5CF6 100%);
        padding: 1.5rem 1.8rem; border-radius: 22px; margin-bottom: 1.2rem;
        border-bottom: 4px solid #FF8A5B; }
    .hero h1 { color: #fff; font-size: 1.9rem; margin: 0; padding: .25rem 0; }
    .hero p { color: #E4DEFF; margin: 0; }
    .hero-kicker { color: #FFB89C; font-size: .75rem; font-weight: 800; letter-spacing: .12em; }
    div[data-testid="stMetric"] { background: #fff; border: 1px solid #DDD8F0;
        border-radius: 16px; padding: .9rem 1rem; box-shadow: 0 2px 10px rgba(30,23,70,.05); }
</style>""", unsafe_allow_html=True)


def brl(value):
    text = f"{abs(float(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if float(value) < 0 else ''}R$ {text}"


def md_brl(value):
    """Em textos markdown, dois "$" na mesma frase viram fórmula: precisa escapar."""
    return brl(value).replace("$", "\\$")


def month_label(month):
    year, number = month.split("-")
    return f"{MONTH_NAMES[int(number) - 1]}/{year}"


def hero(title, subtitle, kicker="GRANA EM DIA"):
    st.markdown(f'<div class="hero"><div class="hero-kicker">{kicker}</div>'
                f'<h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


def flash(message):
    """Mensagem que sobrevive ao st.rerun()."""
    st.session_state["flash"] = message
    st.rerun()


def style_chart(fig, height=340):
    fig.update_layout(
        height=height, separators=",.", showlegend=False, font=dict(color=INK),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=8, r=8, t=24, b=8), xaxis_title=None, yaxis_title=None,
    )
    return fig


def pick_month(label="Mês"):
    months = available_months(conn)
    return st.selectbox(label, months, format_func=month_label, key=f"month_{label}")


def budget_bar(label, spent, limit):
    share = spent / limit
    icon = "🔴" if share > 1 else "🟠" if share >= 0.8 else "🟢"
    st.progress(min(share, 1.0),
                text=f"{icon} {label}: {md_brl(spent)} de {md_brl(limit)} ({share:.0%})")


def month_comparison(month):
    """Variação contra o mês anterior; no mês corrente compara até o dia de hoje."""
    today = date.today()
    day = today.day if month == today.strftime("%Y-%m") else None
    note = f"Comparação com {month_label(shift_month(month, -1))}"
    if day:
        note += f", considerando só até o dia {day} nos dois meses"
    return category_variation(conn, month, day), note + "."


# --- Visão geral -----------------------------------------------------------
def page_overview():
    hero("Seu dinheiro, mais organizado", "O mês em um olhar: total, mudanças e para onde foi.")
    if df_all.empty:
        st.info("Importe uma fatura para começar.")
        return
    month = pick_month()
    months = df_all["Data"].dt.strftime("%Y-%m")
    current = df_all[months == month]
    previous = df_all[months == shift_month(month, -1)]
    is_current = month == date.today().strftime("%Y-%m")
    total = current["Valor"].sum()
    by_cat = current.groupby("Categoria")["Valor"].sum().sort_values()

    c1, c2, c3, c4 = st.columns(4)
    delta = None
    if not previous.empty and not is_current:
        delta = f"{brl(total - previous['Valor'].sum())} vs. mês anterior"
    c1.metric("Total do mês", brl(total), delta=delta, delta_color="inverse")
    c2.metric("Lançamentos", len(current))
    c3.metric("Maior categoria", by_cat.index[-1], help=brl(by_cat.iloc[-1]))
    if is_current:
        c4.metric("Previsão de fechamento", brl(forecast_month(conn)["projected"]))
    else:
        c4.metric("Ticket médio", brl(current["Valor"].mean()))
    if date.today().strftime("%Y-%m") not in set(months):
        st.caption(f"Ainda não há lançamentos em {month_label(date.today().strftime('%Y-%m'))}.")

    budgets = get_budgets(conn, month)
    if None in budgets:
        budget_bar("Orçamento do mês", total, budgets[None])
    waiting = len(pending_review(conn))
    if waiting:
        st.warning(f"{waiting} lançamento(s) sem categoria. Revise na página Lançamentos.")

    left, right = st.columns(2)
    left.subheader("Para onde foi o dinheiro")
    fig = px.bar(x=by_cat.values, y=by_cat.index, orientation="h",
                 text=[brl(v) for v in by_cat.values])
    fig.update_traces(marker_color=PRIMARY, textposition="outside", cliponaxis=False,
                      hovertemplate="%{y}: %{text}<extra></extra>")
    fig.update_xaxes(visible=False, range=[0, by_cat.max() * 1.3])
    left.plotly_chart(style_chart(fig))

    right.subheader("Evolução mensal")
    monthly = df_all.groupby(months)["Valor"].sum().tail(12)
    fig = px.bar(x=[month_label(m) for m in monthly.index], y=monthly.values,
                 text=[brl(v) for v in monthly.values])
    fig.update_traces(marker_color=[PRIMARY if m == month else PRIMARY_SOFT for m in monthly.index],
                      textposition="outside", cliponaxis=False,
                      hovertemplate="%{x}: %{text}<extra></extra>")
    fig.update_yaxes(visible=False, range=[0, monthly.max() * 1.18])
    fig.update_xaxes(type="category")
    right.plotly_chart(style_chart(fig))

    st.subheader("Maiores gastos do mês")
    st.dataframe(
        current.nlargest(5, "Valor")[["Data", "Descrição", "Categoria", "Valor"]],
        hide_index=True,
        column_config={"Data": st.column_config.DateColumn(format="DD/MM/YYYY"),
                       "Valor": st.column_config.NumberColumn(format="R$ %.2f")})

    variation, note = month_comparison(month)
    if not variation.empty:
        st.subheader("O que mudou")
        st.caption(note)
        for category, row in variation.head(3).iterrows():
            if abs(row["var_abs"]) < 1:
                continue
            direction = "a mais" if row["var_abs"] > 0 else "a menos"
            percent = "categoria nova" if pd.isna(row["var_pct"]) else f"{row['var_pct']:+.0f}%"
            st.info(f"**{category}**: {md_brl(abs(row['var_abs']))} {direction} ({percent}).")


# --- Importar --------------------------------------------------------------
def page_import():
    hero("Importar fatura", "Confira antes de salvar. Lançamentos já importados são ignorados.")
    card = st.text_input("Cartão", placeholder="Ex.: Cartão principal • final 6825")
    tab_pdf, tab_text, tab_table = st.tabs(["📄 PDF", "✍️ Texto", "📊 CSV/Excel"])

    def handle_preview(rows, diagnostics, source_name, checksum):
        if not rows:
            st.session_state.pop("preview", None)
            st.warning("Nenhuma compra reconhecida. Confira o formato ou tente outra aba.")
            return
        st.session_state["preview"] = {"rows": rows, "diagnostics": diagnostics,
                                       "source": source_name, "checksum": checksum}

    with tab_pdf:
        pdf_file = st.file_uploader("Fatura PDF", type=["pdf"])
        if pdf_file and st.button("Ler PDF", type="primary"):
            try:
                content = pdf_file.getvalue()
                with st.spinner("Lendo a fatura..."):
                    rows, diag, _ = extract_pdf(content)
                handle_preview(rows, diag, pdf_file.name, file_checksum(content))
            except Exception as error:
                st.error(f"Erro ao abrir PDF: {error}")

    with tab_text:
        text = st.text_area("Cole o texto da fatura", height=220)
        if st.button("Analisar texto", type="primary"):
            rows, diag = parse_invoice_text(text)
            handle_preview(rows, diag, "Fatura colada", file_checksum(text.encode()))

    with tab_table:
        table_file = st.file_uploader("CSV/Excel", type=["csv", "xlsx"])
        if table_file:
            try:
                if table_file.name.lower().endswith(".xlsx"):
                    raw = pd.read_excel(table_file)
                else:
                    content = table_file.getvalue()
                    try:
                        decoded = content.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        decoded = content.decode("latin-1")
                    raw = pd.read_csv(io.StringIO(decoded), sep=None,
                                      engine="python", dtype=str)
                cols = list(raw.columns)
                c1, c2, c3 = st.columns(3)
                date_col = c1.selectbox("Data", cols, 0)
                desc_col = c2.selectbox("Descrição", cols, min(1, len(cols) - 1))
                value_col = c3.selectbox("Valor", cols, len(cols) - 1)
                if st.button("Analisar planilha", type="primary"):
                    rows, diag = normalize_table_rows(raw, date_col, desc_col, value_col)
                    handle_preview(rows, diag, table_file.name,
                                   file_checksum(table_file.getvalue()))
            except Exception as error:
                st.error(f"Erro na planilha: {error}")

    preview = st.session_state.get("preview")
    if not preview:
        return
    st.divider()
    st.subheader("Confira antes de salvar 👀")
    st.caption("Ao trocar uma categoria aqui, o app aprende e repete nas próximas importações.")
    df = pd.DataFrame(preview["rows"])
    guesses = [guess_category(d, conn) for d in df["Descrição"]]
    df["Categoria"] = [g[0] for g in guesses]
    df["Subcategoria"] = pd.Series([g[1] for g in guesses], dtype=object)
    df["Data"] = pd.to_datetime(df["Data"]).dt.date

    edited = st.data_editor(
        df, hide_index=True, num_rows="dynamic",
        column_order=["Data", "Descrição", "Valor", "Categoria", "Subcategoria"],
        column_config={
            "Data": st.column_config.DateColumn(format="DD/MM/YYYY", required=True),
            "Descrição": st.column_config.TextColumn(required=True),
            "Valor": st.column_config.NumberColumn(format="R$ %.2f", min_value=0.01, required=True),
            "Categoria": st.column_config.SelectboxColumn(options=CATEGORIES),
            "Subcategoria": st.column_config.SelectboxColumn(options=ALL_SUBCATEGORIES),
        },
        key=f"preview_{preview['checksum']}",
    )

    diagnostics = preview["diagnostics"]
    total = pd.to_numeric(edited["Valor"], errors="coerce").sum()
    m1, m2, m3 = st.columns(3)
    m1.metric("Lançamentos", len(edited))
    m2.metric("Total", brl(total))
    m3.metric("Ignorados (pagamentos/créditos)", diagnostics.get("ignored_credits", 0))
    if diagnostics.get("invoice_total"):
        difference = total - diagnostics["invoice_total"]
        if abs(difference) <= 0.05:
            st.success("O total bate com a fatura.")
        else:
            st.warning(f"Diferença de {md_brl(difference)} em relação ao total da fatura "
                       f"({md_brl(diagnostics['invoice_total'])}).")
    if diagnostics.get("repeated"):
        st.warning(f"{diagnostics['repeated']} linha(s) idêntica(s) a outra. Se não forem "
                   "compras repetidas de verdade, apague a linha na tabela.")
    if diagnostics.get("unmatched_dates"):
        st.warning(f"{diagnostics['unmatched_dates']} linha(s) com data não puderam ser lidas.")

    save_col, cancel_col = st.columns([4, 1])
    if cancel_col.button("Descartar", width="stretch"):
        st.session_state.pop("preview", None)
        st.rerun()
    if save_col.button("Salvar lançamentos", type="primary", width="stretch"):
        import_id, again = register_import(
            conn, preview["source"], "importador", preview["checksum"])
        saved, duplicates, invalid = save_transactions(
            conn, edited.to_dict("records"), preview["source"], card, import_id)
        st.session_state.pop("preview", None)
        message = f"{saved} salvos, {duplicates} já existiam, {invalid} inválidos."
        if again:
            message += " Este arquivo já tinha sido importado."
        flash(message)


# --- Lançamentos -----------------------------------------------------------
def category_columns():
    return {
        "Data": st.column_config.DateColumn(format="DD/MM/YYYY"),
        "Valor": st.column_config.NumberColumn(format="R$ %.2f"),
        "Categoria": st.column_config.SelectboxColumn(options=CATEGORIES, required=True),
        "Subcategoria": st.column_config.SelectboxColumn(options=ALL_SUBCATEGORIES),
    }


def text_or_none(value):
    return value if isinstance(value, str) and value else None


def apply_edits(original, edited, confirm_column=None):
    """Grava só o que mudou (ou foi confirmado) e aprende com troca de categoria."""
    changed = learned = 0
    for position, (_, row) in enumerate(edited.iterrows()):
        before = original.iloc[position]
        category = row["Categoria"]
        sub = valid_subcategory(category, text_or_none(row.get("Subcategoria")))
        different = (category != before["Categoria"]
                     or sub != text_or_none(before["Subcategoria"]))
        if not different and not (confirm_column and row[confirm_column]):
            continue
        update_transaction(conn, before["id"], category, sub)
        changed += 1
        if different and category != "Outros":
            learned += learn_from_correction(conn, before["Descrição"], category, sub)
    return changed, learned


def page_transactions():
    hero("Seus lançamentos", "Revise, corrija, adicione e exporte.")

    with st.expander("➕ Adicionar lançamento manual (Pix, dinheiro...)"):
        with st.form("manual", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns([1, 2, 1, 1])
            day = c1.date_input("Data", date.today(), format="DD/MM/YYYY")
            description = c2.text_input("Descrição")
            amount = c3.number_input("Valor (R$)", min_value=0.0, step=10.0, format="%.2f")
            category = c4.selectbox("Categoria", ["Automática"] + CATEGORIES)
            if st.form_submit_button("Adicionar", type="primary"):
                if add_manual_transaction(conn, day, description, amount,
                                          None if category == "Automática" else category):
                    flash("Lançamento adicionado.")
                st.error("Informe uma descrição e um valor maior que zero.")

    if df_all.empty:
        st.info("Sem lançamentos. Importe uma fatura ou adicione um acima.")
        return

    review = pending_review(conn)
    if not review.empty:
        st.subheader(f"🔍 Fila de revisão ({len(review)})")
        st.caption("Escolha a categoria. Para manter em “Outros”, marque Confirmar. "
                   "Cada correção vira uma regra.")
        review["Data"] = pd.to_datetime(review["Data"])
        review["Subcategoria"] = review["Subcategoria"].astype(object)
        review["Confirmar"] = False
        edited = st.data_editor(
            review, hide_index=True, column_config=category_columns(),
            column_order=["Data", "Descrição", "Valor", "Categoria", "Subcategoria", "Confirmar"],
            disabled=["Data", "Descrição", "Valor"],
            key=f"review_{hash(tuple(review['id']))}",
        )
        if st.button("Aplicar correções e aprender", type="primary"):
            changed, learned = apply_edits(review, edited, "Confirmar")
            flash(f"{changed} revisado(s); {learned} parecido(s) classificado(s) automaticamente.")
        st.divider()

    st.subheader("Todos os lançamentos")
    f1, f2, f3 = st.columns([1, 1, 2])
    months = df_all["Data"].dt.strftime("%Y-%m")
    sel_month = f1.selectbox("Mês", ["Todos"] + available_months(conn),
                             format_func=lambda m: m if m == "Todos" else month_label(m))
    sel_category = f2.selectbox("Categoria", ["Todas"] + CATEGORIES)
    search = f3.text_input("Buscar", placeholder="Uber, farmácia...")
    mask = pd.Series(True, index=df_all.index)
    if sel_month != "Todos":
        mask &= months == sel_month
    if sel_category != "Todas":
        mask &= df_all["Categoria"] == sel_category
    if search.strip():
        mask &= df_all["Descrição"].str.contains(search.strip(), case=False, na=False, regex=False)
    filtered = df_all[mask].reset_index(drop=True)

    k1, k2 = st.columns(2)
    k1.metric("Total filtrado", brl(filtered["Valor"].sum()))
    k2.metric("Lançamentos", len(filtered))
    if filtered.empty:
        st.caption("Nada encontrado com esses filtros.")
        return

    table = filtered.copy()
    table["Subcategoria"] = table["Subcategoria"].astype(object)
    table["Excluir"] = False
    edited = st.data_editor(
        table, hide_index=True, column_config=category_columns(),
        column_order=["Data", "Descrição", "Valor", "Categoria", "Subcategoria",
                      "Cartão", "Arquivo", "Excluir"],
        disabled=["Data", "Descrição", "Valor", "Arquivo", "Cartão"],
        # a chave muda com o filtro para a edição nunca cair na linha errada
        key=f"transactions_{hash(tuple(filtered['id']))}",
    )
    to_delete = edited.loc[edited["Excluir"], "id"].tolist()
    confirmed = True
    if to_delete:
        confirmed = st.checkbox(f"Confirmo a exclusão de {len(to_delete)} lançamento(s)")
    b1, b2 = st.columns([1, 3])
    if b1.button("Salvar alterações", type="primary", disabled=not confirmed):
        keep = ~edited["Excluir"]
        changed, _ = apply_edits(filtered[keep], edited[keep])
        delete_transactions(conn, to_delete)
        flash(f"{changed} atualizado(s), {len(to_delete)} excluído(s).")
    export = filtered.drop(columns=["id"]).assign(Data=filtered["Data"].dt.strftime("%d/%m/%Y"))
    b2.download_button("⬇️ Exportar CSV",
                       export.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                       "lancamentos.csv", "text/csv")


# --- Categorias e regras ---------------------------------------------------
def page_rules():
    hero("Categorias e regras", "Ensine o app uma vez; ele repete nas próximas faturas.")
    st.subheader("Nova regra")
    c1, c2, c3, c4 = st.columns([2, 2, 2, 1])
    keyword = c1.text_input("Palavra ou trecho da descrição")
    category = c2.selectbox("Categoria", CATEGORIES)
    sub = c3.selectbox("Subcategoria (opcional)", [""] + SUBCATEGORIES.get(category, []))
    priority = c4.number_input("Prioridade", 0, 100, 10)
    if st.button("Adicionar regra", type="primary"):
        if not keyword.strip():
            st.error("Informe a palavra ou trecho.")
        else:
            conn.execute(
                "INSERT OR REPLACE INTO category_rules "
                "(keyword, category, subcategory, priority, active, source) "
                "VALUES (?, ?, ?, ?, 1, 'manual')",
                (keyword.strip().upper(), category, sub or None, priority))
            conn.commit()
            changed = reclassify_pending(conn)
            flash(f"Regra salva. {changed} lançamento(s) pendente(s) classificado(s).")

    st.divider()
    st.subheader("Regras cadastradas")
    rules = pd.read_sql_query(
        "SELECT id, keyword AS Palavra, category AS Categoria, subcategory AS Subcategoria, "
        "priority AS Prioridade, active AS Ativa, source AS Origem FROM category_rules "
        "ORDER BY priority DESC, keyword", conn)
    if rules.empty:
        st.caption("Nenhuma regra personalizada ainda.")
        return
    rules["Ativa"] = rules["Ativa"].astype(bool)
    rules["Origem"] = rules["Origem"].map({"manual": "Manual", "correction": "Aprendida"})
    st.dataframe(rules, hide_index=True,
                 column_order=["Palavra", "Categoria", "Subcategoria",
                               "Prioridade", "Ativa", "Origem"])
    names = dict(zip(rules["id"], rules["Palavra"]))
    rule_id = st.selectbox("Regra selecionada", list(names), format_func=names.get)
    c1, c2 = st.columns(2)
    if c1.button("Ativar / desativar", width="stretch"):
        conn.execute("UPDATE category_rules SET active = 1 - active WHERE id=?", (int(rule_id),))
        conn.commit()
        flash("Regra atualizada.")
    if c2.button("Excluir regra", width="stretch"):
        conn.execute("DELETE FROM category_rules WHERE id=?", (int(rule_id),))
        conn.commit()
        flash("Regra excluída.")


# --- Radar -----------------------------------------------------------------
def page_radar():
    hero("Radar", "O que mudou, o que se repete e o que está fora do padrão.", "ANÁLISE")
    if df_all.empty:
        st.info("Importe dados para ativar o radar.")
        return

    st.subheader("Variação por categoria")
    month = pick_month("Mês analisado")
    variation, note = month_comparison(month)
    if variation.empty:
        st.caption("Não há dados do mês anterior para comparar.")
    else:
        st.caption(note)
        table = variation.iloc[:, :2].copy()
        table.columns = [month_label(c.strftime("%Y-%m")) for c in table.columns]
        table["Variação R$"] = variation["var_abs"]
        table["Variação %"] = variation["var_pct"]
        money = st.column_config.NumberColumn(format="R$ %.2f")
        st.dataframe(
            table.reset_index(), hide_index=True,
            column_config={**{c: money for c in table.columns[:3]},
                           "Variação %": st.column_config.NumberColumn(format="%.1f%%")})

    st.subheader("Cobranças recorrentes")
    recurrences = detect_recurrences(conn)
    if recurrences.empty:
        st.caption("Nenhuma ainda. São necessárias 3 cobranças de valor parecido, "
                   "em intervalos regulares.")
    else:
        st.dataframe(
            recurrences.assign(confidence=recurrences["confidence"] * 100,
                               last_seen=pd.to_datetime(recurrences["last_seen"])),
            hide_index=True,
            column_config={
                "merchant": "Estabelecimento",
                "expected_amount": st.column_config.NumberColumn("Valor esperado", format="R$ %.2f"),
                "frequency": "Frequência",
                "last_seen": st.column_config.DateColumn("Última", format="DD/MM/YYYY"),
                "confidence": st.column_config.NumberColumn("Confiança", format="%.0f%%"),
                "occurrences": "Ocorrências",
            })

    st.subheader("Despesas fora do padrão")
    unusual = outliers(conn)
    if unusual.empty:
        st.caption("Nenhum lançamento muito acima do habitual.")
    else:
        st.caption("Valores bem acima do típico (mediana) da categoria, em todo o histórico.")
        st.dataframe(
            unusual.assign(transaction_date=pd.to_datetime(unusual["transaction_date"])),
            hide_index=True,
            column_order=["transaction_date", "description", "category", "amount", "typical"],
            column_config={
                "transaction_date": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "description": "Descrição", "category": "Categoria",
                "amount": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
                "typical": st.column_config.NumberColumn("Típico", format="R$ %.2f"),
            })


# --- Planejamento ----------------------------------------------------------
def page_planning():
    hero("Planejamento", "Previsão do mês, orçamento, parcelas e simulador.", "PRÓXIMOS PASSOS")
    this_month = date.today().strftime("%Y-%m")

    if df_all.empty:
        st.info("Importe dados para ver previsões.")
    else:
        fc = forecast_month(conn)
        st.subheader(f"Previsão de {month_label(this_month)}")
        if not fc["has_current"]:
            last = date.fromisoformat(fc["last_date"]).strftime("%d/%m/%Y")
            st.warning(f"Não há lançamentos neste mês (o último é de {last}). "
                       "Importe a fatura mais recente para uma previsão melhor.")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Já lançado", brl(fc["spent"]))
        c2.metric("Previsão de fechamento", brl(fc["projected"]))
        c3.metric("Cenário baixo", brl(fc["low"]))
        c4.metric("Cenário alto", brl(fc["high"]))
        st.caption(f"Falta cair: gastos variáveis {md_brl(fc['remaining_variable'])} · "
                   f"recorrências {md_brl(fc['pending_recurrences'])} · "
                   f"parcelas {md_brl(fc['installment_estimate'])}")
        st.caption(f"{fc['assumptions']} Dia {fc['days_elapsed']} de {fc['days_in_month']}; "
                   f"{fc['history_months']} mês(es) de histórico.")

    st.divider()
    st.subheader("Orçamento")
    budgets = get_budgets(conn, this_month)
    current = df_all[df_all["Data"].dt.strftime("%Y-%m") == this_month]
    spent_by_cat = current.groupby("Categoria")["Valor"].sum()
    if None in budgets:
        budget_bar("Total", current["Valor"].sum(), budgets[None])
    for name in CATEGORIES:
        if name in budgets:
            budget_bar(name, spent_by_cat.get(name, 0.0), budgets[name])
    with st.expander("Definir limites mensais", expanded=not budgets):
        st.caption("Deixe 0 para não ter limite. O valor vale deste mês em diante.")
        names = ["Total do mês"] + CATEGORIES
        table = pd.DataFrame({
            "Categoria": names,
            "Limite": [float(budgets.get(None if i == 0 else n, 0.0)) for i, n in enumerate(names)],
        })
        edited = st.data_editor(
            table, hide_index=True, disabled=["Categoria"], key="budget_editor",
            column_config={"Limite": st.column_config.NumberColumn(format="R$ %.2f", min_value=0.0)})
        if st.button("Salvar orçamento", type="primary"):
            for i, row in edited.iterrows():
                value = 0.0 if pd.isna(row["Limite"]) else float(row["Limite"])
                if value != table.at[i, "Limite"]:
                    set_budget(conn, this_month, value, None if i == 0 else row["Categoria"])
            flash("Orçamento salvo.")

    commitments = installment_commitments(conn)
    commitments = commitments[commitments["month"] >= this_month]
    if not commitments.empty:
        st.divider()
        st.subheader("Parcelas que ainda vão cair")
        st.caption("Estimativa a partir de “PARC x/y”, considerando a data da compra original.")
        totals = commitments.groupby("month")["amount"].sum()
        fig = px.bar(x=[month_label(m) for m in totals.index], y=totals.values,
                     text=[brl(v) for v in totals.values])
        fig.update_traces(marker_color=PRIMARY, textposition="outside", cliponaxis=False,
                          hovertemplate="%{x}: %{text}<extra></extra>")
        fig.update_yaxes(visible=False, range=[0, totals.max() * 1.2])
        fig.update_xaxes(type="category")
        st.plotly_chart(style_chart(fig, 260))
        st.dataframe(
            commitments.sort_values("month").assign(month=lambda d: d["month"].map(month_label)),
            hide_index=True,
            column_config={
                "month": "Mês", "description": "Compra", "installment": "Parcela",
                "amount": st.column_config.NumberColumn("Valor", format="R$ %.2f")})

    st.divider()
    st.subheader("Simulador de compra")
    c1, c2, c3 = st.columns(3)
    amount = c1.number_input("Valor da compra (R$)", min_value=1.0, step=10.0, format="%.2f")
    parts = c2.number_input("Parcelas", 1, 24, 1)
    category = c3.selectbox("Categoria", CATEGORIES)
    if st.button("Simular", type="primary"):
        sim = simulate_purchase(conn, amount, category, parts)
        a, b, c = st.columns(3)
        a.metric("Previsão atual", brl(sim["before"]))
        b.metric("Com a compra", brl(sim["after"]), delta=f"+{brl(sim['delta'])}",
                 delta_color="inverse")
        c.metric("Cenário alto", brl(sim["forecast_high"]))
        if parts > 1:
            st.info(f"Este mês pesa {md_brl(sim['delta'])}; restam {parts - 1} parcela(s).")
        if sim["over_budget"]:
            st.error(f"Estoura o orçamento total (limite: {md_brl(sim['budget_limit'])}).")
        elif sim["budget_limit"]:
            st.success(f"Dentro do orçamento total (limite: {md_brl(sim['budget_limit'])}).")
        if sim["over_category"]:
            st.error(f"{category} iria a {md_brl(sim['category_after'])}, acima do limite de "
                     f"{md_brl(sim['category_limit'])}.")
        if not sim["budget_limit"] and not sim["category_limit"]:
            st.caption("Defina um orçamento acima para saber se a compra cabe.")


PAGES = {
    "🏡 Visão geral": page_overview,
    "📥 Importar fatura": page_import,
    "🧾 Lançamentos": page_transactions,
    "🏷️ Categorias e regras": page_rules,
    "📡 Radar": page_radar,
    "🌱 Planejamento": page_planning,
}

conn = db_connection()
with st.sidebar:
    st.markdown("## 💸 Grana em Dia")
    st.caption("Radar de gastos")
    st.divider()
    page = st.radio("Menu", list(PAGES), label_visibility="collapsed")
    st.divider()
    st.caption(f"Banco: {DB_PATH.name} · dados só neste computador")

df_all = load_transactions(conn)
df_all["Data"] = pd.to_datetime(df_all["Data"])
if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))
try:
    PAGES[page]()
finally:
    conn.close()
