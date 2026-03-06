#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Startale 2FA (passkey) + GM. Default: both in one browser per account."
    )
    parser.add_argument(
        "--2fa-only",
        dest="twofa_only",
        action="store_true",
        help="Run only 2FA (passkey) for all keys, without GM.",
    )
    parser.add_argument(
        "--gm-only",
        dest="gm_only",
        action="store_true",
        help="Run only GM for all keys (without passkey).",
    )
    args = parser.parse_args()

    if args.twofa_only and args.gm_only:
        print("Cannot use both --2fa-only and --gm-only.", file=sys.stderr)
        sys.exit(1)

    from modules.startale2fa import run as startale2fa_run
    if args.gm_only:
        # Только GM: без выполнения passkey, но в одном браузере на аккаунт.
        startale2fa_run(do_passkey=False, do_gm=True)
        return

    # По умолчанию: passkey + GM в одном браузере на аккаунт (если GM нужен).
    startale2fa_run(do_passkey=True, do_gm=not args.twofa_only)


if __name__ == "__main__":
    main()
