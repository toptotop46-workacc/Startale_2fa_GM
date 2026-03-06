#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Единое хранилище аккаунтов в quest_results.json: 2FA (2fa_done), GM (gm_done, next_gm_available_at, smart_account_created).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = PROJECT_ROOT / "quest_results.json"
# Старый файл GM — при первом чтении данные сливаются в quest_results.json
LEGACY_GM_PATH = PROJECT_ROOT / "startalegm.json"


def _read_data() -> dict[str, Any]:
    """Читает quest_results.json. При наличии startalegm.json сливает данные и удаляет его."""
    data: dict[str, Any] = {}
    if JSON_PATH.exists():
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                data = json.loads(raw)
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, ValueError):
            data = {}

    # Поддержка старого формата: { "accounts": { "0x...": {...} } } из startalegm.json
    if "accounts" in data and isinstance(data["accounts"], dict):
        data = {k: v for k, v in data["accounts"].items() if isinstance(k, str) and k.startswith("0x")}
    elif any(not k.startswith("0x") for k in data if isinstance(k, str)):
        data = {k: v for k, v in data.items() if isinstance(k, str) and k.startswith("0x")}

    # Миграция из startalegm.json
    if LEGACY_GM_PATH.exists():
        try:
            with open(LEGACY_GM_PATH, "r", encoding="utf-8") as f:
                legacy = json.load(f)
            accs = legacy.get("accounts") if isinstance(legacy, dict) else {}
            if isinstance(accs, dict):
                for addr, rec in accs.items():
                    if not isinstance(rec, dict) or not (addr.startswith("0x")):
                        continue
                    if addr not in data:
                        data[addr] = {}
                    data[addr].setdefault("2fa_done", False)
                    data[addr].setdefault("gm_done", False)
                    data[addr]["next_gm_available_at"] = rec.get("next_gm_available_at")
                    data[addr]["smart_account_created"] = rec.get("smart_account_created", False)
                    if "updated_at" in rec:
                        data[addr]["updated_at"] = rec["updated_at"]
            _write_data(data)
            LEGACY_GM_PATH.unlink()
        except Exception:
            pass

    return data


def _write_data(data: dict[str, Any]) -> None:
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def init_db() -> None:
    """Создаёт quest_results.json с пустым объектом, если файла нет."""
    if not JSON_PATH.exists():
        _write_data({})


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_account_info(eoa_address: str) -> Optional[dict]:
    """Возвращает запись по адресу или None."""
    init_db()
    data = _read_data()
    if eoa_address not in data:
        return None
    rec = dict(data[eoa_address])
    rec["2fa_done"] = bool(rec.get("2fa_done", False))
    rec["gm_done"] = bool(rec.get("gm_done", False))
    rec["smart_account_created"] = bool(rec.get("smart_account_created", False))
    return rec


def upsert_account(
    eoa_address: str,
    *,
    two_fa_done: Optional[bool] = None,
    gm_done: Optional[bool] = None,
    next_gm_available_at: Optional[datetime] = None,
    smart_account_created: Optional[bool] = None,
) -> None:
    """Обновляет запись по EOA. Переданные None не меняют поле."""
    init_db()
    data = _read_data()
    now = _now_utc()
    if eoa_address not in data:
        data[eoa_address] = {
            "2fa_done": two_fa_done if two_fa_done is not None else False,
            "gm_done": gm_done if gm_done is not None else False,
            "next_gm_available_at": next_gm_available_at.isoformat() if next_gm_available_at else None,
            "smart_account_created": bool(smart_account_created) if smart_account_created is not None else False,
            "updated_at": now,
        }
    else:
        rec = data[eoa_address]
        if two_fa_done is not None:
            rec["2fa_done"] = bool(two_fa_done)
        if gm_done is not None:
            rec["gm_done"] = bool(gm_done)
        if next_gm_available_at is not None:
            rec["next_gm_available_at"] = next_gm_available_at.isoformat()
        if smart_account_created is not None:
            rec["smart_account_created"] = bool(smart_account_created)
        rec["updated_at"] = now
    _write_data(data)


def get_all_addresses() -> list[str]:
    return list(_read_data().keys())


def get_accounts_due_for_gm(known_addresses: list[str]) -> list[str]:
    """
    Адреса, для которых пора отправить GM:
    next_gm_available_at отсутствует/null или next_gm_available_at <= now (UTC).
    """
    init_db()
    data = _read_data()
    now_utc = datetime.now(timezone.utc)
    due = []
    for addr in known_addresses:
        rec = data.get(addr)
        if rec is None:
            due.append(addr)
            continue
        next_at_str = rec.get("next_gm_available_at")
        if next_at_str is None:
            due.append(addr)
            continue
        try:
            next_at = datetime.fromisoformat(next_at_str.replace("Z", "+00:00"))
            if next_at <= now_utc:
                due.append(addr)
        except (ValueError, TypeError):
            due.append(addr)
    return due


def is_gm_needed_now(eoa_address: str) -> bool:
    """
    True, если для аккаунта нужно сейчас отправить GM:
    нет next_gm_available_at или next_gm_available_at <= now.
    """
    rec = get_account_info(eoa_address)
    if rec is None:
        return True
    next_at_str = rec.get("next_gm_available_at")
    if next_at_str is None:
        return True
    try:
        next_at = datetime.fromisoformat(next_at_str.replace("Z", "+00:00"))
        return next_at <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True
