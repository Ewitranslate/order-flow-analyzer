# Авторизация: сессии (без лимита устройств)

Одна **активная сессия** на аккаунт. Новый вход завершает предыдущую. JWT access + opaque refresh в SQLite.

## Модули

| Файл | Назначение |
|------|------------|
| `src/auth.py` | Пользователи (`users.json`), вход/выход, ACL страниц |
| `src/auth_store.py` | SQLite-схема и миграции |
| `src/auth_sessions.py` | Создание, проверка, отзыв сессий |
| `src/auth_jwt.py` | Access JWT (HS256), refresh-токены |
| `src/auth_guard.py` | Проверка сессии на каждой странице Streamlit |
| `src/auth_client.py` | User-Agent, IP (метаданные сессии) |
| `src/auth_geo.py` | Страна/город по IP |
| `src/auth_audit.py` | Журнал попыток входа |
| `src/home_page.py` | Вход и регистрация |
| `src/pages/5_Account.py` | Журнал входов |
| `src/pages/6_Active_Sessions.py` | Активные сессии |

## Таблица `user_sessions`

| Поле | Описание |
|------|----------|
| `session_id` | UUID сессии (PK) |
| `username` | Логин пользователя (`user_id` в терминах приложения) |
| `refresh_token_hash` | SHA-256 opaque refresh-токена |
| `access_jti` | Идентификатор текущего access JWT (не хранится сам JWT) |
| `status` | `active` \| `revoked` \| `expired` |
| `created_at` | Время создания (вход) |
| `last_active_at` | Последняя активность |
| `revoked_at` | Когда завершена |
| `revoked_reason` | `superseded`, `logout`, `manual_other`, `inactivity`, `admin` |
| `ip_address` | IP при входе |
| `browser` | Браузер |
| `os_name` | ОС |
| `device_name` | Человекочитаемое «устройство» |
| `country`, `city` | Геолокация по IP |
| `user_agent` | Полный User-Agent |
| `is_active` | Устаревший флаг (синхронизируется со `status`) |

Таблица `user_devices` **удалена** (миграция v2).

## Поток авторизации

1. **Вход:** пароль → `create_user_session()` → все старые `active` сессии → `revoked` (`superseded`) → новая сессия `active` + access/refresh.
2. **Каждый запрос (страница):** `require_valid_session()` проверяет JWT, `session_id`, `access_jti`, `status = active`.
3. **Refresh:** по паре `refresh_token` + `session_id`; ротация refresh и `access_jti`.
4. **Другой браузер:** старая сессия `superseded` → при обновлении страницы сообщение: *«Ваш аккаунт был открыт на другом устройстве. Выполните вход снова.»*
5. **Неактивность:** сессии без активности дольше `session_inactivity_days` (по умолчанию 30) → `expired`.

## Конфигурация (`secrets.toml`)

```toml
[auth]
auth_db = "data/auth.sqlite3"
access_token_ttl_min = 15
refresh_token_ttl_days = 7
session_inactivity_days = 30
```

## Как протестировать

### 1. Одна активная сессия
1. `python3 -m streamlit run src/app.py`
2. Войдите в **Chrome**.
3. Войдите тем же логином в **Firefox** (или приватное окно).
4. Обновите страницу в Chrome — предупреждение о входе с другого устройства.

### 2. Страница «Активные сессии»
Сайдбар → **Активные сессии** — список с браузером, ОС, IP, гео, временем входа и активности. Текущая помечена **Current**.

### 3. Завершить остальные
На странице сессий — **Завершить все остальные сессии** (если их больше одной активной).

### 4. CLI
```bash
python3 src/auth_manage.py clear-sessions USERNAME
sqlite3 data/auth.sqlite3 "SELECT session_id, username, status, browser, created_at FROM user_sessions;"
```

### 5. JWT refresh
Подождите истечения access TTL (или сократите `access_token_ttl_min` до 1) — страница должна продлить сессию без повторного входа.
