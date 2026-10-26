"""Prueba reversible de lectura y escritura en Google Sheets."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from sheet_config import resolve_sheet_id


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_CREDENTIALS_FILE = "utopian-calling-274800-552c65952619.json"
DEFAULT_CELL = "Z1"
TEST_VALUE = "PRUEBA_AUTOMATIZACION"


def get_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verifica permisos de lectura y escritura en Google Sheets."
    )
    parser.add_argument("--sheet-url", default=os.getenv("GOOGLE_SHEET_URL"))
    parser.add_argument("--sheet-id", default=os.getenv("GOOGLE_SHEET_ID"))
    parser.add_argument(
        "--credentials",
        default=os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", DEFAULT_CREDENTIALS_FILE),
        help="Ruta al JSON de la cuenta de servicio.",
    )
    parser.add_argument(
        "--cell",
        default=DEFAULT_CELL,
        help="Celda temporal que se modificará y restaurará.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    arguments = get_arguments()
    sheet_id = resolve_sheet_id(arguments.sheet_url, arguments.sheet_id)
    credentials_path = Path(arguments.credentials).expanduser().resolve()

    if not credentials_path.is_file():
        raise FileNotFoundError(
            f"No se encontró el archivo de credenciales: {credentials_path}"
        )

    credentials = Credentials.from_service_account_file(
        credentials_path,
        scopes=SCOPES,
    )
    sheets_service = build("sheets", "v4", credentials=credentials)

    spreadsheet = (
        sheets_service.spreadsheets()
        .get(spreadsheetId=sheet_id)
        .execute()
    )
    first_sheet = spreadsheet["sheets"][0]
    sheet_title = first_sheet["properties"]["title"]
    sheet_numeric_id = first_sheet["properties"]["sheetId"]

    table_response = (
        sheets_service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=f"'{sheet_title}'!A1:F")
        .execute()
    )
    table_rows = table_response.get("values", [])
    if not table_rows:
        raise RuntimeError("La primera pestaña está vacía.")
    headers = table_rows[0]
    if len(headers) < 6 or not all(str(header).strip() for header in headers[:6]):
        raise RuntimeError(
            "La fila 1 debe contener encabezados en las columnas A:F."
        )
    card_rows = [row for row in table_rows[1:] if len(row) > 5 and str(row[5]).startswith(("http://", "https://"))]
    if not card_rows:
        raise RuntimeError("No se encontraron filas de cartas con URLs en la columna F.")
    print(f"Hoja verificada: {sheet_title}")
    print(f"Encabezados A:F: {headers[:6]}")
    print(f"Filas con cartas: {len(card_rows)}")

    test_range = f"'{sheet_title}'!{arguments.cell}"

    current_response = (
        sheets_service.spreadsheets()
        .values()
        .get(
            spreadsheetId=sheet_id,
            range=test_range,
            valueRenderOption="FORMULA",
        )
        .execute()
    )
    current_values = current_response.get("values", [])
    original_value = current_values[0][0] if current_values else None

    try:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=test_range,
            valueInputOption="RAW",
            body={"values": [[TEST_VALUE]]},
        ).execute()

        verification = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=sheet_id, range=test_range)
            .execute()
        )
        written_value = verification.get("values", [[None]])[0][0]

        if written_value != TEST_VALUE:
            raise RuntimeError(
                f"La escritura no pudo verificarse. Valor recibido: {written_value!r}"
            )
    finally:
        if original_value is None:
            sheets_service.spreadsheets().values().clear(
                spreadsheetId=sheet_id,
                range=test_range,
                body={},
            ).execute()
        else:
            sheets_service.spreadsheets().values().update(
                spreadsheetId=sheet_id,
                range=test_range,
                valueInputOption="RAW",
                body={"values": [[original_value]]},
            ).execute()

    format_test_range = {
        "sheetId": sheet_numeric_id,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 25,
        "endColumnIndex": 26,
    }
    format_response = (
        sheets_service.spreadsheets()
        .get(
            spreadsheetId=sheet_id,
            ranges=[f"'{sheet_title}'!{arguments.cell}"],
            includeGridData=True,
            fields="sheets(data(rowData(values(userEnteredFormat))))",
        )
        .execute()
    )
    original_format = (
        format_response.get("sheets", [{}])[0]
        .get("data", [{}])[0]
        .get("rowData", [{}])[0]
        .get("values", [{}])[0]
        .get("userEnteredFormat", {})
    )
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"repeatCell": {
            "range": format_test_range,
            "cell": {"userEnteredFormat": {"backgroundColor": {
                "red": 0.8, "green": 0.8, "blue": 0.8
            }}},
            "fields": "userEnteredFormat.backgroundColor",
        }}]},
    ).execute()
    sheets_service.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"updateCells": {
            "start": {"sheetId": sheet_numeric_id, "rowIndex": 0, "columnIndex": 25},
            "rows": [{"values": [{"userEnteredFormat": original_format}]}],
            "fields": "userEnteredFormat",
        }}]},
    ).execute()

    print(f"Prueba correcta: acceso, estructura, escritura y formato verificados en {test_range}.")
    print("El valor y el formato temporales fueron restaurados.")


if __name__ == "__main__":
    main()
