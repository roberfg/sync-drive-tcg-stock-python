"""Verifica el inicio de sesión y el vaciado del carrito de la tienda."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Browser, sync_playwright
from screeninfo import get_monitors

from sync_stock import clear_cart, login_if_needed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--headed", action="store_true", default=True)
    parser.add_argument(
        "--monitor",
        type=int,
        default=int(os.getenv("STORE_BROWSER_MONITOR", "2")),
    )
    parser.add_argument(
        "--auth-file",
        default="playwright/.auth/store.json",
    )
    parser.add_argument(
        "--solve-challenge",
        action="store_true",
        help="Permite resolver AWS WAF manualmente.",
    )
    return parser.parse_args()


def verify_logged_in(page) -> None:
    password_fields = page.locator('input[type="password"]')
    if password_fields.count() > 0 and password_fields.first.is_visible():
        raise RuntimeError("La sesión no se inició: el formulario de login sigue visible.")

    success_selector = os.getenv("STORE_LOGIN_SUCCESS_SELECTOR")
    if success_selector and page.locator(success_selector).count() == 0:
        raise RuntimeError(
            "No se encontró STORE_LOGIN_SUCCESS_SELECTOR después del login."
        )

    print("Login verificado.")


def verify_empty_cart(page) -> None:
    confirmation_selector = os.getenv("STORE_EMPTY_CART_CONFIRM_SELECTOR")
    if confirmation_selector:
        if page.locator(confirmation_selector).count() == 0:
            raise RuntimeError("No se encontró la confirmación de carrito vacío.")
    else:
        body = page.locator("body").inner_text().lower()
        if "your shopping cart is empty" not in body and "cart is empty" not in body:
            raise RuntimeError("No se pudo confirmar que el carrito quedó vacío.")
    print("Carrito vacío verificado.")


def main() -> None:
    load_dotenv()
    arguments = parse_args()
    if not os.getenv("STORE_USERNAME") or not os.getenv("STORE_PASSWORD"):
        raise RuntimeError("Faltan STORE_USERNAME o STORE_PASSWORD en .env.")
    browser_args: list[str] = []
    monitors = get_monitors()
    monitor_index = arguments.monitor - 1
    if 0 <= monitor_index < len(monitors):
        monitor = monitors[monitor_index]
        browser_args = [
            f"--window-position={monitor.x},{monitor.y}",
            f"--window-size={monitor.width},{monitor.height}",
        ]

    auth_path = Path(arguments.auth_file)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(headless=False, args=browser_args)
        context = browser.new_context(
            storage_state=str(auth_path) if auth_path.exists() else None
        )
        page = context.new_page()
        login_if_needed(page, arguments.solve_challenge)
        verify_logged_in(page)
        clear_cart(page, arguments.solve_challenge, float(os.getenv("STORE_RETRY_DELAY", "5")))
        verify_empty_cart(page)
        context.storage_state(path=str(auth_path))
        browser.close()


if __name__ == "__main__":
    main()
