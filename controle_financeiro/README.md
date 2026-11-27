# Grana em Dia — Radar Inteligente de Gastos

Aplicação local em Python + Streamlit para importar faturas, classificar
despesas, diagnosticar variações, prever o mês e simular compras.

## Instalação (Windows)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -U -r requirements.txt
streamlit run app.py
```

Rode os comandos dentro da pasta do projeto, para o tema em
`.streamlit/config.toml` ser aplicado.

## Seus dados

- Ficam em `data/financeiro.db` (ou no caminho da variável `FINANCEIRO_DB`).
- Na primeira abertura de um banco antigo, é criada uma cópia
  `financeiro.backup-AAAAMMDD-HHMMSS.db` ao lado dele antes da atualização.
- O app só aceita conexões do próprio computador (`address = "localhost"`).

## Recursos

- Importação de PDF, texto e CSV/Excel (Banco do Brasil e Nubank)
- Lançamento manual e exclusão
- Subcategorias, fila de revisão e aprendizado por correção
- Visão por mês, variação, recorrências e despesas fora do padrão
- Orçamento total e por categoria
- Previsão de fechamento, parcelas futuras e simulador parcelado

## Ferramentas

```bat
python -m unittest discover -s tests -t .
python diagnostico.py
```

`diagnostico.py` analisa uma cópia em memória e não altera o banco.

## Limitações conhecidas

- Duas compras idênticas (data, descrição e valor) são mantidas quando vêm na
  mesma importação; em importações separadas, a segunda é tratada como repetida.
- Parcelas futuras são uma estimativa baseada no texto `PARC x/y`.
