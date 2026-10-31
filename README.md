# Sincronización de stock y precios

El proyecto consulta exclusivamente las cartas de MTGMintCard y actualiza automáticamente sus existencias y precios en Google Sheets. Las URLs de las cartas deben apuntar a `mtgmintcard.com`.

## Estructura de la hoja

La primera fila contiene encabezados. Las cartas comienzan en la fila 2:

| Columna | Uso |
|---|---|
| A | Cantidad solicitada |
| B | Cantidad disponible para pedir, actualizada por el script |
| C | Nombre de la carta |
| D | Precio unitario actualizado por el script |
| E | Columna existente, no modificada |
| F | URL de la carta |

Para cada carta, el script calcula `min(A, stock_web)` y escribe el resultado en `B`. Si la web informa `Out of Stock`, el resultado es `0`. Cuando el resultado es menor que `A`, la celda `B` se marca en rojo.

El precio web se escribe en `D`:

- Verde: el precio web es menor que el precio anterior.
- Rojo: el precio web es mayor que el precio anterior.

## Configuración

Configura `.env` a partir de `.env.example`:

```env
GOOGLE_SHEET_URL=https://docs.google.com/spreadsheets/d/ID_DE_LA_HOJA/edit
GOOGLE_SERVICE_ACCOUNT_FILE=./utopian-calling-274800-552c65952619.json
STORE_USERNAME=
STORE_PASSWORD=
STORE_LOGIN_URL=https://www.mtgmintcard.com/login
STORE_HEADED=false
STORE_SOLVE_CHALLENGE=false
```

La cuenta de servicio debe tener permiso **Editor** sobre la hoja. También se puede usar `GOOGLE_SHEET_ID` en lugar de `GOOGLE_SHEET_URL`.

## Instalación

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

## Ejecución

Un único comando consulta todas las cartas y actualiza Google Sheets:

```powershell
.venv\Scripts\python.exe sync_stock.py
```

El proceso reintenta una carta si la página de MTGMintCard no carga correctamente. Si MTGMintCard muestra un desafío AWS WAF, configura `STORE_SOLVE_CHALLENGE=true`; el script mostrará automáticamente el navegador para que puedas resolverlo. Después de resolverlo, pulsa Enter en la consola.

## Prueba de Google Sheets

El siguiente comando valida las credenciales, el acceso, los encabezados, las URLs y la escritura temporal en la hoja:

```powershell
.venv\Scripts\python.exe test_google_sheet_write.py
```

No se modifica ninguna columna distinta de `B` y `D` durante la sincronización.
