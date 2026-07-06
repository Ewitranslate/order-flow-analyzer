# Order Flow Analyzer

Веб-приложение на **Streamlit** для анализа крипторынка Binance: order flow (δ / cum-delta), Williams %R сканер, Price Compression, ATR, дивергенции, Open Interest.

## Возможности

- **Главный график** — свечи, объём, VWAP, кумулятивная δ, OI (futures), Williams %R, ATR, дивергенции цена ↔ δ, зоны **Price Compression**
- **Cripto Scanner** — массовый поиск по USDT spot (Williams, SMA, сжатие цены, Δкум/OI/ATR 24ч, дивергенции)
- **Авторизация** — регистрация, сессии, активные устройства (см. `docs/SESSION_AUTH.md`)

## Быстрый старт

```bash
git clone https://github.com/Ewitranslate/order-flow-analyzer.git
cd order-flow-analyzer

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Секреты (обязательно перед запуском)

```bash
mkdir -p .streamlit data
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
cp data/users.json.example data/users.json
```

Отредактируйте `.streamlit/secrets.toml`:

- `auth.secret_key` — случайная строка 32+ символов
- при необходимости `telegram.bot_token` / `chat_id` для уведомлений сканера

Создайте первого пользователя:

```bash
python src/auth_manage.py add admin 'ваш-надёжный-пароль' --email you@example.com
```

Для локальной разработки без входа: `auth.enabled = false` в `secrets.toml`.

### Запуск

```bash
streamlit run src/app.py
```

Откройте в браузере: http://localhost:8501

## Структура проекта

```
src/
  app.py                 — главное приложение Streamlit
  pages/                 — Cripto Scanner, Account, Admin, …
  price_compression.py   — алгоритм сжатия цены (Pivot + регрессия)
  williams_scanner.py    — сканер Binance spot
  auth*.py               — авторизация и сессии
data/                    — users.json, кэш (не коммитится)
.streamlit/secrets.toml  — секреты (не коммитится)
```

## Деплой (Streamlit Community Cloud)

1. Запушьте репозиторий на **публичный** GitHub
2. [share.streamlit.io](https://share.streamlit.io) → **New app**
3. **Main file path:** `src/app.py`
4. В **Secrets** вставьте содержимое `secrets.toml`
5. Deploy

> Для продакшена используйте сильный `secret_key`, HTTPS и `auth.enabled = true`.

## Что не попадает в git

| Файл | Причина |
|------|---------|
| `.streamlit/secrets.toml` | ключи, токены |
| `data/users.json` | пароли (хэши) |
| `data/auth.sqlite3` | сессии |
| `data/*.csv`, `data/cache/` | локальные данные |

## Лицензия

MIT — см. `LICENSE` (при необходимости добавьте файл).
