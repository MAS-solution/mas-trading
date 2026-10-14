"""Simulacion de las entradas SabudFX (tipo 1/2/3) sobre velas de 5m, costos y variantes de salida.

Lo usan el scanner en vivo (scan.py) y el estudio historico (estudio.py).
"""
from bisect import bisect_left, bisect_right

import numpy as np
import pandas as pd

from estructura import analizar

# Costo ida y vuelta (spread + comision) en unidades de precio. Supuestos tipicos de FTMO.
COSTO = {"EURUSD": 0.00007, "GBPUSD": 0.00010, "USDJPY": 0.009, "USDCAD": 0.00011,
         "AUDUSD": 0.00009, "GBPJPY": 0.017, "XAUUSD": 0.30}
PIP = {"USDJPY": 0.01, "GBPJPY": 0.01, "XAUUSD": 0.1}

# Variantes de salida: (nombre, objetivo en R, mover a break even al llegar a este R, parcial 50 % en este R)
SALIDAS = [
    ("1R", 1.0, None, None),
    ("1,5R", 1.5, None, None),
    ("2R", 2.0, None, None),
    ("3R", 3.0, None, None),
    ("2R + BE en 1R", 2.0, 1.0, None),
    ("3R + BE en 1R", 3.0, 1.0, None),
    ("50 % en 1R + resto 3R con BE", 3.0, 1.0, 1.0),
]
BASE = "2R"
MAX_VELAS = 12 * 24 * 5  # una operacion abierta mas de 5 dias se cierra a mercado


def pip(par):
    return PIP.get(par, 0.0001)


def sesion(hora_utc):
    if hora_utc < 7:
        return "Asia"
    if hora_utc < 12:
        return "Londres"
    if hora_utc < 16:
        return "Londres + NY"
    if hora_utc < 21:
        return "NY tarde"
    return "Cierre / Asia"


def velas(df5):
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    df1 = df5.resample("1h").agg(agg).dropna()
    df4 = df5.resample("4h", origin="epoch").agg(agg).dropna()
    dfd = df5.resample("1D").agg(agg).dropna()
    return df1, df4, dfd


def tendencia_en(res, idx, cuando, paso):
    """Tendencia de la ultima vela CERRADA antes de `cuando`."""
    k = idx.searchsorted(cuando - paso, side="right") - 1
    return int(res.tendencia[k]) if k >= 0 else 0


def salir(d, entrada, sl, h, l, c, i0, objetivo, be, parcial):
    """Recorre desde la vela i0 (la siguiente a la entrada). Devuelve (R bruto, indice de cierre o None)."""
    riesgo = abs(entrada - sl)
    stop, cobrado, peso = sl, 0.0, 1.0
    fin = min(len(c), i0 + MAX_VELAS)
    for k in range(i0, fin):
        fav = (h[k] - entrada) / riesgo if d == 1 else (entrada - l[k]) / riesgo
        toca_stop = (l[k] <= stop) if d == 1 else (h[k] >= stop)
        if toca_stop:  # criterio conservador: si en la misma vela toca stop y objetivo, cuenta el stop
            r_stop = (stop - entrada) / riesgo * d
            return cobrado + peso * r_stop, k
        if parcial is not None and peso == 1.0 and fav >= parcial:
            cobrado, peso = 0.5 * parcial, 0.5
        if fav >= objetivo:
            return cobrado + peso * objetivo, k
        if be is not None and fav >= be:
            stop = entrada
    if fin < len(c):
        return cobrado + peso * ((c[fin - 1] - entrada) / riesgo * d), fin - 1
    return None, None  # sigue abierta al final de los datos


def simular(par, df5, df1, df4, r5, r1, r4, desde=None):
    """Recorre las velas de 5m aplicando las entradas tipo 1/2/3.

    La secuencia de operaciones (una por vez por par) se arma con la salida BASE; las demas
    variantes se evaluan sobre las mismas entradas.
    """
    paso1, paso4 = pd.Timedelta("1h"), pd.Timedelta("4h")
    t5 = df5.index
    o5, h5, l5, c5 = (df5[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    i1 = df1.index
    i4 = df4.index

    zonas = sorted([(ob.creado + (paso4 if tf == "4h" else paso1), ob, tf)
                    for tf, r in (("4h", r4), ("1h", r1)) for ob in r.obs], key=lambda z: z[0])
    ev5 = sorted(r5.eventos, key=lambda e: e[0])
    ev_t = [e[0] for e in ev5]
    obs5 = {}
    for x in r5.obs:
        obs5.setdefault((x.creado, x.d), x)

    trades, abierto_hasta, armado = [], -1, None
    activas, iz = [], 0
    i_ini = 0 if desde is None else t5.searchsorted(desde)
    base = next(s for s in SALIDAS if s[0] == BASE)
    vivo = None

    for i in range(i_ini, len(df5)):
        t = t5[i]
        while iz < len(zonas) and zonas[iz][0] <= t:
            activas.append(zonas[iz]); iz += 1
        if i % 288 == 0 and activas:
            activas = [z for z in activas if z[1].invalidado is None or z[1].invalidado > t]

        if i <= abierto_hasta:
            continue
        sesgo = tendencia_en(r4, i4, t + pd.Timedelta("5min"), paso4)
        if sesgo == 0:
            armado = None
            continue

        if armado is None or armado["d"] != sesgo:
            armado = None
            for _, ob, tf in reversed(activas):
                if ob.d != sesgo or (ob.invalidado is not None and ob.invalidado <= t):
                    continue
                if l5[i] <= ob.alto and h5[i] >= ob.bajo:
                    armado = {"d": sesgo, "ob": ob, "tf": tf, "desde": t, "extremo": None,
                              "ob5": None, "retest": False, "fract": 0}
                    break
            if armado is None:
                continue

        d = armado["d"]
        ext = l5[i] if d == 1 else h5[i]
        if armado["extremo"] is None or (ext < armado["extremo"] if d == 1 else ext > armado["extremo"]):
            armado["extremo"] = ext
        ob = armado["ob"]
        if (d == 1 and c5[i] < ob.bajo) or (d == -1 and c5[i] > ob.alto):
            armado = None
            continue

        a, b = bisect_left(ev_t, armado["desde"]), bisect_right(ev_t, t)
        a_favor = [e for e in ev5[a:b] if e[2] == d and e[1] in ("BOS", "CAMBIO")]
        if a_favor:
            en_contra = [e for e in ev5[a:b] if e[2] == -d and e[1] == "CAMBIO"]
            if en_contra and en_contra[-1][0] > a_favor[-1][0]:
                armado = None  # dos rompimientos en contra despues del fractal: setup invalido
                continue
        armado["fract"] = len(a_favor)
        if not a_favor:
            continue

        z = obs5.get((a_favor[-1][0], d))
        if z is not None and armado["ob5"] is not z:
            armado["ob5"], armado["retest"] = z, False
        z = armado["ob5"]
        if z is None:
            continue
        if not armado["retest"]:
            if t > z.creado and ((d == 1 and l5[i] <= z.alto) or (d == -1 and h5[i] >= z.bajo)):
                armado["retest"] = True
            continue
        borde = z.alto if d == 1 else z.bajo
        if not ((d == 1 and c5[i] > borde and c5[i] > o5[i]) or (d == -1 and c5[i] < borde and c5[i] < o5[i])):
            continue

        entrada = float(c5[i])
        sl = float(min(armado["extremo"], z.bajo) if d == 1 else max(armado["extremo"], z.alto))
        riesgo = abs(entrada - sl)
        if riesgo <= 0:
            continue
        alineado = tendencia_en(r1, i1, t + pd.Timedelta("5min"), paso1) == d
        tr = {"par": par, "d": d, "tipo": min(armado["fract"], 3), "tf_ob": armado["tf"],
              "entrada": entrada, "sl": sl, "abierta": t, "alineado": bool(alineado),
              "sesion": sesion(t.hour), "dia": int(t.dayofweek),
              "riesgo_pips": riesgo / pip(par), "costo_r": COSTO.get(par, 0) / riesgo,
              "salidas": {}}
        for nombre, obj, be, parcial in SALIDAS:
            r, k = salir(d, entrada, sl, h5, l5, c5, i + 1, obj, be, parcial)
            tr["salidas"][nombre] = None if r is None else (r, t5[k])
        trades.append(tr)
        armado = None
        res = tr["salidas"][base[0]]
        if res is None:
            vivo = tr
            break
        abierto_hasta = t5.searchsorted(res[1])

    # estado actual (para el tablero en vivo)
    precio = float(c5[-1])
    sesgo = tendencia_en(r4, i4, t5[-1] + pd.Timedelta("5min"), paso4)
    if vivo:
        estado = {"fase": "trade_abierto", "tipo": vivo["tipo"], "entrada": vivo["entrada"], "sl": vivo["sl"],
                  "tp": vivo["entrada"] + vivo["d"] * 2 * abs(vivo["entrada"] - vivo["sl"]),
                  "abierta": vivo["abierta"].isoformat()}
    elif armado:
        estado = {"fase": "en_ob" if not armado["fract"] else "setup",
                  "tipo": min(armado["fract"], 3) or None, "tf_ob": armado["tf"],
                  "ob": [armado["ob"].bajo, armado["ob"].alto], "desde": armado["desde"].isoformat(),
                  "esperando": "rompimiento a favor en 5m" if not armado["fract"]
                  else ("retesteo del OB de 5m" if not armado["retest"] else "cierre con cuerpo fuera de la zona")}
    else:
        validos = [(ob, tf) for _, ob, tf in activas if ob.d == sesgo and ob.invalidado is None]
        if validos and sesgo:
            def dist(ob):
                return (precio - ob.alto) if sesgo == 1 else (ob.bajo - precio)
            ob, tf = min(validos, key=lambda x: abs(dist(x[0])))
            pct = dist(ob) / precio * 100
            estado = {"fase": "acercandose" if 0 <= pct <= 0.5 else "lejos",
                      "tf_ob": tf, "ob": [ob.bajo, ob.alto], "distancia_pct": round(pct, 3)}
        else:
            estado = {"fase": "sin_ob"}
    return trades, estado


def correr(par, df5, desde=None):
    df1, df4, dfd = velas(df5)
    r4, r1, r5 = analizar(df4), analizar(df1), analizar(df5)
    trades, estado = simular(par, df5, df1, df4, r5, r1, r4, desde)
    return trades, estado, (dfd, df4, df1, r4, r1)
