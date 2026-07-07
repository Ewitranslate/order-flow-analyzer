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

## Деплой (Render)

1. [render.com](https://render.com) → **New Web Service** → репозиторий `Ewitranslate/order-flow-analyzer`
2. **Start Command:** `bash scripts/render_start.sh`
3. **Health Check Path:** `/_stcore/health`
4. **Environment** (обязательно):

| Переменная | Пример |
|------------|--------|
| `AUTH_SECRET_KEY` | случайная строка 32+ символов |
| `AUTH_ENABLED` | `true` |
| `AUTH_ALLOW_REGISTRATION` | `true` |
| `AUTH_USERS_FILE` | `data/users.json` |
| `AUTH_DB_FILE` | `data/auth.sqlite3` |
| `AUTH_BOOTSTRAP_USER` | `ewitranslate` (логин админа, создаётся/обновляется при старте) |
| `AUTH_BOOTSTRAP_PASSWORD` | пароль админа, ≥8 символов — **только в Render Environment**, не в git |
| `PYTHON_VERSION` | `3.11.9` |

При старте создаётся пустое хранилище в `data/auth.sqlite3` (и зеркало `users.json`) и при необходимости — админ из `AUTH_BOOTSTRAP_*`.

**Важно:** без Persistent Disk аккаунты сбрасываются при каждом redeploy. Надёжный вариант — задать `AUTH_BOOTSTRAP_USER` / `AUTH_BOOTSTRAP_PASSWORD` в Environment (пароль сбрасывается при каждом старте, если пользователь уже есть).

5. **Persistent Disk** → mount path **`/opt/render/project/src/data`** (или `data` относительно корня), размер 1 GB
6. **Первый вход** — один из вариантов:
   - вкладка **«Регистрация»** на сайте, или
   - в Environment задать `AUTH_BOOTSTRAP_USER=admin` и `AUTH_BOOTSTRAP_PASSWORD=ваш_пароль_8+` → redeploy
7. **Manual Deploy** → Clear build cache & deploy

В логах после старта ищите: `Auth storage: users=... count=1` и `Bootstrap: создан администратор`.

> Если пишет «Пользователь не найден» — аккаунтов нет (диск пустой или сброшен).  
> Если «Неверный пароль» — логин верный, сбросьте пароль через `AUTH_BOOTSTRAP_PASSWORD` и redeploy.

### Binance API на Render

Серверы Render иногда **не могут достучаться** до `api.binance.com` (geo-block / 451). Приложение автоматически пробует зеркала, в т.ч. `data-api.binance.vision`.

| Переменная | Когда нужна |
|------------|-------------|
| `BINANCE_HTTP_PROXY` | HTTP(S)-прокси для всех запросов к Binance |
| `HTTPS_PROXY` / `HTTP_PROXY` | альтернатива, если прокси уже настроен в окружении |
| `BINANCE_FAPI_BASE` | другой URL USDT-M futures (по умолчанию `https://fapi.binance.com`) |

Если график пустой и внизу видно `Binance API: ...` — задайте прокси и сделайте **Clear build cache & deploy**.

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
