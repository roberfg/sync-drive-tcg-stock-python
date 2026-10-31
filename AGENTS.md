# Contexto Del Proyecto

## Objetivo

Consultar las páginas de MTGMintCard y sincronizar existencias y precios con una tabla de Google Sheets.

## Configuración

- Configurar `GOOGLE_SHEET_URL` o `GOOGLE_SHEET_ID` en `.env`.
- `GOOGLE_SHEET_URL` tiene prioridad sobre `GOOGLE_SHEET_ID`.
- Configurar la cuenta de servicio con `GOOGLE_SERVICE_ACCOUNT_FILE`.
- La cuenta de servicio necesita permiso Editor sobre la hoja.
- Las credenciales y sesiones están excluidas por `.gitignore`.

## Estructura De La Hoja

La fila 1 contiene encabezados y las cartas empiezan en la fila 2.

- `A`: cantidad solicitada por el usuario; el script solo la lee.
- `B`: cantidad disponible para pedir; el script escribe `min(A, stock_web)`.
- `C`: nombre de la carta.
- `D`: precio unitario; el script escribe el precio web.
- `E`: columna existente que el script no modifica.
- `F`: URL de la carta.

Las URLs de la columna `F` deben pertenecer a MTGMintCard (`mtgmintcard.com`). No se consulta ninguna otra tienda.

## Reglas De Negocio

- Si la web informa `Out of Stock`, el stock es `0`.
- Si el stock web es suficiente, `B` recibe la cantidad solicitada de `A`.
- Si el stock web es menor, `B` recibe el stock disponible.
- Si `B` es menor que `A`, `B` se marca en rojo.
- Si el precio web es menor que el precio anterior, `D` se marca en verde.
- Si el precio web es mayor que el precio anterior, `D` se marca en rojo.
- Solo se modifican las columnas `B`, `D` y los colores asociados.
- No se gestiona ningún carrito.

## Ejecución

El proceso completo se ejecuta sin flags:

```powershell
.venv\Scripts\python.exe sync_stock.py
```

La lectura de cada carta se reintenta indefinidamente cuando la página está incompleta o no se obtiene stock. `STORE_RETRY_DELAY` controla el intervalo entre reintentos.

`STORE_HEADED=true` muestra el navegador. `STORE_SOLVE_CHALLENGE=true` permite resolver manualmente un desafío AWS WAF.

La URL de inicio de sesión se configura con `STORE_LOGIN_URL` y debe apuntar a MTGMintCard, normalmente `https://www.mtgmintcard.com/login`. Cuando `STORE_SOLVE_CHALLENGE=true`, el script muestra automáticamente el navegador aunque `STORE_HEADED` esté desactivado.

## Pruebas

`test_google_sheet_write.py` valida las credenciales, el acceso, la estructura de la hoja y una escritura temporal reversible.
