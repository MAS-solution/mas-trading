# MAS Trading

Scanner de forex que aplica la teoría SabudFX (estructura de mercado, order blocks, liquidez y entradas tipo 1/2/3) y muestra en un tablero dónde se cumplen las condiciones y cómo vienen funcionando las señales.

No es una recomendación de inversión: muestra dónde se dan las condiciones del método y su resultado histórico.

## Cómo funciona
- `scanner/estructura.py`: motor de estructura (BOS con cuerpo de vela, transferencia de mechas, 2 rompimientos = cambio de tendencia, order blocks).
- `scanner/scan.py`: baja velas de Yahoo Finance (1h de 2 años, 4h armadas desde 1h, 5m de 60 días), calcula sesgo 4h/1h, estado de cada par y simula las entradas en 5m. Objetivo fijo 2R (el curso no define salida), stop en el extremo del retroceso.
- `.github/workflows/scan.yml`: corre cada hora y sube los resultados a Supabase (tablas `snapshot` y `senales`).
- `index.html`: tablero en GitHub Pages. Los datos solo se ven con login (RLS de Supabase); el primer ingreso obliga a cambiar la contraseña.

Pares: EURUSD, GBPUSD, USDJPY, USDCAD, AUDUSD, GBPJPY, XAUUSD (oro vía futuro GC=F).

## Puesta en marcha
1. Proyecto Supabase → correr `supabase/schema.sql`.
2. `config.js`: URL y anon key del proyecto.
3. Secrets del repo: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
4. `python scripts/crear_usuario.py <usuario> <clave_inicial>` con las mismas variables de entorno.

Prueba local sin Supabase: `pip install -r requirements.txt && python scanner/scan.py --local` → `salida/snapshot.json`.
