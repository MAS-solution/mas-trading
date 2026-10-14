"""Motor de estructura de mercado segun la teoria SabudFX.

Reglas que implementa:
- Un rompimiento (BOS) solo vale con cierre de cuerpo de vela.
- Si el precio pasa el maximo solo con mecha, se transfiere el swing de ese lado
  y el swing protegido (HL en alcista / LH en bajista) queda igual.
- Un rompimiento en contra = liquidez / retroceso profundo. Dos seguidos = cambio de tendencia.
- Order Block = ultima vela contraria antes del rompimiento con cuerpo.

Para no duplicar logica, todo se calcula en un "espacio espejado": en bajista se
niegan los precios, asi un LL se trata como un HH. `d` es 1 (alcista) o -1 (bajista).
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class OrderBlock:
    d: int                 # direccion del OB: 1 = zona de compra, -1 = zona de venta
    bajo: float
    alto: float
    vela: pd.Timestamp     # vela que forma el OB
    creado: pd.Timestamp   # vela que hizo el rompimiento con cuerpo
    invalidado: pd.Timestamp | None = None  # primera vela que cierra del otro lado de la zona


@dataclass
class Resultado:
    tendencia: np.ndarray            # d vigente al cierre de cada vela (0 = sin definir)
    swing_alto: float
    swing_bajo: float
    rompimientos_en_contra: int
    obs: list = field(default_factory=list)
    eventos: list = field(default_factory=list)  # (fecha, tipo, d)


def analizar(df: pd.DataFrame, piv: int = 2, arranque: int = 20) -> Resultado:
    """df con columnas Open/High/Low/Close e indice de fechas, ordenado."""
    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    t = df.index
    n = len(df)
    tendencia = np.zeros(n, dtype=int)
    if n <= arranque:
        return Resultado(tendencia, float("nan"), float("nan"), 0)

    # funciones en espacio espejado
    def hi(i, d): return h[i] if d == 1 else -l[i]
    def lo(i, d): return l[i] if d == 1 else -h[i]
    def cl(i, d): return d * c[i]
    def op(i, d): return d * o[i]

    d = 1
    definida = False
    top_i = int(np.argmax(h[:arranque])); top = h[top_i]
    bot_i = int(np.argmin(l[:arranque])); bot = l[bot_i]
    rotos = 0          # rompimientos en contra seguidos
    b1_i = lo_i = 0    # vela del primer rompimiento en contra / extremo posterior
    lo_v = 0.0
    obs: list[OrderBlock] = []
    eventos = []

    def crear_ob(desde, hasta, d):
        """Ultima vela contraria entre desde..hasta-1; si no hay, la vela del swing."""
        j = next((k for k in range(hasta - 1, desde - 1, -1) if cl(k, d) < op(k, d)), desde)
        obs.append(OrderBlock(d, float(l[j]), float(h[j]), t[j], t[hasta]))

    for i in range(arranque, n):
        H, L, C = hi(i, d), lo(i, d), cl(i, d)

        # invalidacion de OBs: cierre del otro lado de la zona
        for ob in obs:
            if ob.invalidado is None and ob.creado < t[i]:
                if (ob.d == 1 and c[i] < ob.bajo) or (ob.d == -1 and c[i] > ob.alto):
                    ob.invalidado = t[i]

        if not definida:
            if c[i] > top or c[i] < bot:
                d = 1 if c[i] > top else -1
                definida = True
                top_i = i
                top = hi(i, d)
                rng = range(max(0, i - arranque), i + 1)
                bot_i = min(rng, key=lambda k: lo(k, d)); bot = lo(bot_i, d)
                crear_ob(bot_i, i, d)
                eventos.append((t[i], "BOS", d))
            tendencia[i] = 0
            continue

        bos = C > top and i - top_i > piv
        if bos:
            bot_i = min(range(top_i, i + 1), key=lambda k: lo(k, d)); bot = lo(bot_i, d)
            crear_ob(bot_i, i, d)
            top, top_i, rotos = H, i, 0
            eventos.append((t[i], "BOS", d))
        elif H > top:
            # extension del impulso o transferencia de mecha: se mueve solo el swing de arriba
            top, top_i = H, i

        if not bos and rotos == 0 and C < bot:
            rotos, b1_i, lo_v, lo_i = 1, i, L, i
            eventos.append((t[i], "LIQUIDEZ", d))
        elif not bos and rotos == 1:
            if C < lo_v and i - lo_i > piv:
                # segundo rompimiento: cambio de estructura
                d = -d
                rng = range(b1_i, i + 1)
                top_i = max(rng, key=lambda k: hi(k, d)); top = hi(top_i, d)
                bot_i = min(range(b1_i - 1, i + 1), key=lambda k: lo(k, d)); bot = lo(bot_i, d)
                crear_ob(bot_i, i, d)
                rotos = 0
                eventos.append((t[i], "CAMBIO", d))
            elif L < lo_v:
                lo_v, lo_i = L, i

        tendencia[i] = d

    if d == 1:
        sh, sl = top, bot
    else:
        sh, sl = -bot, -top
    return Resultado(tendencia, float(sh), float(sl), rotos, obs, eventos)
