"""Actualiza existencias y precios de cartas en Google Sheets."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import Browser, Page, sync_playwright
from sheet_config import resolve_sheet_id


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CREDENTIALS_FILE = "utopian-calling-274800-552c65952619.json"
SHEET_RANGE = "A:Z"
REQUESTED_QUANTITY_COLUMN = 0
AVAILABLE_QUANTITY_COLUMN = 1
NAME_COLUMN = 2
UNIT_PRICE_COLUMN = 3
CARD_URL_COLUMN = 5


@dataclass
class CardRow:
    row_number: int
    requested_quantity: int
    name: str
    original_unit_price: float | None
    url: str


@dataclass
class CardResult:
    available: int
    price: float | None


def parse_number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    match = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(value).replace(",", "."))
    return float(match.group(0)) if match else None


def parse_quantity(value: Any) -> int:
    return max(0, int(parse_number(value) or 0))


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

    cards: list[CardRow] = []
    for row_number, values in enumerate(response.get("values", []), start=1):
        if row_number == 1 or len(values) <= CARD_URL_COLUMN:
            continue
        url = str(values[CARD_URL_COLUMN]).strip()
        if not url.startswith(("http://", "https://")):
            continue
        cards.append(
            CardRow(
                row_number=row_number,
                requested_quantity=parse_quantity(
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
    return title, cards


def first_visible(locator: Any) -> Any:
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    return locator.first


def wait_until(predicate: Any, timeout: float = 15) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return bool(predicate())


def is_waf_challenge(page: Page) -> bool:
    try:
        content = page.content()
        body = page.locator("body").inner_text().strip()
    except Exception:
        return False
    return "awsWafCookieDomainList" in content or (len(content) < 3000 and not body)


def wait_for_waf(page: Page) -> None:
    if not is_waf_challenge(page):
        return
    if os.getenv("STORE_SOLVE_CHALLENGE", "false").lower() not in {"1", "true", "yes"}:
        raise RuntimeError(
            "La tienda devolvió un desafío AWS WAF. Configura STORE_SOLVE_CHALLENGE=true "
            "y STORE_HEADED=true para resolverlo manualmente."
        )
    print("Resuelve el desafío de seguridad en el navegador y pulsa Enter aquí.")
    input()
    if is_waf_challenge(page):
        raise RuntimeError("El desafío AWS WAF sigue activo.")


def login_if_needed(page: Page) -> None:
    username = os.getenv("STORE_USERNAME")
    password = os.getenv("STORE_PASSWORD")
    if not username or not password:
        return
    page.goto(os.getenv("STORE_LOGIN_URL", "https://www.mtgmintcard.com"), wait_until="domcontentloaded")
    wait_for_waf(page)
    user = first_visible(page.locator(os.getenv("STORE_USERNAME_SELECTOR", 'input[type="email"], input[name*="user" i], input[name*="email" i]')))
    secret = first_visible(page.locator(os.getenv("STORE_PASSWORD_SELECTOR", 'input[type="password"]')))
    if user.count() == 0 or secret.count() == 0:
        return
    user.fill(username)
    secret.fill(password)
    submit = first_visible(page.locator(os.getenv("STORE_LOGIN_SUBMIT_SELECTOR", 'button[type="submit"], input[type="submit"]')))
    if submit.count() == 0:
        raise RuntimeError("No se encontró el botón de inicio de sesión.")
    submit.click()
    page.wait_for_load_state("domcontentloaded")


def find_stock_control(page: Page) -> Any:
    candidates = page.locator('button:has-text("Add to Cart"), a:has-text("Add to Cart"), input[type="submit"][value*="Add" i]')
    return first_visible(candidates)


def extract_price(text: str) -> float | None:
    match = re.search(r"(?:\$|USD\s*)\s*(\d+(?:[.,]\d{1,2})?)", text, re.IGNORECASE)
    return parse_number(match.group(1)) if match else None


def read_price(page: Page) -> float | None:
    button = find_stock_control(page)
    if button.count() > 0:
        text = button.evaluate(
            """(element) => {
                let node = element;
                for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                    const value = (node.innerText || '').trim();
                    if (/add\\s+to\\s+cart/i.test(value) && /\\$\\s*\\d/.test(value) && value.length < 700) return value;
                }
                return '';
            }"""
        )
        price = extract_price(text)
        if price is not None:
            return price
    for selector in ['meta[property="product:price:amount"]', 'meta[itemprop="price"]', '[itemprop="price"]']:
        element = page.locator(selector).first
        if element.count() > 0:
            price = parse_number(element.get_attribute("content") or element.inner_text())
            if price is not None:
                return price
    for script in page.locator('script[type="application/ld+json"]').all():
        try:
            data = json.loads(script.text_content() or "")
        except json.JSONDecodeError:
            continue
        price = find_structured_price(data)
        if price is not None:
            return price
    return extract_price(page.locator("body").inner_text())


def find_structured_price(value: Any) -> float | None:
    if isinstance(value, dict):
        if "price" in value:
            price = parse_number(value["price"])
            if price is not None:
                return price
        for child in value.values():
            price = find_structured_price(child)
            if price is not None:
                return price
    elif isinstance(value, list):
        for child in value:
            price = find_structured_price(child)
            if price is not None:
                return price
    return None


def read_stock(page: Page) -> int | None:
    options = page.locator("#selectable li.ui-state-default")
    values = [
        parse_quantity(options.nth(index).inner_text())
        for index in range(options.count())
        if options.nth(index).is_visible() and options.nth(index).inner_text().strip().isdigit()
    ]
    if values:
        return max(values)

    selects = page.locator("select")
    for index in range(selects.count()):
        select = selects.nth(index)
        option_values = [parse_quantity(option) for option in select.locator("option").all_text_contents()]
        positive = [value for value in option_values if value > 0]
        if positive:
            return max(positive)

    for selector in ['input[name*="quantity" i]', 'input[id*="quantity" i]', 'input[name*="qty" i]', 'input[id*="qty" i]']:
        field = page.locator(selector).first
        if field.count() > 0 and parse_quantity(field.get_attribute("max")) > 0:
            return parse_quantity(field.get_attribute("max"))
    body = page.locator("body").inner_text()
    if re.search(r"out\s+of\s+stock", body, re.IGNORECASE):
        return 0
    match = re.search(r"(?:in stock|available|stock)\D{0,20}(\d+)", body, re.IGNORECASE)
    return int(match.group(1)) if match else None


def reveal_stock(page: Page) -> None:
    button = find_stock_control(page)
    if button.count() == 0:
        return
    button.click()
    wait_until(lambda: read_stock(page) is not None, timeout=10)


def inspect_card(page: Page, card: CardRow) -> CardResult:
    retry_delay = max(0.5, float(os.getenv("STORE_RETRY_DELAY", "5")))
    attempt = 0
    while True:
        attempt += 1
        try:
            page.goto(card.url, wait_until="domcontentloaded")
            wait_for_waf(page)
            price = read_price(page)
            stock = read_stock(page)
            if stock is None:
                reveal_stock(page)
                stock = read_stock(page)
            if stock is None:
                raise RuntimeError("No se encontró el stock en la página.")
            return CardResult(stock, price)
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(f"REINTENTO | fila {card.row_number} | {card.name} | intento={attempt} | error={error}")
            time.sleep(retry_delay)


def update_sheet(service: Any, sheet_id: str, title: str, card: CardRow, result: CardResult) -> None:
    available_quantity = min(card.requested_quantity, result.available)
    requests = [{
        "range": f"'{title}'!{column_letter(AVAILABLE_QUANTITY_COLUMN + 1)}{card.row_number}",
        "values": [[available_quantity]],
    }]
    if result.price is not None:
        requests.append({
            "range": f"'{title}'!{column_letter(UNIT_PRICE_COLUMN + 1)}{card.row_number}",
            "values": [[result.price]],
        })
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": requests},
    ).execute()

    sheet_id_number = service.spreadsheets().get(
        spreadsheetId=sheet_id, fields="sheets(properties(sheetId,title))"
    ).execute()["sheets"][0]["properties"]["sheetId"]
    format_requests = []
    if available_quantity < card.requested_quantity:
        format_requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id_number, "startRowIndex": card.row_number - 1,
                      "endRowIndex": card.row_number, "startColumnIndex": AVAILABLE_QUANTITY_COLUMN,
                      "endColumnIndex": AVAILABLE_QUANTITY_COLUMN + 1},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.95, "green": 0.75, "blue": 0.75}}},
            "fields": "userEnteredFormat.backgroundColor",
        }})
    if result.price is not None and card.original_unit_price is not None and result.price != card.original_unit_price:
        color = {"red": 0.95, "green": 0.75, "blue": 0.75}
        if result.price < card.original_unit_price:
            color = {"red": 0.75, "green": 0.95, "blue": 0.75}
        format_requests.append({"repeatCell": {
            "range": {"sheetId": sheet_id_number, "startRowIndex": card.row_number - 1,
                      "endRowIndex": card.row_number, "startColumnIndex": UNIT_PRICE_COLUMN,
                      "endColumnIndex": UNIT_PRICE_COLUMN + 1},
            "cell": {"userEnteredFormat": {"backgroundColor": color}},
            "fields": "userEnteredFormat.backgroundColor",
        }})
    if format_requests:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": format_requests}
        ).execute()


def main() -> None:
    load_dotenv()
    sheet_id = resolve_sheet_id(os.getenv("GOOGLE_SHEET_URL"), os.getenv("GOOGLE_SHEET_ID"))
    credentials_file = Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", CREDENTIALS_FILE)).expanduser()
    credentials = Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    service = build("sheets", "v4", credentials=credentials)
    title, cards = load_sheet(service, sheet_id)
    if not cards:
        raise RuntimeError("No se encontraron filas con URLs en la tabla.")

    headed = os.getenv("STORE_HEADED", "false").lower() in {"1", "true", "yes"}
    solve_challenge = os.getenv("STORE_SOLVE_CHALLENGE", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    headed = headed or solve_challenge
    auth_path = Path(os.getenv("STORE_AUTH_FILE", "playwright/.auth/store.json"))
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(headless=not headed)
        context = browser.new_context(storage_state=str(auth_path) if auth_path.exists() else None)
        page = context.new_page()
        login_if_needed(page)
        for card in cards:
            result = inspect_card(page, card)
            update_sheet(service, sheet_id, title, card, result)
            print(
                f"OK | fila {card.row_number} | {card.name} | "
                f"solicitada={card.requested_quantity} | disponible={result.available} | "
                f"actualizada={min(card.requested_quantity, result.available)} | "
                f"precio={result.price if result.price is not None else 'desconocido'}"
            )
        context.storage_state(path=str(auth_path))
        browser.close()


if __name__ == "__main__":
    main()
