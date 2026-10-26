"""Resuelve el identificador de una hoja de Google desde URL o ID."""

from __future__ import annotations

import re


SHEET_ID_PATTERN = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


def resolve_sheet_id(sheet_url: str | None = None, sheet_id: str | None = None) -> str:
    """Devuelve el ID priorizando la URL sobre el ID directo."""
    if sheet_url:
        match = SHEET_ID_PATTERN.search(sheet_url.strip())
        if not match:
            raise ValueError("La URL de Google Sheets no contiene un ID válido.")
        return match.group(1)
    if sheet_id and sheet_id.strip():
        return sheet_id.strip()
    raise ValueError(
        "Configura GOOGLE_SHEET_URL o GOOGLE_SHEET_ID, o usa --sheet-url/--sheet-id."
    )
