# Sincronización de stock y precios

Este proyecto lee cartas desde Google Sheets, consulta cada URL en MTGMintCard y puede actualizar el stock real, el precio unitario y los colores de la hoja.

## Configuración de la hoja

La hoja se configura en `.env` mediante su URL:

```env
GOOGLE_SHEET_URL=https://docs.google.com/spreadsheets/d/ID_DE_LA_HOJA/edit
```

También se puede usar el ID directamente:

```env
GOOGLE_SHEET_ID=ID_DE_LA_HOJA
```

La URL tiene prioridad sobre el ID. Desde la línea de comandos también se puede indicar otra hoja:

```powershell
.venv\Scripts\python.exe sync_stock.py --sheet-url "https://docs.google.com/spreadsheets/d/OTRO_ID/edit"
```

La cuenta de servicio indicada en `GOOGLE_SERVICE_ACCOUNT_FILE` debe tener permiso **Editor** sobre la hoja seleccionada.

## Estructura de la tabla

La primera fila debe contener encabezados. Las cartas comienzan en la fila 2:

| Columna | Uso |
|---|---|
| A | Cantidad real que se puede pedir |
| B | Cantidad solicitada |
| C | Nombre de la carta |
| D | Precio unitario |
| E | Columna existente, no modificada por el script |
| F | URL de la carta |

La cantidad de `A` se calcula como el mínimo entre la cantidad solicitada de `B` y el stock real de la web. Si la web tiene menos unidades, `A` se marca en rojo. `Out of Stock` se interpreta como `0`.

Si la página indica `N in Cart`, ese valor se resta del stock disponible. El selector de la tienda se elige con la cantidad total final: si ya hay `2 in Cart` y la fila necesita `2` adicionales, se selecciona `4`. `A` conserva únicamente las unidades nuevas de la fila.

El precio de la web se escribe únicamente en `D`:

- Verde: el precio web es menor que el precio original.
- Rojo: el precio web es mayor que el precio original.

## Instalación

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m playwright install chromium
```

Copia `.env.example` como `.env` y completa la configuración local. No compartas `.env`, el JSON de Google ni `playwright/.auth/`.

## Prueba de Google Sheets

El test valida:

- Credenciales y acceso a la hoja.
- Primera pestaña disponible.
- Encabezados en `A:F`.
- Filas con URLs en `F`.
- Permiso de lectura y escritura.
- Modificación temporal de `Z1` y restauración.
- Aplicación temporal de formato y restauración.

Ejecutar:

```powershell
.venv\Scripts\python.exe test_google_sheet_write.py
```

## Modos de sincronización

### Simulación

Consulta la tienda y muestra los valores sin modificar la hoja:

```powershell
.venv\Scripts\python.exe sync_stock.py --headed --solve-challenge --probe-stock
```

### Actualizar stock y precio

```powershell
.venv\Scripts\python.exe sync_stock.py --headed --solve-challenge --probe-stock --update-sheet
```

### Actualizar únicamente stock

```powershell
.venv\Scripts\python.exe sync_stock.py --headed --solve-challenge --probe-stock --stock-only
```

### Probar un número limitado de cartas

```powershell
.venv\Scripts\python.exe sync_stock.py --headed --solve-challenge --probe-stock --limit 5
```

`--limit` acepta cualquier entero positivo y limita el procesamiento a las primeras filas seleccionadas.

### Agregar cantidades reales al carrito

```powershell
.venv\Scripts\python.exe sync_stock.py --headed --solve-challenge --probe-stock --apply
```

`--apply` es destructivo: vacía el carrito usando `STORE_EMPTY_CART_SELECTOR`, consulta cada carta y selecciona la cantidad real en `Select Qty`. No usar hasta validar los selectores y la simulación.

Para probar el carrito sin modificar Google Sheets, usa `--cart-only`:

```powershell
.venv\Scripts\python.exe sync_stock.py --headed --solve-challenge --probe-stock --cart-only --limit 5
```

Este modo vacía el carrito, agrega únicamente las cantidades de `A` de las cinco filas procesadas y valida el carrito final.

La validación final comprueba que las cartas de `A` aparezcan en orden alfabético por nombre y con la cantidad exacta. Las cartas con el mismo nombre se diferencian mediante su URL y pueden aparecer en cualquier orden entre sí.

Después de pulsar `Remove All` se acepta la confirmación y se espera el estado de carrito vacío. Después de seleccionar `Select Qty` se espera a que el botón refleje `N in Cart`; no se usa una pausa fija para avanzar.

## Navegador y AWS WAF

Con `--headed`, Playwright abre un navegador visible. Por defecto se usa el monitor 2:

```powershell
.venv\Scripts\python.exe sync_stock.py --headed --monitor 2 --solve-challenge --probe-stock --update-sheet
```

Si aparece AWS WAF, resuelve el desafío en el navegador y pulsa Enter en la terminal. El navegador se abre en una ventana propia; no es necesario mantener otra ventana de la tienda abierta.

Si el formulario de login de la tienda usa controles no estándar, configura `STORE_USERNAME_SELECTOR`, `STORE_PASSWORD_SELECTOR` y `STORE_LOGIN_SUBMIT_SELECTOR` con selectores CSS.

Si una página carga incompleta, el script reintenta indefinidamente. Las esperas principales se basan en estados de la página y tienen un timeout máximo. `Ctrl+C` detiene la ejecución.

En modo visible, el navegador navega al carrito final `https://www.mtgmintcard.com/shopping-cart` y permanece abierto al terminar para permitir trabajo manual. Usa `--close-browser` si quieres cerrarlo automáticamente.

Si la validación del carrito falla, se muestran las cartas faltantes, sobrantes o con cantidades diferentes y el proceso termina con error.

## Verificar login y carrito

`test_store_cart.py` abre el navegador, inicia sesión, entra al carrito, lo vacía y confirma que aparece el estado de carrito vacío. No procesa cartas ni modifica Google Sheets.

```powershell
.venv\Scripts\python.exe test_store_cart.py --solve-challenge
```

Si `STORE_EMPTY_CART_SELECTOR` está vacío, se buscan únicamente controles visibles llamados `Empty Cart`, `Clear Cart` o `Remove All`. Para una instalación con nombres diferentes, configura el selector explícitamente en `.env`.

## Archivos sensibles

`.gitignore` excluye `.env`, archivos JSON, sesiones de Playwright y el entorno virtual. Nunca subas credenciales ni cookies de sesión.
