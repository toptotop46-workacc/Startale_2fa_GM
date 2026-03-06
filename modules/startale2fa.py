#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StartaleGM (упрощённый) — импорт кошелька в Rabby и подключение к Startale.
Через AdsPower + Playwright. Страница остаётся открытой после подключения.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import random
import warnings
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

if sys.platform == "win32":
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010

import requests
from loguru import logger
from web3 import Web3

from modules import db
from modules.portal_api import check_startale_gm_5_done, check_startale_passkey_quest_done

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROXY_FILE = PROJECT_ROOT / "proxy.txt"
if __name__ == "__main__":
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

STARTALE_LOGIN_URL = "https://app.startale.com/log-in"
STARTALE_APP_URL = "https://app.startale.com/"
RABBY_EXTENSION_ID = "acmacodkjbdgmoleebolmdjonilkdbch"
# Bitwarden (popup), открываемое из Settings
EXTENSION_POPUP_ID = "nngceckbapebfimnlniiiahkandclblb"

# Временная почта: mail.tm (запросы через прокси из proxy.txt)
MAILTM_BASE = "https://api.mail.tm"
MAILTM_PASSWORD = "BitwardenTemp1"

# Мастер-пароль Bitwarden: 12+ символов; подсказка: до 50 символов
BITWARDEN_MASTER_PASSWORD = "Password1234!@#45"
BITWARDEN_MASTER_HINT = "startale"

# Регулярка для ссылки подтверждения Bitwarden
BITWARDEN_VERIFY_LINK_RE = re.compile(
    r"https://vault\.bitwarden\.com/redirect-connector\.html#finish-signup\?[^\s\"'<>]+"
)


async def _human_like_click(page, locator, timeout: int = 15000) -> None:
    """
    Эмулирует клик мышью как у человека: движение к случайной точке внутри элемента,
    небольшая пауза, затем mousedown/mouseup.
    """
    await locator.wait_for(state="attached", timeout=timeout)
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    await locator.wait_for(state="visible", timeout=timeout)
    box = await locator.bounding_box()
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        await locator.click(timeout=timeout)
        return
    # Случайная точка внутри кнопки (отступ от краёв ~20%)
    padding_w = max(2, box["width"] * 0.2)
    padding_h = max(2, box["height"] * 0.2)
    x = box["x"] + padding_w + random.uniform(0, max(1, box["width"] - 2 * padding_w))
    y = box["y"] + padding_h + random.uniform(0, max(1, box["height"] - 2 * padding_h))
    await page.mouse.move(x, y)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.02, 0.08))
    await page.mouse.up()


def _close_windows_passkey_dialog_win() -> Tuple[int, List[str]]:
    """
    Закрывает системные окна Windows, в заголовке которых есть 'Passkeys', 'Security Keys'
    или 'Windows Security'. Возвращает (количество закрытых окон, список заголовков).
    Только для Windows; на других ОС возвращает (0, []).
    """
    if sys.platform != "win32":
        return 0, []
    closed_titles: List[str] = []
    keywords = ("Passkeys", "Security Keys", "Security Key", "Windows Security", "Безопасность")

    def _enum_callback(hwnd: int, _lparam: int) -> bool:
        try:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value or ""
            if any(kw in title for kw in keywords):
                if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                    closed_titles.append(title)
        except Exception:
            pass
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(WNDENUMPROC(_enum_callback), 0)
    return len(closed_titles), closed_titles


def _poll_quest_done_until_enter(wallet_address: str, interval_sec: int = 12) -> None:
    """
    Фоновый поток: каждые interval_sec сек опрашивает портал (bonus-dapp).
    При первом isDone по квесту passkey startale_7 выводит сообщение в консоль (один раз).
    """
    notified = [False]

    def _run() -> None:
        while True:
            try:
                proxies = load_random_proxy()
                if check_startale_passkey_quest_done(wallet_address, proxies):
                    if not notified[0]:
                        notified[0] = True
                        logger.success(
                            "Квест по passkey засчитан на портале. "
                            "Можете отвязать passkey в браузере (троеточие → Remove passkey → подтвердить) и нажать Enter здесь."
                        )
                    break
            except Exception:
                pass
            time.sleep(interval_sec)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


async def _wait_quest_done_then_unbind_passkey(page, wallet_address: str, interval_sec: int = 12) -> None:
    """
    Опрашивает портал каждые interval_sec сек; при засчитывании квеста отвязывает passkey
    на странице Startale (троеточие → Remove passkey → подтвердить в диалоге). Все клики — human-like.
    """
    logger.info("Ожидание засчитывания квеста на портале (опрос каждые {} сек)...", interval_sec)
    while True:
        proxies = load_random_proxy()
        done = await asyncio.to_thread(check_startale_passkey_quest_done, wallet_address, proxies)
        if done:
            break
        await asyncio.sleep(interval_sec)
    logger.success("Квест засчитан. Отвязываем passkey...")
    # Три точки в карточке passkey (кнопка с aria-haspopup="menu" в блоке с текстом "Passkey [ID:")
    menu_btn = page.locator("div.rounded-xl.border.border-zinc-200").filter(has_text="Passkey [ID:").locator("button[aria-haspopup='menu']").first
    await _human_like_click(page, menu_btn, timeout=30000)
    await asyncio.sleep(0.3)
    # Пункт меню "Remove passkey"
    remove_menuitem = page.get_by_role("menuitem", name=re.compile(r"Remove\s+passkey", re.IGNORECASE))
    await _human_like_click(page, remove_menuitem.first, timeout=20000)
    await asyncio.sleep(0.3)
    # Подтверждение в диалоге — кнопка "Remove passkey"
    confirm_btn = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"Remove\s+passkey", re.IGNORECASE))
    await _human_like_click(page, confirm_btn.first, timeout=20000)
    await asyncio.sleep(5)
    # Проверка: на странице должен появиться текст "No passkeys yet"
    await page.get_by_text("No passkeys yet").wait_for(state="visible", timeout=30000)
    logger.success("Passkey отвязан (на странице отображается «No passkeys yet»).")


def load_random_proxy() -> Optional[dict]:
    """Загружает proxy.txt и возвращает случайный прокси для requests (или None)."""
    if not PROXY_FILE.exists():
        return None
    lines = []
    with open(PROXY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(":")
                if len(parts) >= 4:
                    ip, port, user, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
                    proxy_url = f"http://{user}:{password}@{ip}:{port}"
                    lines.append({"http": proxy_url, "https": proxy_url})
    return random.choice(lines) if lines else None


def get_disposable_email(proxies: Optional[dict] = None) -> str:
    """Создаёт временный аккаунт mail.tm и возвращает email. Запросы через proxies."""
    r = requests.get(
        f"{MAILTM_BASE}/domains",
        headers={"Accept": "application/json"},
        proxies=proxies,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "hydra:member" in data:
        domains = data["hydra:member"]
    elif isinstance(data, list):
        domains = data
    else:
        raise ValueError("Неверный ответ mail.tm/domains")
    if not domains:
        raise ValueError("Нет доступных доменов mail.tm")
    domain = domains[0].get("domain") if isinstance(domains[0], dict) else domains[0]
    local = f"startale_{uuid.uuid4().hex[:12]}"
    address = f"{local}@{domain}"
    create = requests.post(
        f"{MAILTM_BASE}/accounts",
        json={"address": address, "password": MAILTM_PASSWORD},
        headers={"Content-Type": "application/json"},
        proxies=proxies,
        timeout=15,
    )
    create.raise_for_status()
    return address


def fetch_verification_link_from_inbox(
    email: str,
    timeout_seconds: int = 120,
    poll_interval: int = 8,
    proxies: Optional[dict] = None,
) -> Optional[str]:
    """
    Опрашивает mail.tm: ждёт письмо и извлекает ссылку подтверждения Bitwarden.
    Запросы через proxies.
    """
    if "@" not in email:
        return None
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            token_r = requests.post(
                f"{MAILTM_BASE}/token",
                json={"address": email, "password": MAILTM_PASSWORD},
                headers={"Content-Type": "application/json"},
                proxies=proxies,
                timeout=15,
            )
            token_r.raise_for_status()
            token = token_r.json().get("token")
            if not token:
                raise ValueError("Нет token в ответе")
            msg_list = requests.get(
                f"{MAILTM_BASE}/messages",
                headers={"Authorization": f"Bearer {token}"},
                proxies=proxies,
                timeout=15,
            )
            msg_list.raise_for_status()
            messages = msg_list.json()
            if isinstance(messages, dict) and "hydra:member" in messages:
                messages = messages["hydra:member"]
            elif not isinstance(messages, list):
                messages = []
            for msg in messages:
                msg_id = msg.get("id") if isinstance(msg, dict) else msg
                if not msg_id:
                    continue
                read_r = requests.get(
                    f"{MAILTM_BASE}/messages/{msg_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    proxies=proxies,
                    timeout=15,
                )
                read_r.raise_for_status()
                data = read_r.json()
                html = data.get("html")
                if isinstance(html, list):
                    body = " ".join(html)
                else:
                    body = str(html or data.get("htmlBody") or data.get("body") or data.get("text") or "")
                match = BITWARDEN_VERIFY_LINK_RE.search(body)
                if match:
                    return match.group(0).rstrip("&>'\"")
        except Exception as e:
            logger.debug("Ошибка при опросе почты: {}", e)
        time.sleep(poll_interval)
    return None


def _read_keys_from_file() -> list[str]:
    """Читает список приватных ключей из keys.txt."""
    keys_file = PROJECT_ROOT / "keys.txt"
    if not keys_file.exists():
        raise FileNotFoundError(
            f"Файл {keys_file} не найден. Создайте файл и укажите в нём приватные ключи."
        )
    keys = []
    with open(keys_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                if re.match(r"^0x[a-fA-F0-9]{64}$", line):
                    keys.append(line)
                elif re.match(r"^[a-fA-F0-9]{64}$", line):
                    keys.append("0x" + line)
    if not keys:
        raise ValueError(f"В файле {keys_file} не найдено действительных приватных ключей")
    return keys


def get_keys_count() -> int:
    """Возвращает количество ключей в keys.txt."""
    return len(_read_keys_from_file())


def load_private_key(key_index: int = 0) -> str:
    """Загружает приватный ключ из keys.txt по индексу."""
    keys = _read_keys_from_file()
    if key_index < 0 or key_index >= len(keys):
        raise ValueError(
            f"Индекс ключа {key_index} вне диапазона (доступно: {len(keys)})"
        )
    return keys[key_index]


def load_adspower_api_key() -> str:
    """Загружает API ключ AdsPower из adspower_api_key.txt."""
    api_key_file = PROJECT_ROOT / "adspower_api_key.txt"
    if not api_key_file.exists():
        raise FileNotFoundError(
            f"Файл {api_key_file} не найден. Укажите в нём API ключ AdsPower."
        )
    with open(api_key_file, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    if not lines or not lines[0] or lines[0] == "your_adspower_api_key_here":
        raise ValueError(
            f"В файле {api_key_file} укажите реальный API ключ AdsPower."
        )
    return lines[0]


def _get_cdp_endpoint(browser_info: dict) -> Optional[str]:
    """Извлекает CDP (Puppeteer) endpoint из ответа AdsPower."""
    ws_data = browser_info.get("ws")
    if isinstance(ws_data, dict):
        cdp = ws_data.get("puppeteer")
        if cdp:
            return cdp
    cdp = (
        browser_info.get("ws_endpoint")
        or browser_info.get("ws_endpoint_driver")
        or browser_info.get("puppeteer")
        or browser_info.get("debugger_address")
    )
    if isinstance(cdp, dict):
        cdp = cdp.get("puppeteer") or cdp.get("ws")
    if isinstance(cdp, str) and cdp.startswith("ws://"):
        return cdp
    for _, value in browser_info.items():
        if isinstance(value, str) and value.startswith("ws://"):
            return value
        if isinstance(value, dict):
            cdp = value.get("puppeteer") or value.get("ws")
            if cdp:
                return cdp
    return None


class StartaleGMBrowser:
    """Создание профиля AdsPower, запуск браузера, импорт кошелька, подключение к Startale."""

    def __init__(
        self,
        api_key: str,
        api_port: int = 50325,
        base_url: Optional[str] = None,
        timeout: int = 30,
    ):
        self.api_key = api_key
        self.base_url = base_url or f"http://local.adspower.net:{api_port}"
        self.timeout = timeout
        self.profile_id: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update(
            {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        )

    def _make_request(
        self, method: str, endpoint: str, data: Optional[dict] = None
    ) -> dict:
        url = f"{self.base_url}{endpoint}"
        params = {"api_key": self.api_key}
        if method.upper() == "GET":
            r = self.session.get(url, params=params, timeout=self.timeout)
        elif method.upper() == "POST":
            r = self.session.post(url, params=params, json=data, timeout=self.timeout)
        else:
            raise ValueError(f"Метод {method} не поддерживается")
        r.raise_for_status()
        result = r.json()
        if result.get("code") != 0:
            raise ValueError(result.get("msg", "Ошибка API AdsPower"))
        return result

    def create_temp_profile(self, use_proxy: bool = True) -> str:
        """Создаёт временный профиль браузера (по умолчанию со случайным прокси из AdsPower)."""
        name = f"startale2fa_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        profile_data = {
            "name": name,
            "group_id": "0",
            "fingerprint_config": {
                "automatic_timezone": "1",
                "language_switch": "0",
                "language": ["en-US", "en"],
                "webrtc": "disabled",
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            },
        }
        if use_proxy:
            profile_data["proxyid"] = "random"
            logger.info("Профиль создаётся со случайным прокси из AdsPower")
        else:
            profile_data["user_proxy_config"] = {"proxy_soft": "no_proxy"}
        result = self._make_request("POST", "/api/v2/browser-profile/create", profile_data)
        self.profile_id = result.get("data", {}).get("profile_id")
        if not self.profile_id:
            raise ValueError("API не вернул profile_id")
        logger.info(f"Профиль создан: {self.profile_id}")
        return self.profile_id

    def start_browser(self, profile_id: Optional[str] = None) -> dict:
        """Запускает браузер по profile_id. Отключает нативный Windows WebAuthn (Windows Hello), чтобы открывалось окно Bitwarden при создании passkey."""
        pid = profile_id or self.profile_id
        if not pid:
            raise ValueError("Не указан profile_id")
        body = {
            "profile_id": pid,
            "launch_args": ["--disable-features=WebAuthenticationUseNativeWinApi"],
        }
        result = self._make_request("POST", "/api/v2/browser-profile/start", body)
        data = result.get("data", {})
        if not data:
            raise ValueError("API не вернул данные браузера")
        logger.info("Браузер запущен")
        return data

    def stop_browser(self, profile_id: Optional[str] = None) -> None:
        """Закрывает браузер по profile_id."""
        pid = profile_id or self.profile_id
        if not pid:
            return
        self._make_request("POST", "/api/v2/browser-profile/stop", {"profile_id": pid})

    def delete_profile(self, profile_id: Optional[str] = None) -> None:
        """Удаляет профиль (вместе с кэшем и данными)."""
        pid = profile_id or self.profile_id
        if not pid:
            return
        try:
            self._make_request("POST", "/api/v2/browser-profile/delete", {"profile_id": [pid]})
        except ValueError:
            self._make_request("POST", "/api/v2/browser-profile/delete", {"Profile_id": [pid]})
        logger.info("Временный профиль удалён")

    async def _import_wallet(
        self, cdp_endpoint: str, private_key: str, password: str = "Password123"
    ) -> None:
        """Импортирует кошелёк в Rabby по CDP."""
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
            if not browser.contexts:
                raise RuntimeError("Нет контекстов в браузере")
            context = browser.contexts[0]
            setup_url = f"chrome-extension://{RABBY_EXTENSION_ID}/index.html#/new-user/guide"
            page = None
            for p in context.pages:
                if RABBY_EXTENSION_ID in p.url or ("chrome-extension://" in p.url and "rabby" in p.url.lower()):
                    page = p
                    if "#/new-user/guide" not in p.url:
                        await page.goto(setup_url)
                        await asyncio.sleep(2)
                    break
            if not page:
                page = await context.new_page()
                await page.goto(setup_url)
                await asyncio.sleep(3)

            # 1) I already have an address
            await page.wait_for_selector('span:has-text("I already have an address")', timeout=60000)
            await page.click('span:has-text("I already have an address")')
            await asyncio.sleep(0.5)
            # 2) Seed phrase or private key (карточка, не Hardware Wallet)
            seed_phrase_card = page.locator('div.rabby-ItemWrapper-rabby--mylnj7').filter(has_text="Seed phrase or private key")
            await seed_phrase_card.wait_for(state="visible", timeout=60000)
            await seed_phrase_card.click()
            await asyncio.sleep(0.5)
            # 3) Вкладка Private Key
            await page.wait_for_selector('div.pills-switch__item:has-text("Private Key")', timeout=60000)
            await page.click('div.pills-switch__item:has-text("Private Key")')
            await asyncio.sleep(0.5)
            # 4) Поле для приватного ключа (id=privateKey, в новом UI type=password)
            await page.wait_for_selector("#privateKey", timeout=60000)
            await page.fill("#privateKey", private_key)
            await asyncio.sleep(0.3)
            # 5) Кнопка Next (активна после ввода ключа)
            await page.wait_for_selector('button.ant-btn-primary:has-text("Next"):not([disabled])', timeout=60000)
            await page.click('button.ant-btn-primary:has-text("Next"):not([disabled])')
            await asyncio.sleep(0.5)
            # 6) Пароль и подтверждение
            await page.wait_for_selector('#password', timeout=60000)
            await page.fill("#password", password)
            await page.wait_for_selector('#confirmPassword', timeout=60000)
            await page.fill("#confirmPassword", password)
            await asyncio.sleep(0.3)
            # 7) Кнопка Confirm
            await page.wait_for_selector('button.ant-btn-primary:has-text("Confirm"):not([disabled])', timeout=60000)
            await page.click('button.ant-btn-primary:has-text("Confirm"):not([disabled])')
            await page.wait_for_selector("text=Imported Successfully", timeout=60000)
            logger.success("Кошелёк импортирован в Rabby")
            await page.close()
            logger.info("Вкладка импорта кошелька закрыта")
            # Закрыть вкладку Bitwarden (browser-start), если открыта
            for p in context.pages:
                if "bitwarden.com/browser-start" in p.url:
                    await p.close()
                    logger.info("Вкладка bitwarden.com/browser-start закрыта")
                    break
        finally:
            await playwright.stop()

    async def _connect_startale(self, cdp_endpoint: str, wallet_address: str, *, do_passkey: bool = True) -> None:
        """Открывает app.startale.com/log-in, подключает кошелёк и переходит на app.startale.com. Если do_passkey=True — выполняет шаги Bitwarden+passkey."""
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
            if not browser.contexts:
                raise RuntimeError("Нет контекстов в браузере")
            context = browser.contexts[0]
            page = None
            for p in context.pages:
                if not p.url.startswith("chrome-extension://"):
                    page = p
                    break
            if not page:
                page = await context.new_page()
            await page.goto(STARTALE_LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            logger.success(f"Открыта страница: {STARTALE_LOGIN_URL}")

            connect_btn = page.get_by_role("button", name="Connect a wallet")
            await connect_btn.wait_for(state="visible", timeout=45000)
            await connect_btn.click()
            logger.success('Нажата кнопка "Connect a wallet"')
            await asyncio.sleep(2)

            rabby_btn = page.get_by_role("button", name="Rabby")
            await rabby_btn.wait_for(state="visible", timeout=45000)
            async with context.expect_page() as wallet_popup_info:
                await rabby_btn.click()
            wallet_popup = await wallet_popup_info.value
            await wallet_popup.wait_for_load_state("domcontentloaded", timeout=30000)
            logger.success("Открыто popup окно кошелька Rabby")

            connect_btn_wallet = wallet_popup.get_by_role("button", name="Connect")
            await connect_btn_wallet.wait_for(state="visible", timeout=45000)
            await connect_btn_wallet.click()
            logger.success("Нажата кнопка Connect в popup кошелька")

            sign_popup = await context.wait_for_event("page", timeout=45000)
            await sign_popup.wait_for_load_state("domcontentloaded", timeout=30000)
            logger.success("Открыт popup кошелька (Sign/Confirm)")

            sign_btn = sign_popup.get_by_role("button", name="Sign")
            await sign_btn.wait_for(state="visible", timeout=45000)
            await sign_btn.click()
            logger.success("Нажата кнопка Sign в popup кошелька")
            await asyncio.sleep(1)

            confirm_btn = sign_popup.get_by_role("button", name="Confirm")
            await confirm_btn.wait_for(state="visible", timeout=45000)
            await confirm_btn.click()
            logger.success("Нажата кнопка Confirm в popup кошелька")
            await asyncio.sleep(1)

            approve_btn = page.get_by_role("button", name="Approve")
            try:
                await approve_btn.wait_for(state="visible", timeout=30000)
                await approve_btn.click()
                logger.success("Нажата кнопка Approve на странице log-in")
            except Exception:
                pass
            await asyncio.sleep(1)

            await page.goto(STARTALE_APP_URL, wait_until="domcontentloaded", timeout=90000)
            logger.success("Открыта страница {}", STARTALE_APP_URL)
            if not do_passkey:
                logger.info("Passkey не требуется — пропускаем шаги Bitwarden/passkey.")
                return
            # Клик по иконке кошелька → выпадающее меню → Settings
            wallet_icon = page.locator('img[alt="Wallet"]')
            await wallet_icon.wait_for(state="visible", timeout=45000)
            await wallet_icon.click()
            await asyncio.sleep(0.5)
            settings_btn = page.locator('span.text-zinc-950').filter(has_text="Settings")
            await settings_btn.wait_for(state="visible", timeout=30000)
            await settings_btn.click()
            logger.success("Открыт раздел Settings")
            # Открыть расширение в новой вкладке
            extension_url = f"chrome-extension://{EXTENSION_POPUP_ID}/popup/index.html"
            ext_page = await context.new_page()
            await ext_page.goto(extension_url, wait_until="domcontentloaded", timeout=45000)
            logger.success("Открыто расширение")
            await asyncio.sleep(1)
            # Create account (текст внутри span: " Create account ")
            create_btn = ext_page.get_by_text("Create account", exact=False)
            await create_btn.first.wait_for(state="visible", timeout=45000)
            await create_btn.first.click()
            await asyncio.sleep(1)
            # Дождаться формы регистрации (поле Email address)
            await ext_page.wait_for_selector("#register-start_form_input_email", timeout=45000)
            # Временный email через mail.tm (запросы через случайный прокси из proxy.txt)
            proxies = load_random_proxy()
            if proxies:
                logger.info("Запросы к mail.tm через прокси из proxy.txt")
            disposable_email = get_disposable_email(proxies)
            logger.info("Временный email: {}", disposable_email)
            await ext_page.fill("#register-start_form_input_email", disposable_email)
            await asyncio.sleep(0.3)
            # Continue
            continue_btn = ext_page.locator('button[type="submit"]').filter(has_text="Continue")
            await continue_btn.wait_for(state="visible", timeout=20000)
            await continue_btn.click()
            logger.success("Bitwarden: введён email, нажато Continue")
            # Ждём письмо с ссылкой подтверждения и переходим по ней
            logger.info("Ожидание письма с подтверждением (до 2 мин)...")
            verification_link = await asyncio.to_thread(
                fetch_verification_link_from_inbox, disposable_email, 120, 8, proxies
            )
            if verification_link:
                await ext_page.goto(verification_link, wait_until="domcontentloaded", timeout=90000)
                logger.success("Переход по ссылке подтверждения Bitwarden")
                await asyncio.sleep(1)
                # Форма "Set a strong password": мастер-пароль, подтверждение, подсказка, Create account
                await ext_page.wait_for_selector("#input-password-form_new-password", timeout=60000)
                await ext_page.fill("#input-password-form_new-password", BITWARDEN_MASTER_PASSWORD)
                await ext_page.fill("#input-password-form_new-password-confirm", BITWARDEN_MASTER_PASSWORD)
                await ext_page.fill("#input-password-form_new-password-hint", BITWARDEN_MASTER_HINT[:50])
                await asyncio.sleep(0.3)
                create_btn = ext_page.locator('button[type="submit"]').filter(has_text="Create account")
                await create_btn.wait_for(state="visible", timeout=20000)
                await create_btn.click()
                logger.success("Bitwarden: введён мастер-пароль, нажато Create account")
                await ext_page.wait_for_selector("text=Bitwarden extension is installed!", timeout=60000)
                logger.success("Bitwarden: расширение установлено")
                await ext_page.close()
                ext_page = await context.new_page()
                login_url = f"chrome-extension://{EXTENSION_POPUP_ID}/popup/index.html#/login"
                await ext_page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
                logger.success("Bitwarden: открыта страница входа")
                await asyncio.sleep(0.5)
                # Ввод почты и Continue
                email_input = ext_page.locator('input[type="email"]').first
                await email_input.wait_for(state="visible", timeout=30000)
                await email_input.fill(disposable_email)
                await ext_page.get_by_role("button", name="Continue").click()
                await asyncio.sleep(0.5)
                # Ввод мастер-пароля и Log in with master password
                await ext_page.wait_for_selector('input[type="password"]', timeout=30000)
                await ext_page.fill('input[type="password"]', BITWARDEN_MASTER_PASSWORD)
                await ext_page.get_by_role("button", name="Log in with master password").click()
                await ext_page.wait_for_selector("text=Your vault is empty", timeout=60000)
                logger.success("Bitwarden: вход выполнен, vault пустой")
                await ext_page.close()
                # На странице Startale (Settings) кликаем Passkey → переход на /settings/passkeys
                passkey_btn = page.locator('button').filter(has_text="Passkey")
                await passkey_btn.first.wait_for(state="visible", timeout=30000)
                await passkey_btn.first.click()
                logger.success("Нажата кнопка Passkey на Startale")
                await asyncio.sleep(1)
                # Дожидаемся появления кнопки "New passkey" (SPA может не менять URL как полная навигация)
                new_passkey_btn = page.locator('button').filter(has_text="New passkey")
                await new_passkey_btn.first.wait_for(state="visible", timeout=45000)
                # Клик New passkey → открывается окно Bitwarden (popout с Fido2Popout в URL)
                await _human_like_click(page, new_passkey_btn.first)
                logger.success("Нажата кнопка New passkey (имитация мыши)")
                # Ждём именно страницу с passkey-popout: ...?uilocation=popout&singleActionPopout=vault_Fido2Popout...
                bitwarden_popup = None
                deadline = time.time() + 30
                while time.time() < deadline:
                    for p in context.pages:
                        u = p.url or ""
                        if "uilocation=popout" in u and "Fido2Popout" in u:
                            bitwarden_popup = p
                            break
                    if bitwarden_popup:
                        break
                    await asyncio.sleep(0.5)
                if not bitwarden_popup:
                    raise RuntimeError(
                        "Не найдено окно Bitwarden passkey (ожидался URL с uilocation=popout и Fido2Popout)"
                    )
                await bitwarden_popup.wait_for_load_state("domcontentloaded", timeout=45000)
                # Кнопка в Bitwarden: RU "Сохранить passkey как новый логин" или EN "Save passkey as new login"
                save_btn_re = re.compile(
                    r"(Сохранить\s+passkey.*новый\s+логин|Save\s+passkey\s+as\s+new\s+login)",
                    re.IGNORECASE,
                )
                save_login_btn = bitwarden_popup.get_by_role("button", name=save_btn_re).first
                await _human_like_click(bitwarden_popup, save_login_btn, timeout=45000)
                logger.success("Нажата кнопка «Сохранить passkey как новый логин» в Bitwarden")
                await asyncio.sleep(1)
                # Ждём засчитывания квеста на портале, затем автоматически отвязываем passkey
                await _wait_quest_done_then_unbind_passkey(page, wallet_address, interval_sec=12)
                db.upsert_account(wallet_address, two_fa_done=True)
            else:
                logger.warning("Ссылка подтверждения не получена из почты. Подтвердите вручную.")
        finally:
            await playwright.stop()

    def run_one(
        self,
        key_index: int = 0,
        wallet_password: str = "Password123",
        use_proxy: bool = True,
        *,
        do_passkey: bool = True,
        do_gm: bool = True,
    ) -> bool:
        """Один запуск на аккаунт в одном браузере: (опционально) passkey, затем (опционально) GM."""
        pid_to_clean: Optional[str] = None
        try:
            private_key = load_private_key(key_index=key_index)
            address = Web3.to_checksum_address(Web3().eth.account.from_key(private_key).address)

            passkey_done = (not do_passkey)
            gm_satisfied = (not do_gm)
            gm_quest_done = False
            gm_should_send = False

            # 1) Passkey: сначала кэш (quest_results), затем портал (если нужно выполнять passkey)
            if do_passkey:
                db.init_db()
                acc = db.get_account_info(address)
                if acc and acc.get("2fa_done"):
                    passkey_done = True
                else:
                    api_result = None
                    max_retries = 3
                    for attempt in range(max_retries):
                        proxies = load_random_proxy()
                        api_result = check_startale_passkey_quest_done(address, proxies)
                        if api_result is not None:
                            break
                        if attempt < max_retries - 1:
                            time.sleep(3)

                    if api_result is True:
                        passkey_done = True
                        db.upsert_account(address, two_fa_done=True)
                    elif api_result is False:
                        passkey_done = False
                        db.upsert_account(address, two_fa_done=False)
                    else:
                        logger.warning(
                            "После {} попыток запрос к порталу (passkey) не удался — продолжаем без обновления кэша.",
                            max_retries,
                        )

            # 2) GM: кэш/портал (5/5 GM), и нужно ли сейчас отправлять GM (по cooldown в quest_results)
            if do_gm:
                db.init_db()
                acc = db.get_account_info(address)
                if acc and acc.get("gm_done"):
                    gm_quest_done = True
                else:
                    gm_api = None
                    max_gm_retries = 3
                    for attempt in range(max_gm_retries):
                        proxies = load_random_proxy()
                        gm_api = check_startale_gm_5_done(address, proxies)
                        if gm_api is not None:
                            break
                        if attempt < max_gm_retries - 1:
                            time.sleep(3)

                    if gm_api is True:
                        gm_quest_done = True
                        db.upsert_account(address, gm_done=True)
                    elif gm_api is False:
                        gm_quest_done = False
                        db.upsert_account(address, gm_done=False)
                    else:
                        logger.warning(
                            "После {} попыток запрос к порталу (5/5 GM) не удался — продолжаем без обновления кэша.",
                            max_gm_retries,
                        )

                gm_should_send = (not gm_quest_done) and db.is_gm_needed_now(address)
                if gm_quest_done:
                    gm_satisfied = True
                elif not gm_should_send:
                    gm_satisfied = True
                    logger.info(
                        "Кошелёк {}: следующий GM ещё не доступен (время не пришло), пропуск.",
                        address,
                    )
                else:
                    gm_satisfied = False

            if passkey_done and gm_satisfied:
                logger.info("Кошелёк {}: passkey и GM уже выполнены/не требуются (портал/кэш), пропуск.", address)
                return True

            logger.info("Кошелёк: {}", address)
            self.create_temp_profile(use_proxy=use_proxy)
            pid_to_clean = self.profile_id
            browser_info = self.start_browser(self.profile_id)
            time.sleep(5)

            cdp = _get_cdp_endpoint(browser_info)
            if not cdp:
                raise RuntimeError("Не удалось получить CDP endpoint от AdsPower")

            try:
                asyncio.run(self._import_wallet(cdp, private_key, password=wallet_password))
                asyncio.run(self._connect_startale(cdp, address, do_passkey=do_passkey and not passkey_done))

                if do_gm and not gm_satisfied:
                    from modules.startalegm import run_gm_on_existing_browser

                    asyncio.run(run_gm_on_existing_browser(cdp, address))
            except Exception as e:
                logger.warning("Ошибка при импорте/подключении: {} — браузер открыт, можно завершить вручную.", e)
        finally:
            if pid_to_clean:
                try:
                    self.stop_browser(pid_to_clean)
                except Exception as e:
                    logger.warning("Закрытие браузера: {} (браузер мог быть уже закрыт)", e)
                try:
                    self.delete_profile(pid_to_clean)
                except Exception as e:
                    logger.error("Не удалось удалить профиль: {}", e)
        return True


def run(*, do_passkey: bool = True, do_gm: bool = True) -> None:
    """Точка входа: обрабатываются все ключи из keys.txt по порядку (в одном браузере на аккаунт)."""
    logging.getLogger("asyncio").setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", message=".*Task was destroyed but it is pending.*")
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO",
    )
    try:
        api_key = load_adspower_api_key()
        n_keys = get_keys_count()
        logger.info("Ключей в keys.txt: {}. Обработка по порядку.", n_keys)
        manager = StartaleGMBrowser(api_key=api_key)
        interrupted = False
        for i in range(n_keys):
            logger.info("——— Ключ {}/{} ———", i + 1, n_keys)
            try:
                manager.run_one(key_index=i, do_passkey=do_passkey, do_gm=do_gm)
            except KeyboardInterrupt:
                interrupted = True
                break
            except Exception as e:
                logger.warning("Ошибка для ключа {}: {} — переходим к следующему.", i, e)
        if not interrupted:
            logger.success("Обработаны все {} ключей.", n_keys)
    except KeyboardInterrupt:
        pass
    except FileNotFoundError as e:
        logger.error(str(e))
        raise SystemExit(1)
    except ValueError as e:
        logger.error(str(e))
        raise SystemExit(1)
