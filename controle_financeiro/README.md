# Grana em Dia — Radar Inteligente de Gastos

Aplicação local em Python + Streamlit para importar faturas, classificar
despesas, diagnosticar variações, prever o mês e simular compras.

## Novidades da versão Radar

- Subcategorias (ex.: Alimentação → Mercado/Restaurante)
- Fila de revisão com aprendizado por correção (vira regra reutilizável)
- Diagnóstico: variação por categoria, recorrências, outliers
- Previsão de fechamento com faixa de incerteza e premissas explícitas
- Simulador de compra com efeito no orçamento
- Deduplicação por checksum de arquivo + fingerprint de lançamento
- Migração automática do banco antigo (sem perder dados)

## Instalação

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py