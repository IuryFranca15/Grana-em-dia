# Grana em Dia — Controle Financeiro Pessoal

Aplicação local em Python + Streamlit para importar faturas, revisar despesas, classificar compras e acompanhar os gastos ao longo do tempo.

## O que esta versão faz

- Reconhece automaticamente faturas do **Nubank** e do **Banco do Brasil (Ourocard)** — PDF ou texto copiado.
- Importa faturas em **PDF**, texto copiado, **CSV** e **Excel (.xlsx)**.
- Tenta reconhecer compras mesmo quando o PDF quebra descrições em linhas ou coloca mais de um lançamento na mesma linha.
- Reconhece estabelecimentos da fatura de exemplo: IFD/iFood, Tocapub, Glacai, Uber, 99, Nelfarma, Caio Farma, Redepharma, Pague Menos, Google, YouTube e Amazon Prime.
- Classifica multas, juros e encargos em **Tarifas e encargos**.
- Ignora saldo anterior, pagamentos, créditos e estornos para não contabilizá-los como novas despesas.
- Mostra uma prévia editável antes de salvar qualquer lançamento.
- Evita parte das duplicidades na reimportação.
- Permite adicionar despesas manualmente, editar categorias, criar regras personalizadas e exportar CSV.
- Apresenta gráficos, histórico, médias e projeções simples.

## Requisitos

- Windows 10 ou 11
- Python 3.10 ou superior

## Instalação no Windows

1. Instale Python pelo site oficial: https://www.python.org/downloads/
2. Durante a instalação, marque **Add Python to PATH**.
3. Extraia a pasta do projeto.
4. Abra o Prompt de Comando dentro da pasta `controle_financeiro`.
5. Execute:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
streamlit run app.py
```

O navegador abrirá o aplicativo. Para encerrar, pressione `Ctrl+C` no terminal.

## Testes rápidos do leitor

Para executar os testes automatizados do classificador e do importador:

```bat
python -m unittest discover -s tests -v
```

## Dados e privacidade

- O banco SQLite fica em `data/financeiro.db` e é criado automaticamente.
- PDFs e planilhas são processados localmente.
- O aplicativo não envia faturas para um servidor nem exige conexão bancária.
- Faça cópia de segurança da pasta `data` para preservar o histórico.
- Evite compartilhar faturas sem ocultar CPF, endereço, número do cartão, códigos e demais dados pessoais.

## Sobre a leitura de faturas

Os emissores usam layouts diferentes. O leitor combina reconhecimento de datas e valores com regras para descrições quebradas e lançamentos lado a lado. Mesmo assim, **a prévia deve ser conferida antes de salvar**. Se um banco mudar o layout ou o PDF for uma digitalização em imagem, a leitura poderá falhar. OCR ainda não está incluído.

**Nubank:** as datas vêm como `16 SET` (o ano é deduzido do cabeçalho da fatura). Pagamentos, "Saldo em atraso", "Crédito de atraso" e o par "Juros de dívida encerrada / Encerramento de dívida" (que se anulam) são ignorados; IOF, juros e multa de atraso entram como `Tarifas e encargos`. Compras financiadas no crédito (Pix/boleto), que aparecem com o seu nome, entram como despesa com o valor total a pagar.

Depois de ler, a prévia compara o total identificado com o total da fatura (diferenças de centavos por arredondamento são normais).

Pagamentos, créditos, estornos e saldo anterior são excluídos do total de despesas. Multas, juros e encargos positivos são registrados como despesas na categoria `Tarifas e encargos`.

## Próximas evoluções possíveis

- Adaptar o leitor a layouts específicos de cada banco com PDFs anonimizados.
- Adicionar OCR para faturas digitalizadas.
- Melhorar o tratamento de estornos, parcelamentos e compras repetidas.
- Criar orçamento mensal por categoria e alertas de limite.
