"""Главная страница: описание приложения, вход и регистрация."""

from __future__ import annotations

import html

import streamlit as st

from auth import (
    _registration_key_expected,
    allow_registration,
    auth_enabled,
    enter_application,
    login_user,
    register_user,
)

_AUTH_FLASH = "home_auth_flash"
_AUTH_FLASH_KIND = "home_auth_flash_kind"


def _landing_css() -> None:
    st.markdown(
        """
<style>
.home-hero h1 {
    font-size: 2.1rem; font-weight: 700; margin-bottom: 0.35rem;
}
.home-lead {
    color: #b8c5db; font-size: 1.05rem; max-width: 52rem; line-height: 1.55;
}
.home-feat-card {
    background: #12171f; border: 1px solid #243044; border-radius: 10px;
    padding: 0.85rem 1rem; margin-bottom: 0.65rem;
}
.home-feat-card h4 { margin: 0 0 0.35rem 0; font-size: 1rem; }
.home-feat-card p {
    margin: 0.25rem 0 0 0; color: #9fb0c9; font-size: 0.92rem; line-height: 1.45;
}
</style>
""",
        unsafe_allow_html=True,
    )


_FEATURES: list[tuple[str, str]] = [
    (
        "Свечной график и кумулятивная δ",
        "Binance Spot и USDT-M perpetual: 5m, 15m, 1h, 2h, 4h, 1d. Отдельная пара для цены и для кумулятивной δ.",
    ),
    (
        "Order flow прокси",
        "Прокси по объёму свечи: 2×taker buy − volume. Накопление по выбранной паре.",
    ),
    (
        "Open Interest и VWAP",
        "Фьючерсный OI по паре графика, объём базового актива, VWAP с сбросом по UTC-дню.",
    ),
    (
        "Williams %R и дивергенции",
        "Индикаторы на графике; поиск дивергенций цена ↔ кум. δ на главной.",
    ),
    (
        "Cripto Scanner",
        "Все USDT spot пары: зоны EMA −20/−80, Williams %R и дивергенция цена ↔ кум. δ (5m–1d).",
    ),
]


def _feat_card(title: str, body: str) -> str:
    return (
        f'<div class="home-feat-card"><h4>{html.escape(title)}</h4>'
        f"<p>{html.escape(body)}</p></div>"
    )


def _set_auth_flash(kind: str, message: str) -> None:
    st.session_state[_AUTH_FLASH] = message
    st.session_state[_AUTH_FLASH_KIND] = kind


def _render_auth_flash() -> None:
    msg = st.session_state.pop(_AUTH_FLASH, None)
    kind = st.session_state.pop(_AUTH_FLASH_KIND, "info")
    if not msg:
        return
    if kind == "error":
        st.error(msg)
    elif kind == "warning":
        st.warning(msg)
    elif kind == "success":
        st.success(msg)
    else:
        st.info(msg)


def _handle_registration(username: str, password: str, password2: str, invite: str) -> None:
    if not allow_registration():
        _set_auth_flash("warning", "Регистрация закрыта. Обратитесь к администратору.")
        return

    name = (username or "").strip()
    if not name:
        _set_auth_flash("error", "Укажите логин.")
        return
    if not password:
        _set_auth_flash("error", "Укажите пароль.")
        return
    if password != password2:
        _set_auth_flash("error", "Пароли не совпадают.")
        return

    expected_invite = _registration_key_expected()
    if expected_invite and invite != expected_invite:
        _set_auth_flash("error", "Неверный код приглашения.")
        return

    try:
        register_user(name, password)
    except ValueError as e:
        msg = str(e)
        if "уже существует" in msg:
            msg = f"{msg}. Войдите на вкладке «Вход» или выберите другой логин."
        _set_auth_flash("error", msg)
        return
    except OSError as e:
        _set_auth_flash("error", f"Не удалось сохранить аккаунт: {e}")
        return

    login_result = login_user(name, password)
    if login_result == "ok":
        enter_application()
        st.rerun()
    elif login_result == "unverified":
        _set_auth_flash(
            "warning",
            "Аккаунт создан, но требуется подтверждение email. Войдите после подтверждения.",
        )
    else:
        _set_auth_flash("success", "Аккаунт создан. Войдите с логином и паролем.")


def render_home_landing() -> None:
    """Главная до входа: возможности + формы входа и регистрации."""
    from app import apply_dark_shell

    apply_dark_shell()
    _landing_css()

    st.markdown(
        """
<div class="home-hero">
  <h1>Order Flow Analyzer</h1>
  <p class="home-lead">
    Анализ потока ордеров и цены по данным Binance: свечи, кумулятивная δ,
    Open Interest и дивергенции. Доступ только для зарегистрированных пользователей.
  </p>
</div>
""",
        unsafe_allow_html=True,
    )

    feat_col, auth_col = st.columns([1.35, 1], gap="large")

    with feat_col:
        st.markdown("### Возможности")
        cards = "".join(_feat_card(t, b) for t, b in _FEATURES)
        st.markdown(cards, unsafe_allow_html=True)

    with auth_col:
        with st.container(border=True):
            _render_auth_flash()

            if auth_enabled():
                tab_login, tab_register = st.tabs(["Вход", "Регистрация"])

                with tab_login:
                    with st.form("home_login", clear_on_submit=False):
                        lu = st.text_input("Логин", autocomplete="username", key="home_login_user")
                        lp = st.text_input(
                            "Пароль",
                            type="password",
                            autocomplete="current-password",
                            key="home_login_pass",
                        )
                        login_submit = st.form_submit_button("Войти", type="primary", use_container_width=True)
                    if login_submit:
                        if not (lu or "").strip():
                            _set_auth_flash("error", "Укажите логин.")
                            st.rerun()
                        if not lp:
                            _set_auth_flash("error", "Укажите пароль.")
                            st.rerun()
                        result = login_user(lu, lp)
                        if result == "ok":
                            enter_application()
                            st.rerun()
                        elif result == "unverified":
                            _set_auth_flash("warning", "Аккаунт не активирован. Обратитесь к администратору.")
                            st.rerun()
                        elif result == "inactive":
                            _set_auth_flash("error", "Аккаунт заблокирован администратором.")
                            st.rerun()
                        else:
                            _set_auth_flash("error", "Неверный логин или пароль.")
                            st.rerun()

                with tab_register:
                    reg_open = allow_registration()
                    if not reg_open:
                        st.caption("Самостоятельная регистрация отключена администратором.")

                    with st.form("home_register", clear_on_submit=False):
                        ru = st.text_input(
                            "Логин",
                            autocomplete="username",
                            key="home_reg_user",
                            help="3–64 символа: латиница, цифры, . _ -",
                            disabled=not reg_open,
                        )
                        rp = st.text_input(
                            "Пароль",
                            type="password",
                            autocomplete="new-password",
                            key="home_reg_pass",
                            help="Не короче 8 символов",
                            disabled=not reg_open,
                        )
                        rp2 = st.text_input(
                            "Повтор пароля",
                            type="password",
                            autocomplete="new-password",
                            key="home_reg_pass2",
                            disabled=not reg_open,
                        )
                        invite = ""
                        if _registration_key_expected():
                            invite = st.text_input(
                                "Код приглашения",
                                type="password",
                                key="home_reg_invite",
                                disabled=not reg_open,
                            )
                        reg_submit = st.form_submit_button(
                            "Создать аккаунт",
                            type="primary",
                            use_container_width=True,
                            disabled=not reg_open,
                        )

                    if reg_submit:
                        _handle_registration(ru, rp, rp2, invite)
                        if st.session_state.get(_AUTH_FLASH):
                            st.rerun()
