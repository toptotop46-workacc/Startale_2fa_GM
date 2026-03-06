#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Общая работа с API портала Soneium (bonus-dapp): проверка квестов startale_7.
"""

from __future__ import annotations

from typing import Any, List, Optional

import requests

SONEIUM_BONUS_URL = "https://portal.soneium.org/api/profile/bonus-dapp"
SONEIUM_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ru,ru-RU;q=0.9,en-US;q=0.8,en;q=0.7",
    "dnt": "1",
    "priority": "u=1, i",
    "referer": "https://portal.soneium.org/en/profile/",
    "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
}


def get_bonus_dapp_data(address: str, proxies: Optional[dict] = None) -> Optional[List[Any]]:
    """
    Запрос к portal.soneium.org/api/profile/bonus-dapp.
    Возвращает список dapp-объектов или None при ошибке.
    """
    url = f"{SONEIUM_BONUS_URL}?address={address}"
    try:
        r = requests.get(
            url,
            headers=SONEIUM_HEADERS,
            proxies=proxies,
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return data


def check_startale_passkey_quest_done(address: str, proxies: Optional[dict] = None) -> Optional[bool]:
    """
    Проверяет, выполнено ли задание «Set up Passkey or social recovery» в квесте startale_7.
    Возвращает True/False при успешном ответе, None при ошибке.
    """
    data = get_bonus_dapp_data(address, proxies)
    if not data:
        return None
    for item in data:
        if item.get("id") != "startale_7":
            continue
        quests = item.get("quests") or []
        for q in quests:
            desc = (q.get("description") or "").lower()
            if "passkey" in desc or "social recovery" in desc:
                return bool(q.get("isDone"))
    return False


def check_startale_gm_5_done(address: str, proxies: Optional[dict] = None) -> Optional[bool]:
    """
    Проверяет, выполнено ли задание «Send Daily GM 5 times after opt in» в квесте startale_7 (5/5 GM).
    Возвращает True — выполнено, False — не выполнено, None — ошибка запроса.
    """
    data = get_bonus_dapp_data(address, proxies)
    if not data:
        return None
    for item in data:
        if item.get("id") != "startale_7":
            continue
        quests = item.get("quests") or []
        for q in quests:
            desc = (q.get("description") or "")
            if "Send Daily GM" in desc or ("Daily GM" in desc and q.get("required") == 5):
                return bool(q.get("isDone"))
    return False
