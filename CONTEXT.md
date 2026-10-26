# Contexto Del Proyecto

## Objetivo

Sincronizar una tabla de cartas de Google Sheets con las páginas de MTGMintCard. El proceso consulta precio y stock, actualiza la hoja y, opcionalmente, agrega al carrito las cantidades realmente disponibles.

## Configuración actual

- El enlace de la hoja se configura con `GOOGLE_SHEET_URL` en `.env`.
- También se acepta `GOOGLE_SHEET_ID` o `--sheet-url`/`--sheet-id`.
- La URL tiene prioridad sobre el ID.
- La cuenta de servicio se configura con `GOOGLE_SERVICE_ACCOUNT_FILE`.
- La cuenta de servicio necesita permiso Editor sobre la hoja.
- Las credenciales y sesiones están excluidas por `.gitignore`.

## Estructura de la hoja

La fila 1 contiene encabezados. Las cartas empiezan en la fila 2.

- `A`: cantidad real que se puede pedir.
- `B`: cantidad solicitada por el usuario.
- `C`: nombre de la carta.
- `D`: precio unitario, único precio que se modifica.
- `E`: columna existente que el script no debe modificar.
- `F`: URL de la carta.

## Reglas de negocio

- La cantidad real es `min(B, stock_web)`.
- Si la página indica `N in Cart`, la cantidad disponible para la fila es `max(0, stock_web - N)`.
- El selector de la tienda representa el total final del carrito: se selecciona `N + cantidad_real`.
- Si la web informa `Out of Stock`, el stock es `0`.
- Si la cantidad real es menor que la solicitada, `A` se marca en rojo.
- El precio válido es el de la página web y se escribe en `D`.
- Si el precio web es menor que el precio original, `D` se marca en verde.
- Si el precio web es mayor que el precio original, `D` se marca en rojo.
- `E` no se toca.
- El carrito solo se modifica con `--apply`.
- Al vaciar el carrito se acepta la confirmación de `Remove All` y se espera la confirmación de carrito vacío.
- Después de seleccionar `Select Qty` se espera a que el botón refleje `N in Cart` para confirmar la adición.

## Flujo de stock en la tienda

1. Abrir la URL de la carta.
2. Detectar `Out of Stock`.
3. Si hay `Add to Cart`, pulsarlo para mostrar `Select Qty`.
4. Leer el máximo de la secuencia de cantidades visible.
5. En modo `--apply`, seleccionar la cantidad real; esa selección confirma la adición al carrito.

El primer click en `Add to Cart` solo revela el selector según el comportamiento observado. La selección de `Select Qty` es la acción que agrega al carrito.

## Modos importantes

- Sin opción de actualización: consulta y muestra datos.
- `--update-sheet`: actualiza `A`, `D` y colores, sin tocar el carrito.
- `--stock-only`: actualiza solo `A` y el color de stock.
- `--apply`: actualiza la hoja y manipula el carrito.
- `--probe-stock`: pulsa `Add to Cart` para revelar el stock.
- `--limit 1`: procesa solo la primera carta.
- `--headed --solve-challenge`: abre navegador visible y permite resolver AWS WAF.

## Reintentos

La lectura de cada carta se reintenta indefinidamente cuando la página está incompleta, no aparece el botón de compra, falla la carga o no se obtienen datos. Las esperas de página usan estados y timeouts; el intervalo entre reintentos es de 5 segundos y se cambia con `--retry-delay` o `STORE_RETRY_DELAY`. `Ctrl+C` detiene el proceso.

## Navegador

Se usa Playwright Chromium. Con `--headed`, `screeninfo` detecta monitores y el navegador se abre por defecto en el monitor 2. Se puede cambiar con `--monitor` o `STORE_BROWSER_MONITOR`.

En modo visible, el navegador navega al carrito final y permanece abierto al terminar. `--close-browser` habilita el cierre automático.

## Test De Google Sheets

`test_google_sheet_write.py` valida credenciales, acceso, pestaña, encabezados, URLs, lectura, escritura temporal y formato temporal. Usa `Z1`, restaura su valor y restaura su formato al terminar.

## Test De Tienda

`test_store_cart.py` valida el inicio de sesión y el vaciado del carrito sin procesar cartas ni modificar Google Sheets. Si no se configura `STORE_EMPTY_CART_SELECTOR`, busca controles visibles `Empty Cart`, `Clear Cart` o `Remove All`.

## Problemas conocidos

- MTGMintCard puede mostrar un desafío AWS WAF.
- La página puede cargar de forma incompleta o cambiar su HTML.
- El modo `--apply` requiere configurar correctamente el selector para vaciar el carrito.
- Las credenciales de la tienda pueden requerir una sesión manual si hay CAPTCHA o 2FA.
- Si el formulario de login cambia, se pueden configurar selectores CSS mediante `STORE_USERNAME_SELECTOR`, `STORE_PASSWORD_SELECTOR` y `STORE_LOGIN_SUBMIT_SELECTOR`.

## Próximos pasos posibles

- Validar los selectores de vaciado del carrito antes de usar `--apply`.
- Añadir un reporte persistente por ejecución.
- Limpiar colores antiguos cuando una carta vuelva a estar disponible o su precio vuelva a coincidir.
- Añadir pruebas automatizadas con HTML simulado de la tienda.
