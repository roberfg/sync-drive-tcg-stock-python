"""Sincroniza stock y precios de una tabla de Google Sheets con la tienda web."""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import Browser, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from screeninfo import get_monitors
from sheet_config import resolve_sheet_id
import os


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_FILE = "utopian-calling-274800-552c65952619.json"
FINAL_CART_URL = "https://www.mtgmintcard.com/shopping-cart"
SHEET_RANGE = "A:Z"
CARD_URL_COLUMN = 5
ACTUAL_QUANTITY_COLUMN = 0
REQUESTED_QUANTITY_COLUMN = 1
NAME_COLUMN = 2
UNIT_PRICE_COLUMN = 3
TOTAL_PRICE_COLUMN = 4


@dataclass
class CardRow:
    row_number: int
    quantity: int
    name: str
    original_unit_price: float | None
    url: str


@dataclass
class CardResult:
    available: int | None
    price: float | None
    error: str | None = None
    in_cart: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Modifica Sheets y el carrito.")
    parser.add_argument(
        "--update-sheet",
        action="store_true",
        help="Actualiza precios, cantidades y colores, sin tocar el carrito.",
    )
    parser.add_argument(
        "--stock-only",
        action="store_true",
        help="Actualiza únicamente cantidad y color de stock; no modifica precios.",
    )
    parser.add_argument("--sheet-url", default=os.getenv("GOOGLE_SHEET_URL"))
    parser.add_argument("--sheet-id", default=os.getenv("GOOGLE_SHEET_ID"))
    parser.add_argument(
        "--credentials",
        default=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", CREDENTIALS_FILE),
    )
    parser.add_argument("--headed", action="store_true", help="Muestra el navegador.")
    parser.add_argument("--auth-file", default="playwright/.auth/store.json")
    parser.add_argument(
        "--solve-challenge",
        action="store_true",
        help="Espera a que resuelvas manualmente un desafío de la tienda.",
    )
    parser.add_argument(
        "--probe-stock",
        action="store_true",
        help="Activa el control Add to Cart para revelar el stock de la carta.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limita la prueba a las primeras filas de la tabla.",
    )
    parser.add_argument(
        "--monitor",
        type=int,
        default=int(os.getenv("STORE_BROWSER_MONITOR", "2")),
        help="Número de monitor para el navegador visible, empezando en 1.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=float(os.getenv("STORE_RETRY_DELAY", "5")),
        help="Segundos entre reintentos de una página que no cargó correctamente.",
    )
    parser.add_argument(
        "--cart-action-delay",
        type=float,
        default=float(os.getenv("STORE_CART_ACTION_DELAY", "3")),
        help="Segundos de espera después de una acción del carrito.",
    )
    parser.add_argument(
        "--close-browser",
        action="store_true",
        help="Cierra el navegador al terminar; por defecto queda abierto en modo visible.",
    )
    return parser.parse_args()


def parse_number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value).replace(",", "."))
    return float(match.group(0)) if match else None


def parse_quantity(value: Any) -> int:
    number = parse_number(value)
    return max(0, int(number or 0))


def column_letter(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def load_sheet(service: Any, sheet_id: str) -> tuple[str, list[CardRow]]:
    metadata = service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    sheet = metadata["sheets"][0]
    title = sheet["properties"]["title"]
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"'{title}'!{SHEET_RANGE}")
        .execute()
    )

    rows: list[CardRow] = []
    for row_number, values in enumerate(response.get("values", []), start=1):
        # La primera fila contiene los encabezados de la tabla.
        if row_number == 1:
            continue
        if len(values) <= CARD_URL_COLUMN:
            continue
        url = str(values[CARD_URL_COLUMN]).strip()
        if not url.startswith(("http://", "https://")):
            continue
        rows.append(
            CardRow(
                row_number=row_number,
                quantity=parse_quantity(
                    values[REQUESTED_QUANTITY_COLUMN]
                    if len(values) > REQUESTED_QUANTITY_COLUMN
                    else 0
                ),
                name=str(values[NAME_COLUMN]) if len(values) > NAME_COLUMN else url,
                original_unit_price=parse_number(
                    values[UNIT_PRICE_COLUMN] if len(values) > UNIT_PRICE_COLUMN else None
                ),
                url=url,
            )
        )
    return title, rows


def is_waf_challenge(page: Page) -> bool:
    try:
        content = page.content()
        body_text = page.locator("body").inner_text().strip()
    except Exception:
        # AWS WAF puede reemplazar el documento mientras se obtiene el contenido.
        return False
    return "awsWafCookieDomainList" in content or (
        len(content) < 3000 and not body_text
    )


def wait_for_waf(page: Page, solve_challenge: bool) -> None:
    if not is_waf_challenge(page):
        return
    if not solve_challenge:
        raise RuntimeError(
            "La tienda devolvió un desafío AWS WAF. Ejecuta con --headed --solve-challenge "
            "y resuélvelo manualmente en el navegador."
        )
    print("Resuelve el desafío de seguridad en el navegador visible.")
    input("Pulsa Enter cuando la tienda se haya cargado correctamente... ")
    if is_waf_challenge(page):
        raise RuntimeError("El desafío AWS WAF sigue activo.")


def first_visible(locator: Any) -> Any:
    """Devuelve el primer elemento visible de un locator."""
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    return locator.first


def wait_until(predicate: Any, timeout: float = 15, interval: float = 0.1) -> bool:
    """Espera una condición de página sin usar una pausa fija de acción."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def wait_for_card_state(page: Page, timeout: float = 15) -> bool:
    """Espera precio, stock agotado o el control de compra de la carta."""
    return wait_until(
        lambda: (
            page_is_out_of_stock(page)
            or read_main_product_price(page) is not None
            or find_add_to_cart(page).count() > 0
        ),
        timeout=timeout,
    )


def wait_for_cart_quantity(page: Page, expected: int, timeout: float = 15) -> bool:
    """Espera que la página refleje la cantidad total esperada en el carrito."""
    return wait_until(lambda: read_in_cart(page) >= expected, timeout=timeout)


def login_if_needed(page: Page, solve_challenge: bool) -> None:
    username = os.getenv("STORE_USERNAME")
    password = os.getenv("STORE_PASSWORD")
    login_url = os.getenv("STORE_LOGIN_URL")
    if not username or not password:
        return
    if login_url:
        page.goto(login_url, wait_until="domcontentloaded")
    else:
        page.goto("https://www.mtgmintcard.com", wait_until="domcontentloaded")
    wait_for_waf(page, solve_challenge)
    page.wait_for_load_state("domcontentloaded")

    user_selector = os.getenv(
        "STORE_USERNAME_SELECTOR",
        'input[type="email"], input[name*="user" i], input[name*="email" i]',
    )
    password_selector = os.getenv(
        "STORE_PASSWORD_SELECTOR",
        'input[type="password"]',
    )
    submit_selector = os.getenv(
        "STORE_LOGIN_SUBMIT_SELECTOR",
        'button[type="submit"], input[type="submit"]',
    )
    user = first_visible(page.locator(user_selector))
    secret = first_visible(page.locator(password_selector))
    if user.count() == 0 or secret.count() == 0:
        print("Sesión guardada detectada; se reutilizará sin volver a iniciar sesión.")
        return
    user.fill(username)
    secret.fill(password)
    submit = page.locator(submit_selector).first
    if submit.count() == 0:
        raise RuntimeError(
            "No se encontró el botón de inicio de sesión. "
            "Configura STORE_LOGIN_SUBMIT_SELECTOR."
        )
    submit.click()
    page.wait_for_load_state("domcontentloaded")
    wait_until(
        lambda: (
            first_visible(page.locator(password_selector)).count() == 0
            or not first_visible(page.locator(password_selector)).is_visible()
        )
    )
    remaining_password = first_visible(page.locator(password_selector))
    if remaining_password.count() > 0 and remaining_password.is_visible():
        raise RuntimeError("El usuario o la contraseña fueron rechazados por la tienda.")


def extract_price(text: str) -> float | None:
    matches = re.findall(
        r"(?:\$|USD\s*)\s*(\d+(?:[.,]\d{1,2})?)",
        text,
        re.IGNORECASE,
    )
    values = [float(item.replace(",", ".")) for item in matches]
    return values[0] if values else None


def read_main_product_price(page: Page) -> float | None:
    """Lee el primer precio del bloque de compra de la carta principal."""
    add_button = find_add_to_cart(page)
    if add_button.count() == 0:
        return None
    block_text = add_button.evaluate(
        """
        (element) => {
            let node = element;
            for (let level = 0; level < 8 && node; level += 1, node = node.parentElement) {
                const text = (node.innerText || '').trim();
                if (/add\\s+to\\s+cart/i.test(text) && /\\$\\s*\\d/.test(text) && text.length < 700) {
                    return text;
                }
            }
            return '';
        }
        """
    )
    return extract_price(block_text)


def find_structured_price(value: Any) -> float | None:
    if isinstance(value, dict):
        if "price" in value:
            parsed = parse_number(value["price"])
            if parsed is not None:
                return parsed
        for child in value.values():
            found = find_structured_price(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_structured_price(child)
            if found is not None:
                return found
    return None


def extract_stock(text: str) -> int | None:
    patterns = [
        r"(?:in stock|available|stock)\D{0,20}(\d+)",
        r"(\d+)\D{0,20}(?:in stock|available|units)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def read_in_cart(page: Page) -> int:
    """Lee la cantidad ya agregada de la carta principal."""
    cart_pattern = re.compile(r"^\s*(\d+)\s+in\s+Cart\s*$", re.IGNORECASE)
    controls = page.locator('button, a, [role="button"]')
    for index in range(controls.count()):
        item = controls.nth(index)
        if not item.is_visible():
            continue
        match = cart_pattern.match(" ".join(item.inner_text().split()))
        if match:
            return int(match.group(1))

    locator = page.get_by_text(cart_pattern)
    for index in range(locator.count()):
        item = locator.nth(index)
        if not item.is_visible():
            continue
        match = cart_pattern.match(" ".join(item.inner_text().split()))
        if match:
            return int(match.group(1))
    return 0


def read_product_stock(page: Page) -> int | None:
    """Lee únicamente el selector de cantidad del producto principal."""
    grid_stock = read_quantity_grid_stock(page)
    if grid_stock is not None:
        return grid_stock
    visible_menu_stock = read_visible_quantity_menu(page)
    if visible_menu_stock is not None:
        return visible_menu_stock

    product_form = page.locator("form").filter(
        has_text=re.compile(r"Add to Cart|Select Qty|Out of Stock", re.IGNORECASE)
    ).first
    has_product_form = product_form.count() > 0
    product_scope = product_form if has_product_form else page

    quantity_select = find_quantity_select(product_scope)
    if quantity_select.count() == 0 and has_product_form:
        quantity_select = find_quantity_select(page)
    if quantity_select.count() > 0:
        options = quantity_select.locator("option:not([disabled])")
        option_values = []
        for index in range(options.count()):
            option = options.nth(index)
            option_values.extend([
                parse_quantity(option.get_attribute("value")),
                parse_quantity(option.inner_text()),
            ])
        positive_values = [value for value in option_values if value > 0]
        return max(positive_values) if positive_values else 0

    quantity_inputs = product_scope.locator(
        'input[name*="quantity" i], input[id*="quantity" i], input[name*="qty" i], input[id*="qty" i]'
    )
    if quantity_inputs.count() > 0:
        maximum = parse_quantity(quantity_inputs.first.get_attribute("max"))
        if maximum > 0:
            return maximum

    if has_product_form and re.search(
        r"Out of Stock", product_form.inner_text(), re.IGNORECASE
    ):
        return 0
    return None


def read_quantity_grid_stock(page: Page) -> int | None:
    """Lee el stock del menú real de la tienda (#selectable)."""
    options = page.locator("#selectable li.ui-state-default")
    visible_values = []
    for index in range(options.count()):
        option = options.nth(index)
        if not option.is_visible():
            continue
        value = parse_quantity(option.inner_text())
        if option.inner_text().strip().isdigit():
            visible_values.append(value)
    if visible_values:
        return max(visible_values)
    return 0 if page.locator("#selectable li.select-qty").count() > 0 and page.locator("#selectable li.select-qty").first.is_visible() else None


def quantity_grid_is_visible(page: Page) -> bool:
    """Indica si la cuadrícula de cantidades ya está abierta."""
    markers = page.locator("#selectable li.select-qty")
    return any(
        markers.nth(index).is_visible()
        for index in range(markers.count())
    )


def wait_for_quantity_grid(page: Page, timeout: float = 10) -> bool:
    """Espera a que aparezca la cuadrícula real de cantidades."""
    try:
        page.wait_for_function(
            """
            () => Array.from(document.querySelectorAll('#selectable li.select-qty'))
                .some((element) => {
                    const style = window.getComputedStyle(element);
                    return style.display !== 'none' && style.visibility !== 'hidden';
                })
            """,
            timeout=int(timeout * 1000),
        )
        return True
    except Exception:
        return quantity_grid_is_visible(page)


def find_quantity_select(scope: Any) -> Any:
    """Encuentra un selector cuyo contenido contiene las cantidades de compra."""
    selects = scope.locator("select")
    fallback = selects.first
    for index in range(selects.count()):
        candidate = selects.nth(index)
        option_text = " ".join(
            candidate.locator("option").all_text_contents()
        ).lower()
        if "select qty" in option_text:
            if candidate.is_visible():
                return candidate
            if fallback.count() == 0:
                fallback = candidate
    return fallback if fallback.count() > 0 and "select qty" in " ".join(
        fallback.locator("option").all_text_contents()
    ).lower() else page_empty_locator(scope)


def page_empty_locator(scope: Any) -> Any:
    """Devuelve un locator vacío manteniendo el contexto de Playwright."""
    return scope.locator('select[data-stock-selector-that-does-not-exist="1"]')


def read_visible_quantity_menu(page: Page) -> int | None:
    """Lee las opciones numéricas del menú visible tras pulsar Add to Cart."""
    candidates = page.get_by_text("Select Qty", exact=True)
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if not candidate.is_visible():
            continue
        values = candidate.evaluate(
            """
            (element) => {
                let node = element;
                for (let level = 0; level < 8 && node; level += 1, node = node.parentElement) {
                    const lines = (node.innerText || '')
                        .split(/\\n|\\r/)
                        .map((line) => line.trim())
                        .filter(Boolean);
                    const marker = lines.findIndex((line) => line.toLowerCase() === 'select qty');
                    if (marker < 0) continue;
                    const values = [];
                    for (const line of lines.slice(marker + 1)) {
                        if (!/^\\d+$/.test(line)) break;
                        values.push(Number(line));
                    }
                    if (values.length > 0) return values;
                }
                return [];
            }
            """
        )
        positive_values = [int(value) for value in values if int(value) > 0]
        if positive_values:
            return max(positive_values)
        if values:
            return 0
    return None


def select_visible_quantity_menu(page: Page, quantity: int) -> bool:
    """Selecciona una cantidad visible; esa selección confirma el carrito."""
    grids = page.locator("#selectable")
    for grid_index in range(grids.count()):
        grid = grids.nth(grid_index)
        if not grid.is_visible():
            continue
        grid_options = grid.locator("li.ui-state-default")
        for index in range(grid_options.count()):
            option = grid_options.nth(index)
            if option.is_visible() and option.inner_text().strip() == str(quantity):
                option.click()
                return True

    candidates = page.get_by_text("Select Qty", exact=True)
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if not candidate.is_visible():
            continue
        marker_box = candidate.bounding_box()
        if marker_box:
            quantity_candidates = page.get_by_text(str(quantity), exact=True)
            nearby: list[tuple[float, Any]] = []
            marker_x = marker_box["x"] + marker_box["width"] / 2
            marker_y = marker_box["y"] + marker_box["height"] / 2
            for quantity_index in range(quantity_candidates.count()):
                option = quantity_candidates.nth(quantity_index)
                if not option.is_visible():
                    continue
                option_box = option.bounding_box()
                if not option_box:
                    continue
                option_x = option_box["x"] + option_box["width"] / 2
                option_y = option_box["y"] + option_box["height"] / 2
                distance = ((option_x - marker_x) ** 2 + (option_y - marker_y) ** 2) ** 0.5
                nearby.append((distance, option))
            if nearby:
                distance, option = min(nearby, key=lambda item: item[0])
                if distance < 400:
                    option.click()
                    return True
        selected = candidate.evaluate(
            """
            (element, target) => {
                let node = element;
                for (let level = 0; level < 8 && node; level += 1, node = node.parentElement) {
                    const lines = (node.innerText || '')
                        .split(/\\n|\\r/)
                        .map((line) => line.trim())
                        .filter(Boolean);
                    const marker = lines.findIndex((line) => line.toLowerCase() === 'select qty');
                    const following = marker >= 0 ? lines.slice(marker + 1) : [];
                    if (!following.includes(String(target))) continue;
                    const elements = Array.from(node.querySelectorAll('*'));
                    const option = elements.find((item) => {
                        const style = window.getComputedStyle(item);
                        return style.display !== 'none' && style.visibility !== 'hidden'
                            && (item.innerText || '').trim() === String(target);
                    });
                    if (option) {
                        option.click();
                        return true;
                    }
                }
                return false;
            }
            """,
            quantity,
        )
        if selected:
            return True
    return False


def page_is_out_of_stock(page: Page) -> bool:
    """Detecta el estado sin stock visible de la carta principal."""
    if find_add_to_cart(page).count() > 0:
        return False
    out_of_stock = page.get_by_text("Out of Stock", exact=True)
    return any(
        out_of_stock.nth(index).is_visible()
        for index in range(out_of_stock.count())
    )


def print_stock_controls(page: Page) -> None:
    """Muestra controles relevantes para diagnosticar cambios del sitio."""
    selects = page.locator("select")
    print(f"DIAGNOSTICO | URL={page.url} | selectores={selects.count()}")
    for index in range(selects.count()):
        select = selects.nth(index)
        print(
            f"DIAGNOSTICO | select[{index}] visible={select.is_visible()} "
            f"name={select.get_attribute('name')} id={select.get_attribute('id')} "
            f"class={select.get_attribute('class')} "
            f"opciones={select.locator('option').all_text_contents()}"
        )
    for index in range(page.locator("input").count()):
        input_element = page.locator("input").nth(index)
        input_type = input_element.get_attribute("type") or ""
        input_name = input_element.get_attribute("name") or ""
        input_id = input_element.get_attribute("id") or ""
        if any(value in f"{input_type} {input_name} {input_id}".lower() for value in ["qty", "quantity", "cart"]):
            print(
                f"DIAGNOSTICO | input[{index}] visible={input_element.is_visible()} "
                f"type={input_type} name={input_name} id={input_id} "
                f"max={input_element.get_attribute('max')} value={input_element.get_attribute('value')}"
            )
    add_text = page.get_by_text("Add to Cart", exact=True)
    print(f"DIAGNOSTICO | elementos Add to Cart={add_text.count()}")
    for index in range(min(add_text.count(), 5)):
        element = add_text.nth(index)
        print(
            f"DIAGNOSTICO | add[{index}] visible={element.is_visible()} "
            f"tag={element.evaluate('(node) => node.tagName')} "
            f"html={element.evaluate('(node) => node.outerHTML')[:500]}"
        )


def find_add_to_cart(scope: Any) -> Any:
    selectors = [
        'button:has-text("Add to Cart"), button:has-text("in Cart")',
        'a:has-text("Add to Cart"), a:has-text("in Cart")',
        'input[type="submit"][value*="Add" i]',
    ]
    for selector in selectors:
        candidates = scope.locator(selector)
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if candidate.is_visible():
                return candidate
    candidates = scope.get_by_text(
        re.compile(r"^\s*(Add to Cart|\d+\s+in\s+Cart)\s*$", re.IGNORECASE)
    )
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if candidate.is_visible():
            return candidate
    return candidates.first


def reveal_stock(page: Page) -> None:
    """Pulsa Add to Cart solo para que la página muestre el selector de stock."""
    product_form = page.locator("form").filter(
        has_text=re.compile(r"Add to Cart|\d+\s+in\s+Cart", re.IGNORECASE)
    ).first
    button_scope = product_form if product_form.count() > 0 else page
    add_button = find_add_to_cart(button_scope)
    if add_button.count() == 0 and button_scope != page:
        add_button = find_add_to_cart(page)
    if add_button.count() == 0:
        raise RuntimeError("No se encontró el botón Add to Cart de la carta principal.")
    add_button.click()
    if not wait_for_quantity_grid(page):
        raise RuntimeError("No apareció la cuadrícula de cantidades.")


def extract_page_data(page: Page, probe_stock: bool = False) -> tuple[float | None, int | None, int]:
    """Obtiene precio y stock desde metadatos, controles y texto de la página."""
    price: float | None = read_main_product_price(page)

    # El precio estructurado representa el precio del producto, no cualquier importe visible.
    for script in page.locator('script[type="application/ld+json"]').all() if price is None else []:
        try:
            structured = json.loads(script.text_content() or "")
        except json.JSONDecodeError:
            continue
        price = find_structured_price(structured)
        if price is not None:
            break

    if price is None:
        for selector in [
            'meta[property="product:price:amount"]',
            'meta[itemprop="price"]',
            '[itemprop="price"]',
        ]:
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            element = locator.first
            price = parse_number(
                element.get_attribute("content") or element.inner_text()
            )
            if price is not None:
                break

    custom_price_selector = os.getenv("STORE_PRICE_SELECTOR")
    if price is None and custom_price_selector:
        locator = page.locator(custom_price_selector).first
        if locator.count() > 0:
            price = parse_number(locator.inner_text())

    body_text = page.locator("body").inner_text()
    if price is None:
        labeled_price = re.search(
            r"(?:our\s+price|sale\s+price|price)\s*:?\s*(?:USD\s*)?\$?\s*(\d+(?:[.,]\d{1,2})?)",
            body_text,
            re.IGNORECASE,
        )
        price = parse_number(labeled_price.group(1)) if labeled_price else None

    in_cart = read_in_cart(page)
    stock = read_product_stock(page)
    if stock is None and page_is_out_of_stock(page):
        stock = 0
    if stock is None and probe_stock:
        reveal_stock(page)
        revealed_stock = read_visible_quantity_menu(page)
        stock = revealed_stock if revealed_stock is not None else read_product_stock(page)
        if stock is None:
            print_stock_controls(page)
    if stock is None:
        stock = extract_stock(body_text)
    return price, stock, in_cart


def inspect_card(
    page: Page,
    card: CardRow,
    solve_challenge: bool,
    probe_stock: bool,
    retry_delay: float,
) -> CardResult:
    """Lee una carta y reintenta indefinidamente si la página falla."""
    attempt = 0
    while True:
        attempt += 1
        try:
            page.goto(card.url, wait_until="domcontentloaded")
            wait_for_waf(page, solve_challenge)
            if not wait_for_card_state(page):
                raise RuntimeError("La carta no terminó de cargar sus controles.")
            price, stock, in_cart = extract_page_data(page, probe_stock)
            if price is None and stock is None:
                raise RuntimeError("No se encontraron precio ni stock en la página.")
            warning = None
            if price is None:
                warning = "No se encontró el precio en la página."
            elif stock is None:
                warning = "No se encontró la cantidad disponible en la página."
            return CardResult(stock, price, warning, in_cart)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(
                f"REINTENTO | fila {card.row_number} | {card.name} | "
                f"intento={attempt} | error={error} | "
                f"esperando={retry_delay:g}s"
            )
            time.sleep(max(0.5, retry_delay))


def clear_cart(
    page: Page,
    solve_challenge: bool = False,
    retry_delay: float = 5,
    action_delay: float = 3,
) -> None:
    cart_url = os.getenv("STORE_CART_URL", "https://www.mtgmintcard.com/cart")
    empty_selector = os.getenv("STORE_EMPTY_CART_SELECTOR")
    attempt = 0
    while True:
        attempt += 1
        try:
            page.goto(cart_url, wait_until="domcontentloaded")
            wait_for_waf(page, solve_challenge)
            if not wait_until(
                lambda: "your shopping cart" in page.locator("body").inner_text().lower()
                or "cart" in page.locator("body").inner_text().lower(),
            ):
                raise RuntimeError("El carrito no terminó de cargar.")

            body_text = page.locator("body").inner_text().lower()
            if "your shopping cart is empty" in body_text or "cart is empty" in body_text:
                print("Carrito ya vacío; no es necesario vaciarlo.")
                return

            if empty_selector:
                button = first_visible(page.locator(empty_selector))
            else:
                button = first_visible(page.get_by_role(
                    "button",
                    name=re.compile(
                        r"Empty Cart|Clear Cart|Remove All|Empty Shopping Cart|Remove All Items",
                        re.IGNORECASE,
                    ),
                ))
                if button.count() == 0:
                    button = first_visible(page.get_by_text(
                        re.compile(
                            r"^\s*(Empty Cart|Clear Cart|Remove All|Empty Shopping Cart|Remove All Items)\s*$",
                            re.IGNORECASE,
                        )
                    ))
                if button.count() == 0:
                    button = first_visible(page.locator(
                        'a[href*="empty" i], a[href*="clear" i], '
                        'input[value*="empty" i], input[value*="clear" i], '
                        'button[name*="empty" i], button[name*="clear" i]'
                    ))
            if button.count() == 0 or not button.is_visible():
                raise RuntimeError("No apareció el control para vaciar el carrito.")
            page.once("dialog", lambda dialog: dialog.accept())
            button.click()
            wait_until(
                lambda: page.locator('[role="dialog"], .modal, .dialog').count() > 0
                or "your shopping cart is empty" in page.locator("body").inner_text().lower()
                or "cart is empty" in page.locator("body").inner_text().lower(),
                timeout=max(5, action_delay + 2),
            )
            confirm_selector = os.getenv("STORE_EMPTY_CART_CONFIRM_SELECTOR")
            if confirm_selector:
                confirm_button = first_visible(page.locator(confirm_selector))
            else:
                dialog_scope = page.locator('[role="dialog"], .modal, .dialog').last
                confirm_button = first_visible(dialog_scope.get_by_role(
                    "button",
                    name=re.compile(r"Confirm|Yes|OK|Remove All|Empty Cart", re.IGNORECASE),
                ))
            if confirm_button.count() > 0 and confirm_button.is_visible():
                confirm_button.click()
                wait_until(
                    lambda: "your shopping cart is empty" in page.locator("body").inner_text().lower()
                    or "cart is empty" in page.locator("body").inner_text().lower(),
                    timeout=max(5, action_delay + 2),
                )
            page.wait_for_load_state("domcontentloaded")
            final_body = page.locator("body").inner_text().lower()
            if "your shopping cart is empty" not in final_body and "cart is empty" not in final_body:
                raise RuntimeError("El carrito no confirmó el vaciado.")
            print("Carrito vaciado y confirmado.")
            return
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(
                f"REINTENTO CARRITO | intento={attempt} | error={error} | "
                f"esperando={retry_delay:g}s"
            )
            time.sleep(max(0.5, retry_delay))


def keep_browser_open() -> None:
    """Mantiene viva la sesión visible para permitir trabajo manual."""
    print("Navegador mantenido abierto. Pulsa Ctrl+C cuando quieras finalizar el proceso.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("Proceso detenido manualmente; se conserva la ventana del navegador mientras siga activa.")


def open_final_cart(page: Page, solve_challenge: bool, retry_delay: float) -> None:
    """Abre el carrito final antes de dejar el navegador para uso manual."""
    attempt = 0
    while True:
        attempt += 1
        try:
            page.goto(FINAL_CART_URL, wait_until="domcontentloaded")
            wait_for_waf(page, solve_challenge)
            if not wait_until(
                lambda: "shopping cart" in page.locator("body").inner_text().lower()
                or "cart" in page.locator("body").inner_text().lower(),
            ):
                raise RuntimeError("El carrito final no terminó de cargar.")
            print(f"Carrito final abierto: {FINAL_CART_URL}")
            return
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(
                f"REINTENTO CARRITO FINAL | intento={attempt} | error={error} | "
                f"esperando={retry_delay:g}s"
            )
            time.sleep(max(0.5, retry_delay))


def add_to_cart(page: Page, card: CardRow, quantity: int, action_delay: float = 3) -> None:
    product_form = page.locator("form").filter(
        has_text=re.compile(r"Add to Cart|\d+\s+in\s+Cart", re.IGNORECASE)
    ).first
    button_scope = product_form if product_form.count() > 0 else page
    if not quantity_grid_is_visible(page):
        add_button = find_add_to_cart(button_scope)
        if add_button.count() == 0 and button_scope != page:
            add_button = find_add_to_cart(page)
        if add_button.count() == 0:
            raise RuntimeError(f"No se encontró Add to Cart para {card.name}.")

        # El primer click solo revela la cantidad. Seleccionar la opción confirma la adición.
        add_button.click()
        if not wait_for_quantity_grid(page):
            raise RuntimeError(f"No apareció la cuadrícula de cantidades para {card.name}.")
    if select_visible_quantity_menu(page, quantity):
        if not wait_for_cart_quantity(page, quantity):
            raise RuntimeError(
                f"La tienda no confirmó {quantity} unidades en el carrito para {card.name}."
            )
        return
    quantity_select = find_quantity_select(button_scope)
    if quantity_select.count() == 0 and button_scope != page:
        quantity_select = find_quantity_select(page)
    if quantity_select.count() == 0:
        raise RuntimeError(f"No apareció el selector de cantidad para {card.name}.")
    try:
        quantity_select.select_option(str(quantity))
    except Exception:
        quantity_select.select_option(label=str(quantity))
    if not wait_for_cart_quantity(page, quantity):
        raise RuntimeError(
            f"La tienda no confirmó {quantity} unidades en el carrito para {card.name}."
        )


def update_sheet(
    service: Any,
    sheet_id: str,
    title: str,
    card: CardRow,
    result: CardResult,
    update_prices: bool = True,
) -> None:
    if result.price is None and result.available is None:
        return
    quantity_range = f"'{title}'!{column_letter(ACTUAL_QUANTITY_COLUMN + 1)}{card.row_number}"
    unit_price_range = f"'{title}'!{column_letter(UNIT_PRICE_COLUMN + 1)}{card.row_number}"
    remaining_stock = (
        max(0, result.available - result.in_cart)
        if result.available is not None
        else None
    )
    actual_quantity = (
        min(card.quantity, remaining_stock)
        if remaining_stock is not None
        else None
    )
    requests = []
    if actual_quantity is not None:
        requests.append({"range": quantity_range, "values": [[actual_quantity]]})
    if update_prices and result.price is not None:
        requests.append({"range": unit_price_range, "values": [[result.price]]})
    if not requests:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": requests},
    ).execute()

    format_requests = []
    if (
        update_prices
        and result.price is not None
        and card.original_unit_price is not None
        and result.price != card.original_unit_price
    ):
        color = {"red": 0.95, "green": 0.75, "blue": 0.75}
        if result.price < card.original_unit_price:
            color = {"red": 0.75, "green": 0.95, "blue": 0.75}
        sheet_id_number = service.spreadsheets().get(
            spreadsheetId=sheet_id, fields="sheets(properties(sheetId,title))"
        ).execute()["sheets"][0]["properties"]["sheetId"]
        format_requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id_number, "startRowIndex": card.row_number - 1,
                          "endRowIndex": card.row_number, "startColumnIndex": UNIT_PRICE_COLUMN,
                          "endColumnIndex": UNIT_PRICE_COLUMN + 1},
                "cell": {"userEnteredFormat": {"backgroundColor": color}},
                "fields": "userEnteredFormat.backgroundColor",
            }})

    if remaining_stock is not None and remaining_stock < card.quantity:
        sheet_id_number = service.spreadsheets().get(
            spreadsheetId=sheet_id, fields="sheets(properties(sheetId,title))"
        ).execute()["sheets"][0]["properties"]["sheetId"]
        format_requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id_number, "startRowIndex": card.row_number - 1,
                      "endRowIndex": card.row_number, "startColumnIndex": ACTUAL_QUANTITY_COLUMN,
                      "endColumnIndex": ACTUAL_QUANTITY_COLUMN + 1},
            "cell": {"userEnteredFormat": {"backgroundColor": {
                "red": 0.95, "green": 0.75, "blue": 0.75
            }}},
            "fields": "userEnteredFormat.backgroundColor",
        }})

    if format_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": format_requests},
        ).execute()


def main() -> None:
    load_dotenv()
    arguments = parse_args()
    sheet_id = resolve_sheet_id(arguments.sheet_url, arguments.sheet_id)
    credentials = Credentials.from_service_account_file(arguments.credentials, scopes=SCOPES)
    sheets_service = build("sheets", "v4", credentials=credentials)
    title, cards = load_sheet(sheets_service, sheet_id)
    if not cards:
        raise RuntimeError("No se encontraron filas con URLs en la tabla.")
    if arguments.limit is not None:
        cards = cards[: arguments.limit]

    with sync_playwright() as playwright:
        browser_args = []
        if arguments.headed:
            monitors = get_monitors()
            monitor_index = arguments.monitor - 1
            if 0 <= monitor_index < len(monitors):
                monitor = monitors[monitor_index]
                browser_args = [
                    f"--window-position={monitor.x},{monitor.y}",
                    f"--window-size={monitor.width},{monitor.height}",
                ]
                print(
                    f"NAVEGADOR | monitor={arguments.monitor} | "
                    f"posicion=({monitor.x},{monitor.y}) | "
                    f"tamano={monitor.width}x{monitor.height}"
                )
            else:
                print(
                    f"AVISO | no existe el monitor {arguments.monitor}; "
                    "se usará la pantalla principal."
                )
        browser: Browser = playwright.chromium.launch(
            headless=not arguments.headed,
            args=browser_args,
        )
        auth_path = Path(arguments.auth_file)
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        context = browser.new_context(
            storage_state=str(auth_path) if auth_path.exists() else None
        )
        page = context.new_page()
        login_if_needed(page, arguments.solve_challenge)
        if arguments.apply:
            clear_cart(
                page,
                arguments.solve_challenge,
                arguments.retry_delay,
                arguments.cart_action_delay,
            )
        for card in cards:
            result = inspect_card(
                page,
                card,
                arguments.solve_challenge,
                arguments.probe_stock,
                arguments.retry_delay,
            )
            if result.error:
                print(f"AVISO | fila {card.row_number} | {card.name} | {result.error}")
            remaining_stock = (
                max(0, result.available - result.in_cart)
                if result.available is not None
                else None
            )
            actual_quantity = (
                min(card.quantity, remaining_stock)
                if remaining_stock is not None
                else None
            )
            price_text = f"{result.price:.2f}" if result.price is not None else "desconocido"
            stock_text = result.available if result.available is not None else "desconocido"
            actual_text = actual_quantity if actual_quantity is not None else "sin determinar"
            target_cart_quantity = (
                result.in_cart + actual_quantity
                if actual_quantity is not None
                else None
            )
            target_text = target_cart_quantity if target_cart_quantity is not None else "sin determinar"
            print(f"OK | fila {card.row_number} | {card.name} | stock={stock_text} | en_carrito={result.in_cart} | restante={remaining_stock if remaining_stock is not None else 'desconocido'} | precio={price_text} | solicitada={card.quantity} | real={actual_text} | total_carrito={target_text}")
            should_update_sheet = (
                arguments.apply or arguments.update_sheet or arguments.stock_only
            )
            if should_update_sheet and (
                result.price is not None or result.available is not None
            ):
                update_sheet(
                    sheets_service,
                    sheet_id,
                    title,
                    card,
                    result,
                    update_prices=not arguments.stock_only,
                )
                if arguments.apply and actual_quantity is not None and actual_quantity > 0:
                    try:
                        add_to_cart(page, card, target_cart_quantity, arguments.cart_action_delay)
                    except Exception as error:
                        print(
                            f"PROCESO DETENIDO | fila {card.row_number} | {card.name} | "
                            f"no se pudo confirmar la cantidad {actual_quantity} en el carrito: {error}"
                        )
                        context.storage_state(path=str(auth_path))
                        if arguments.headed and not arguments.close_browser:
                            open_final_cart(page, arguments.solve_challenge, arguments.retry_delay)
                            keep_browser_open()
                        else:
                            input("Pulsa Enter para cerrar el proceso... ")
                        return
        if arguments.apply or arguments.update_sheet or arguments.stock_only:
            context.storage_state(path=str(auth_path))
        if arguments.headed and not arguments.close_browser:
            open_final_cart(page, arguments.solve_challenge, arguments.retry_delay)
            keep_browser_open()
        else:
            browser.close()


if __name__ == "__main__":
    main()
