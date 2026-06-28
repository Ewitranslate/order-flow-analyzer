# Order Flow Analyzer + Divergence Engine (Binance)

MVP (Этапы 1–7): WebSocket trades → delta/cum-delta → CSV → Plotly chart.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Доступ (только зарегистрированные пользователи)

1. Скопируйте пример секретов:
   ```bash
   mkdir -p .streamlit
   cp .streamlit/secrets.toml.example .streamlit/secrets.toml
   ```
2. В `secrets.toml` задайте `auth.secret_key`.
3. Создать пользователя вручную (для админа):
   ```bash
   python src/auth_manage.py add myuser 'надёжный-пароль'
   ```
4. Запустите приложение — **главная** с входом и **регистрацией** (логин + пароль, без email).

Опционально: `require_email_verification = true` и блок `[auth.smtp]` — если нужно подтверждение по почте.

`auth.enabled = false` — только для локальной разработки без входа.

## Run streamer (writes CSV)

```bash
python -m src.main
```

## Plot from CSV

```bash
python -m src.visualization.plot --symbol btcusdt
```

CSV files appear in `./data/` (project root).

