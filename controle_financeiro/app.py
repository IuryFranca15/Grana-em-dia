from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from financeiro import (
    CATEGORIES,
    DB_PATH,
    db_connection,
    extract_pdf,
    fingerprint,
    guess_category,
    load_transactions,
    normalize_table_rows,
    parse_invoice_text,
)

st.set_page_config(
    page_title="Grana em Dia | Controle Financeiro",
    page_icon="💸",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Visual leve, com cores quentes e cartões arredondados.
st.markdown(
    """
    <style>
    html, body, [class*="css"] { font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    .stApp { background: #F6F7F2; }
    [data-testid="stAppViewContainer"] > .main .block-container {
        max-width: 1320px; padding-top: 1.7rem; padding-bottom: 3rem;
    }
    [data-testid="stSidebar"] { background: #173A35; }
    [data-testid="stSidebar"] * { color: #F6F7F2; }
    [data-testid="stSidebar"] [data-testid="stRadio"] label { font-weight: 600; }
    [data-testid="stSidebar"] hr { border-color: #41635D; }
    .brand { font-size: 1.35rem; font-weight: 800; letter-spacing: -0.04em; color: #FFFFFF; }
    .brand-sub { color: #BBD0C8; font-size: .83rem; margin-top: -.3rem; }
    .hero {
        background: linear-gradient(115deg, #173A35 0%, #24584D 70%, #397564 100%);
        padding: 1.55rem 1.8rem; border-radius: 24px; color: #FFFFFF;
        margin-bottom: 1.3rem; box-shadow: 0 12px 30px rgba(23,58,53,.10);
    }
    .hero-kicker { color: #F9B78E; font-size: .76rem; font-weight: 800; letter-spacing: .12em; text-transform: uppercase; }
    .hero h1 { color: #FFFFFF; font-size: 2.05rem; margin: .35rem 0 .25rem 0; letter-spacing: -.045em; }
    .hero p { color: #DCEAE4; margin: 0; font-size: .98rem; }
    .section-kicker { color: #E47F54; font-size: .76rem; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }
    h2, h3 { color: #173A35; letter-spacing: -.025em; }
    div[data-testid="stMetric"] {
        background: #FFFFFF; border: 1px solid #E5EAE2; border-radius: 18px;
        padding: 1rem 1.1rem; box-shadow: 0 5px 18px rgba(23,58,53,.035);
    }
    div[data-testid="stMetricLabel"] { color: #64756D; font-weight: 600; }
    div[data-testid="stMetricValue"] { color: #173A35; font-weight: 800; }
    div[data-testid="stVerticalBlockBorderWrapper"] > div {
        border-radius: 18px; border-color: #E5EAE2; background: #FFFFFF;
    }
    .hint { color: #687970; font-size: .9rem; }
    .pill { display:inline-block; background:#FFF0E8; color:#A64F2B; padding:.28rem .65rem; border-radius:99px; font-size:.78rem; font-weight:700; }
    .stButton button[kind="primary"] { background: #E98259; border: 0; color: white; border-radius: 12px; font-weight: 700; }
    .stButton button { border-radius: 12px; font-weight: 600; }
    [data-testid="stFileUploader"] { background: #FFFFFF; border: 1px dashed #B8C9BE; border-radius: 16px; padding: .5rem; }
    [data-testid="stDataFrame"], [data-testid="stDataEditor"] { border-radius: 14px; }
    </style>
    """,
    unsafe_allow_html=True,
)


def brl(value):
    return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def hero(title, subtitle, kicker="SEU DINHEIRO, SEM DRAMA"):
    st.markdown(
        f'<div class="hero"><div class="hero-kicker">{kicker}</div>'
        f'<h1>{title}</h1><p>{subtitle}</p></div>',
        unsafe_allow_html=True,
    )


def store_preview(rows, diagnostics, source_name, card):
    st.session_state["finance_import_preview"] = {
        "rows": rows,
        "diagnostics": diagnostics,
        "source_name": source_name,
        "card": card,
    }


def render_import_preview(conn):
    preview = st.session_state.get("finance_import_preview")
    if not preview:
        return

    rows = preview["rows"]
    diagnostics = preview["diagnostics"]
    st.divider()
    st.subheader("Dá uma conferida antes de salvar 👀")

    if not rows:
        st.warning("Não encontrei compras com segurança nesse arquivo. Veja o texto extraído e tente novamente com outro formato.")
        st.caption("O leitor ignora saldo anterior, pagamentos e créditos porque eles não são novas despesas.")
        return

    df = pd.DataFrame(rows)
    df["Categoria"] = df["Descrição"].apply(lambda value: guess_category(value, conn))
    df["Data"] = pd.to_datetime(df["Data"]).dt.date

    m1, m2, m3 = st.columns(3)
    m1.metric("Lançamentos encontrados", len(df))
    m2.metric("Total identificado", brl(df["Valor"].sum()))
    m3.metric("Créditos/pagamentos ignorados", diagnostics.get("ignored_credits", 0))

    invoice_total = diagnostics.get("invoice_total")
    if invoice_total:
        diff = round(float(df["Valor"].sum()) - invoice_total, 2)
        if abs(diff) <= 0.05:
            st.success(f"Conferido: o total identificado bate com o total da fatura ({brl(invoice_total)}).")
        else:
            st.warning(
                f"O total identificado ({brl(df['Valor'].sum())}) difere do total da fatura "
                f"({brl(invoice_total)}) em {brl(abs(diff))}. Confira se falta ou sobra algum lançamento."
            )
    if diagnostics.get("unmatched_dates", 0):
        st.warning(
            f"{diagnostics['unmatched_dates']} data(s) não tiveram um valor associado. "
            "Esses trechos não foram incluídos; revise o arquivo se estiver faltando alguma compra."
        )
    if diagnostics.get("ignored_credits", 0):
        st.caption("Pagamentos, saldo anterior, créditos e estornos foram deixados de fora para não duplicar despesas.")

    edited = st.data_editor(
        df,
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        key=f"finance_import_editor_{hash(str(rows))}",
        column_config={
            "Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY", required=True),
            "Descrição": st.column_config.TextColumn("Estabelecimento / descrição", required=True),
            "Valor": st.column_config.NumberColumn("Valor", min_value=0.01, format="R$ %.2f", required=True),
            "Categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIES, required=True),
        },
    )

    st.caption("Você pode corrigir a descrição, o valor e a categoria diretamente na tabela. Nada é salvo até clicar no botão abaixo.")
    if st.button("Salvar lançamentos no meu histórico", type="primary", use_container_width=True):
        saved, duplicates, invalid = 0, 0, 0
        for _, row in edited.iterrows():
            try:
                parsed_date = pd.to_datetime(row["Data"]).date().isoformat()
                description = str(row["Descrição"]).strip()
                amount = abs(float(row["Valor"]))
                category = str(row["Categoria"])
                if not description or amount <= 0 or category not in CATEGORIES:
                    invalid += 1
                    continue
                fp = fingerprint(parsed_date, description, amount)
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO transactions
                       (fingerprint, transaction_date, description, amount, category, source_file, card)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (fp, parsed_date, description, amount, category, preview["source_name"], preview["card"]),
                )
                if cursor.rowcount:
                    saved += 1
                else:
                    duplicates += 1
            except Exception:
                invalid += 1
        conn.commit()
        st.session_state.pop("finance_import_preview", None)
        st.success(f"Pronto! {saved} lançamento(s) salvo(s), {duplicates} duplicidade(s) ignorada(s) e {invalid} linha(s) inválida(s).")
        st.rerun()


conn = db_connection()

with st.sidebar:
    st.markdown('<div class="brand">💸 Grana em Dia</div><div class="brand-sub">seu dinheiro no seu ritmo</div>', unsafe_allow_html=True)
    st.divider()
    page = st.radio(
        "MENU",
        ["🏡 Visão geral", "📥 Importar fatura", "🧾 Lançamentos", "🏷️ Categorias e regras", "🌱 Planejamento"],
        label_visibility="collapsed",
    )
    st.divider()
    st.caption("🔒 Seus dados ficam neste computador.")
    st.caption(f"Banco local: {DB_PATH.name}")


df_all = load_transactions(conn)

if page == "📥 Importar fatura":
    hero("Importar fatura", "Traga sua fatura, confira os lançamentos e deixe a organização com a gente.", "ENTRADA DE DADOS")
    st.info(
        "O importador reconhece compras e encargos. Saldo anterior, pagamento da fatura, créditos e estornos são ignorados para não contar o mesmo dinheiro duas vezes."
    )
    card = st.text_input("Identificação do cartão", placeholder="Ex.: Cartão principal • final 6825")

    tab_pdf, tab_text, tab_table = st.tabs(["📄 Arquivo PDF", "✍️ Texto copiado", "📊 CSV ou Excel"])

    with tab_pdf:
        uploaded_pdf = st.file_uploader("Selecione a fatura em PDF", type=["pdf"], key="invoice_pdf")
        if uploaded_pdf:
            if st.button("Ler PDF", type="primary", key="read_pdf"):
                try:
                    rows, diagnostics, raw_text = extract_pdf(uploaded_pdf.getvalue())
                    store_preview(rows, diagnostics, uploaded_pdf.name, card)
                    st.session_state["finance_raw_text"] = raw_text
                except Exception as error:
                    st.error(f"Não consegui abrir esse PDF: {error}")
        if st.session_state.get("finance_raw_text"):
            with st.expander("Ver texto extraído do PDF"):
                st.text_area("Texto reconhecido", st.session_state["finance_raw_text"], height=220, disabled=True)

    with tab_text:
        pasted_text = st.text_area(
            "Cole aqui o texto da fatura",
            height=230,
            placeholder="Cole o conteúdo copiado do PDF ou do aplicativo do banco...",
            key="invoice_pasted_text",
        )
        if st.button("Analisar texto", type="primary", key="analyze_text"):
            rows, diagnostics = parse_invoice_text(pasted_text)
            store_preview(rows, diagnostics, "Fatura colada", card)

    with tab_table:
        uploaded_table = st.file_uploader("Envie um arquivo CSV ou Excel", type=["csv", "xlsx"], key="invoice_table")
        if uploaded_table:
            try:
                if uploaded_table.name.lower().endswith(".xlsx"):
                    raw_df = pd.read_excel(uploaded_table)
                else:
                    try:
                        raw_df = pd.read_csv(uploaded_table, sep=None, engine="python", encoding="utf-8-sig")
                    except UnicodeDecodeError:
                        uploaded_table.seek(0)
                        raw_df = pd.read_csv(uploaded_table, sep=None, engine="python", encoding="latin-1")
                columns = list(raw_df.columns)
                if columns:
                    def find_default(candidates):
                        for column in columns:
                            normalized = str(column).strip().lower()
                            if any(candidate in normalized for candidate in candidates):
                                return column
                        return columns[0]

                    c1, c2, c3 = st.columns(3)
                    date_col = c1.selectbox("Coluna de data", columns, index=columns.index(find_default(["data", "date"])))
                    desc_col = c2.selectbox("Coluna de descrição", columns, index=columns.index(find_default(["descr", "estabelecimento", "merchant", "nome"])))
                    amount_col = c3.selectbox("Coluna de valor", columns, index=columns.index(find_default(["valor", "amount", "total"])))
                    if st.button("Analisar planilha", type="primary", key="analyze_table"):
                        rows, diagnostics = normalize_table_rows(raw_df, date_col, desc_col, amount_col)
                        store_preview(rows, diagnostics, uploaded_table.name, card)
            except Exception as error:
                st.error(f"Não consegui ler essa planilha: {error}")

    render_import_preview(conn)

elif page == "🧾 Lançamentos":
    hero("Seus lançamentos", "Tudo organizado em um só lugar. Filtre, confira e ajuste quando precisar.", "HISTÓRICO")
    if df_all.empty:
        st.info("Seu histórico ainda está vazio. Importe uma fatura para começar.")
    else:
        df_all["Data"] = pd.to_datetime(df_all["Data"])
        periods = sorted(df_all["Data"].dt.to_period("M").astype(str).unique(), reverse=True)
        f1, f2, f3 = st.columns([1, 1, 2])
        selected_period = f1.selectbox("Mês", ["Todos"] + periods)
        categories = ["Todas"] + sorted(df_all["Categoria"].dropna().unique().tolist())
        selected_category = f2.selectbox("Categoria", categories)
        search = f3.text_input("Buscar estabelecimento", placeholder="Ex.: Uber, farmácia, mercado...")

        filtered = df_all.copy()
        if selected_period != "Todos":
            filtered = filtered[filtered["Data"].dt.to_period("M").astype(str) == selected_period]
        if selected_category != "Todas":
            filtered = filtered[filtered["Categoria"] == selected_category]
        if search.strip():
            filtered = filtered[filtered["Descrição"].str.contains(search.strip(), case=False, na=False)]

        k1, k2 = st.columns(2)
        k1.metric("Total filtrado", brl(filtered["Valor"].sum()))
        k2.metric("Lançamentos", len(filtered))

        edited = st.data_editor(
            filtered.drop(columns=["id"]),
            use_container_width=True,
            hide_index=True,
            key="transactions_editor",
            column_config={
                "Data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                "Valor": st.column_config.NumberColumn("Valor", format="R$ %.2f"),
                "Categoria": st.column_config.SelectboxColumn("Categoria", options=CATEGORIES),
            },
            disabled=["Data", "Descrição", "Valor", "Arquivo", "Cartão"],
        )
        if st.button("Salvar alterações de categoria", type="primary"):
            for position, (_, row) in enumerate(edited.iterrows()):
                transaction_id = int(filtered.iloc[position]["id"])
                conn.execute("UPDATE transactions SET category=? WHERE id=?", (row["Categoria"], transaction_id))
            conn.commit()
            st.success("Categorias atualizadas!")
            st.rerun()

        st.download_button(
            "⬇️ Exportar histórico filtrado (CSV)",
            filtered.drop(columns=["id"]).to_csv(index=False).encode("utf-8-sig"),
            "meus_lancamentos.csv",
            "text/csv",
        )

        with st.expander("Adicionar uma despesa manualmente"):
            with st.form("manual_transaction"):
                c1, c2 = st.columns(2)
                manual_date = c1.date_input("Data", value=date.today())
                manual_amount = c2.number_input("Valor (R$)", min_value=0.01, step=1.0, format="%.2f")
                manual_desc = st.text_input("Descrição", placeholder="Ex.: almoço, mercado...")
                manual_category = st.selectbox("Categoria", CATEGORIES, index=CATEGORIES.index("Outros"))
                manual_submit = st.form_submit_button("Adicionar lançamento")
            if manual_submit:
                if manual_desc.strip():
                    dt = manual_date.isoformat()
                    fp = fingerprint(dt, manual_desc.strip(), manual_amount)
                    conn.execute(
                        "INSERT OR IGNORE INTO transactions (fingerprint, transaction_date, description, amount, category, source_file, card) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (fp, dt, manual_desc.strip(), manual_amount, manual_category, "Adicionado manualmente", ""),
                    )
                    conn.commit()
                    st.success("Despesa adicionada!")
                    st.rerun()
                else:
                    st.warning("Preencha uma descrição para continuar.")

        st.divider()
        if st.button("Excluir todo o histórico", type="secondary"):
            st.session_state["confirm_delete_all"] = True
        if st.session_state.get("confirm_delete_all"):
            st.warning("Tem certeza? Essa ação apaga todos os lançamentos salvos neste computador.")
            yes, no = st.columns(2)
            if yes.button("Sim, apagar tudo"):
                conn.execute("DELETE FROM transactions")
                conn.commit()
                st.session_state["confirm_delete_all"] = False
                st.rerun()
            if no.button("Cancelar"):
                st.session_state["confirm_delete_all"] = False
                st.rerun()

elif page == "🏷️ Categorias e regras":
    hero("Categorias e regras", "Ensine o app a reconhecer seus estabelecimentos. Ele vai aplicar as regras nas próximas importações.", "ORGANIZAÇÃO")
    st.subheader("Regras automáticas que já vêm prontas")
    st.caption("Incluí os estabelecimentos encontrados na fatura enviada, como IFD/iFood, Tocapub, Glacai, Uber, 99, Nelfarma, Caio Farma, Redepharma, Google e Amazon Prime.")
    with st.expander("Ver palavras-chave reconhecidas"):
        from financeiro import DEFAULT_PATTERNS
        for category, terms in DEFAULT_PATTERNS.items():
            st.markdown(f"**{category}:** " + ", ".join(terms))

    st.divider()
    st.subheader("Criar uma regra sua")
    with st.form("rule_form"):
        c1, c2 = st.columns([2, 1])
        keyword = c1.text_input("Palavra ou trecho do estabelecimento", placeholder="Ex.: MERCADO DO BAIRRO")
        category = c2.selectbox("Classificar como", CATEGORIES)
        submitted = st.form_submit_button("Adicionar regra", type="primary")
    if submitted:
        if keyword.strip():
            conn.execute(
                "INSERT OR REPLACE INTO category_rules (keyword, category) VALUES (?, ?)",
                (keyword.strip(), category),
            )
            conn.commit()
            st.success("Regra salva. Ela terá prioridade sobre a classificação automática.")
            st.rerun()
        else:
            st.warning("Digite uma palavra ou trecho para criar a regra.")

    rules = pd.read_sql_query("SELECT id, keyword AS Palavra, category AS Categoria FROM category_rules ORDER BY keyword", conn)
    if not rules.empty:
        st.subheader("Suas regras personalizadas")
        st.dataframe(rules.drop(columns=["id"]), use_container_width=True, hide_index=True)
        rule_to_delete = st.selectbox("Remover uma regra", rules["Palavra"].tolist())
        if st.button("Remover regra selecionada"):
            conn.execute("DELETE FROM category_rules WHERE keyword=?", (rule_to_delete,))
            conn.commit()
            st.success("Regra removida.")
            st.rerun()
    else:
        st.caption("Você ainda não criou regras personalizadas.")

elif page == "🌱 Planejamento":
    hero("Planejamento", "Entenda seu ritmo de gastos e simule pequenas mudanças sem complicação.", "OLHANDO PRA FRENTE")
    if df_all.empty:
        st.info("Importe algumas faturas para liberar as projeções e simulações.")
    else:
        df_all["Data"] = pd.to_datetime(df_all["Data"])
        monthly = df_all.groupby(df_all["Data"].dt.to_period("M"))["Valor"].sum().sort_index()
        current_period = pd.Timestamp.today().to_period("M")
        current = df_all[df_all["Data"].dt.to_period("M") == current_period]
        spent = current["Valor"].sum()
        days_elapsed = max(1, date.today().day)
        days_in_month = pd.Timestamp.today().days_in_month
        projected = spent / days_elapsed * days_in_month if spent else 0

        a, b, c = st.columns(3)
        a.metric("Gasto registrado neste mês", brl(spent))
        b.metric("Projeção até o fim do mês", brl(projected))
        c.metric("Média mensal do histórico", brl(monthly.mean()))
        st.caption("A projeção é uma estimativa linear baseada no ritmo de gastos até hoje. Não é uma previsão garantida.")

        st.subheader("Simular economia")
        category_options = sorted(df_all["Categoria"].dropna().unique().tolist())
        s1, s2 = st.columns([1, 2])
        selected = s1.selectbox("Categoria", category_options)
        reduction = s2.slider("Quanto você gostaria de reduzir?", 0, 80, 15, 5, format="%d%%")
        category_monthly = df_all[df_all["Categoria"] == selected].groupby(
            df_all.loc[df_all["Categoria"] == selected, "Data"].dt.to_period("M")
        )["Valor"].sum()
        average = category_monthly.mean() if not category_monthly.empty else 0
        saving = average * reduction / 100
        r1, r2 = st.columns(2)
        r1.metric("Economia mensal estimada", brl(saving))
        r2.metric("Em 12 meses", brl(saving * 12))

        st.subheader("Evolução dos gastos")
        monthly_df = monthly.rename_axis("Mês").reset_index(name="Valor")
        monthly_df["Mês"] = monthly_df["Mês"].astype(str)
        st.plotly_chart(
            px.line(monthly_df, x="Mês", y="Valor", markers=True, template="plotly_white", title="Total registrado por mês"),
            use_container_width=True,
        )

else:
    hero("Seu dinheiro, mais organizado.", "Um painel simples para acompanhar para onde a grana está indo.", "BEM-VINDO AO GRANA EM DIA")
    if df_all.empty:
        st.markdown("### Vamos começar pelo primeiro passo? 👋")
        st.write("Importe uma fatura e o app vai separar as despesas por categoria, montar gráficos e criar seu histórico.")
        st.markdown(
            "<div class='pill'>PDF • Texto copiado • CSV • Excel</div>",
            unsafe_allow_html=True,
        )
        st.markdown("\n")
        st.info("Dica: você pode revisar e corrigir cada lançamento antes de salvar. O app não envia seus dados bancários para a internet.")
    else:
        df_all["Data"] = pd.to_datetime(df_all["Data"])
        total = df_all["Valor"].sum()
        current_month = pd.Timestamp.today().to_period("M")
        this_month = df_all[df_all["Data"].dt.to_period("M") == current_month]["Valor"].sum()
        month_count = max(1, df_all["Data"].dt.to_period("M").nunique())
        average = total / month_count

        c1, c2, c3 = st.columns(3)
        c1.metric("Total no histórico", brl(total))
        c2.metric("Gastos neste mês", brl(this_month))
        c3.metric("Média por mês", brl(average))

        left, right = st.columns([1, 1.25])
        by_category = df_all.groupby("Categoria", as_index=False)["Valor"].sum().sort_values("Valor", ascending=False)
        left.subheader("Para onde vai seu dinheiro")
        left.plotly_chart(
            px.pie(by_category, names="Categoria", values="Valor", hole=.58, template="plotly_white"),
            use_container_width=True,
        )

        monthly = (
            df_all.groupby(df_all["Data"].dt.to_period("M").astype(str))["Valor"]
            .sum()
            .rename_axis("Mês")
            .reset_index()
        )
        right.subheader("Ritmo dos gastos")
        right.plotly_chart(
            px.bar(monthly, x="Mês", y="Valor", template="plotly_white"),
            use_container_width=True,
        )

        st.subheader("Últimos lançamentos")
        recent = df_all.sort_values("Data", ascending=False).head(8).copy()
        recent["Data"] = recent["Data"].dt.strftime("%d/%m/%Y")
        st.dataframe(recent.drop(columns=["id"]), use_container_width=True, hide_index=True)

        if len(monthly) >= 2:
            totals = df_all.groupby(df_all["Data"].dt.to_period("M"))["Valor"].sum().sort_index()
            previous, latest = totals.iloc[-2], totals.iloc[-1]
            delta = (latest / previous - 1) * 100 if previous else 0
            st.caption(f"No último mês registrado, os gastos ficaram {abs(delta):.1f}% {'acima' if delta > 0 else 'abaixo' if delta < 0 else 'iguais'} ao mês anterior.")

conn.close()
