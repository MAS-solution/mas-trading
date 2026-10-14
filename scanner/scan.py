"""Scanner SabudFX: estructura 4h/1h, order blocks, setups en 5m y backtest.

Uso:
  python scanner/scan.py --local      # escribe salida/snapshot.json (para probar)
  python scanner/scan.py              # sube a Supabase (SUPABASE_URL + SUPABASE_SERVICE_KEY)
"""
import argparse
import json
import math
import os
import sys
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from estructura import analizar  # noqa: E402

PARES = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCAD": "USDCAD=X",
    "AUDUSD": "AUDUSD=X",
    "GBPJPY": "GBPJPY=X",
    "XAUUSD": "GC=F",  # oro: futuro de COMEX, sigue al spot
}
TP_R = 2.0        # supuesto: el curso no define objetivo; se toma 2R
CERCA_PCT = 0.5   # "acercandose al OB" si esta a menos de 0,5 %


def bajar(ticker, periodo, intervalo):
    df = yf.download(ticker, period=periodo, interval=intervalo, progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df.index = pd.to_datetime(df.index, utc=True)
    return df[~df.index.duplicated()].sort_index()


def a_4h(df1h):
    return df1h.resample("4h", origin="epoch").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()


def vigente_en(res, df, cuando):
    """Tendencia de la ultima vela CERRADA antes de `cuando`."""
    paso = df.index[1] - df.index[0] if len(df) > 1 else pd.Timedelta(0)
    idx = df.index.searchsorted(cuando - paso, side="right") - 1
    return int(res.tendencia[idx]) if idx >= 0 else 0


def simular(df5, df1, df4, r5, r1, r4):
    """Recorre las velas de 5m aplicando las entradas tipo 1/2/3. Devuelve trades y estado actual."""
    zonas = [(ob, tf) for tf, r in (("4h", r4), ("1h", r1)) for ob in r.obs]
    paso1, paso4 = pd.Timedelta("1h"), pd.Timedelta("4h")
    ev5 = sorted(r5.eventos, key=lambda e: e[0])
    obs5 = sorted(r5.obs, key=lambda o: o.creado)

    trades, abierto, armado = [], None, None
    estado = {"fase": "sin_ob"}
    t5, o5, h5, l5, c5 = df5.index, df5.Open.values, df5.High.values, df5.Low.values, df5.Close.values
    i_ev = 0

    for i in range(len(df5)):
        t = t5[i]
        sesgo = vigente_en(r4, df4, t + pd.Timedelta("5min"))
        while i_ev < len(ev5) and ev5[i_ev][0] < t:
            i_ev += 1

        # 1) trade abierto: ver si toca stop o objetivo (stop primero, criterio conservador)
        if abierto:
            d = abierto["d"]
            if (d == 1 and l5[i] <= abierto["sl"]) or (d == -1 and h5[i] >= abierto["sl"]):
                abierto.update(estado="perdida", r=-1.0, cierre=t.isoformat())
            elif (d == 1 and h5[i] >= abierto["tp"]) or (d == -1 and l5[i] <= abierto["tp"]):
                abierto.update(estado="ganancia", r=TP_R, cierre=t.isoformat())
            if abierto["estado"] != "abierta":
                abierto = None
            continue

        if sesgo == 0:
            armado = None
            continue

        # 2) armar: el precio entra a un OB de 4h/1h valido a favor del sesgo
        if armado is None or armado["d"] != sesgo:
            armado = None
            for ob, tf in zonas:
                if ob.d != sesgo or ob.creado + (paso4 if tf == "4h" else paso1) > t:
                    continue
                if ob.invalidado is not None and ob.invalidado <= t:
                    continue
                if l5[i] <= ob.alto and h5[i] >= ob.bajo:
                    armado = {"d": sesgo, "ob": ob, "tf": tf, "desde": t, "extremo": None,
                              "fractales": 0, "ob5": None, "retest": False}
                    break
            if armado is None:
                continue

        d = armado["d"]
        # extremo del retroceso (para el stop)
        ext = l5[i] if d == 1 else h5[i]
        if armado["extremo"] is None or (ext < armado["extremo"] if d == 1 else ext > armado["extremo"]):
            armado["extremo"] = ext

        # el OB de 4h/1h se rompe con cuerpo: se desarma
        ob = armado["ob"]
        if (d == 1 and c5[i] < ob.bajo) or (d == -1 and c5[i] > ob.alto):
            armado = None
            continue

        # 3) contar fractales (BOS a favor en 5m desde que toco el OB) y 2 quiebres en contra
        nuevos = [e for e in ev5 if armado["desde"] <= e[0] <= t]
        a_favor = [e for e in nuevos if e[2] == d and e[1] in ("BOS", "CAMBIO")]
        en_contra = [e for e in nuevos if e[2] == -d and e[1] == "CAMBIO"]
        if en_contra and a_favor and en_contra[-1][0] > a_favor[-1][0]:
            # dos rompimientos en contra despues del fractal: setup invalido, esperar otro toque
            armado = None
            continue
        armado["fractales"] = len(a_favor)
        if not a_favor:
            continue

        # OB de 5m del ultimo rompimiento a favor (zona de price action)
        ult = a_favor[-1][0]
        cand = [x for x in obs5 if x.d == d and x.creado == ult]
        if cand:
            if armado["ob5"] is not cand[-1]:
                armado["ob5"], armado["retest"] = cand[-1], False
        z = armado["ob5"]
        if z is None:
            continue
        borde = z.alto if d == 1 else z.bajo
        if not armado["retest"]:
            if t > z.creado and ((d == 1 and l5[i] <= z.alto) or (d == -1 and h5[i] >= z.bajo)):
                armado["retest"] = True
            continue
        # 4) entrada: cierre con cuerpo de vuelta por fuera de la zona
        if (d == 1 and c5[i] > borde and c5[i] > o5[i]) or (d == -1 and c5[i] < borde and c5[i] < o5[i]):
            entrada = float(c5[i])
            sl = float(min(armado["extremo"], z.bajo) if d == 1 else max(armado["extremo"], z.alto))
            riesgo = abs(entrada - sl)
            if riesgo <= 0:
                continue
            tipo = min(armado["fractales"], 3)
            abierto = {"d": d, "tipo": tipo, "tf_ob": armado["tf"], "entrada": entrada, "sl": sl,
                       "tp": entrada + d * TP_R * riesgo, "abierta": t.isoformat(), "estado": "abierta",
                       "r": None, "cierre": None}
            trades.append(abierto)
            armado = None

    # estado actual
    precio = float(c5[-1])
    sesgo = vigente_en(r4, df4, t5[-1] + pd.Timedelta("5min"))
    if abierto:
        estado = {"fase": "trade_abierto", **{k: abierto[k] for k in ("tipo", "entrada", "sl", "tp", "abierta")}}
    elif armado:
        estado = {"fase": "en_ob" if not armado["fractales"] else "setup",
                  "tipo": min(armado["fractales"], 3) or None, "tf_ob": armado["tf"],
                  "ob": [armado["ob"].bajo, armado["ob"].alto], "desde": armado["desde"].isoformat(),
                  "esperando": "rompimiento a favor en 5m" if not armado["fractales"]
                  else ("retesteo del OB de 5m" if not armado["retest"] else "cierre con cuerpo fuera de la zona")}
    else:
        validos = [(ob, tf) for ob, tf in zonas if ob.d == sesgo and ob.invalidado is None]
        if validos and sesgo:
            def dist(ob):
                return (precio - ob.alto) if sesgo == 1 else (ob.bajo - precio)
            ob, tf = min(validos, key=lambda x: abs(dist(x[0])))
            pct = dist(ob) / precio * 100
            estado = {"fase": "acercandose" if 0 <= pct <= CERCA_PCT else "lejos",
                      "tf_ob": tf, "ob": [ob.bajo, ob.alto], "distancia_pct": round(pct, 3)}
        else:
            estado = {"fase": "sin_ob"}
    return trades, estado


def resumen(trades):
    cerrados = [x for x in trades if x["estado"] != "abierta"]
    g = sum(1 for x in cerrados if x["r"] > 0)
    return {"operaciones": len(cerrados), "ganadas": g,
            "acierto_pct": round(g / len(cerrados) * 100, 1) if cerrados else None,
            "r_total": round(sum(x["r"] for x in cerrados), 2),
            "abiertas": len(trades) - len(cerrados)}


def obs_json(r, n=4):
    vivos = [o for o in r.obs if o.invalidado is None][-n:]
    return [{"d": o.d, "bajo": o.bajo, "alto": o.alto, "creado": o.creado.isoformat()} for o in vivos]


def nombre(d):
    return {1: "alcista", -1: "bajista", 0: "rango"}[int(d)]


def analizar_par(par, ticker):
    df1 = bajar(ticker, "730d", "1h")
    df5 = bajar(ticker, "60d", "5m")
    df4 = a_4h(df1)
    dfd = df1.resample("1D").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    rd, r4, r1 = analizar(dfd), analizar(df4), analizar(df1)
    r5 = analizar(df5)
    trades, estado = simular(df5, df1, df4, r5, r1, r4)
    t4, t1 = int(r4.tendencia[-1]), int(r1.tendencia[-1])
    if t4 == 0:
        bias = "sin definir"
    elif t4 == t1:
        bias = "compras" if t4 == 1 else "ventas"
    else:
        bias = ("ventas momentaneas, se siguen buscando compras" if t4 == 1
                else "compras momentaneas, se siguen buscando ventas")
    return {
        "par": par, "precio": float(df5.Close.iloc[-1]), "actualizado": df5.index[-1].isoformat(),
        "diario": nombre(rd.tendencia[-1]),
        "h4": {"tendencia": nombre(t4), "swing_alto": r4.swing_alto, "swing_bajo": r4.swing_bajo,
               "rompimientos_en_contra": r4.rompimientos_en_contra, "obs": obs_json(r4)},
        "h1": {"tendencia": nombre(t1), "swing_alto": r1.swing_alto, "swing_bajo": r1.swing_bajo,
               "rompimientos_en_contra": r1.rompimientos_en_contra, "obs": obs_json(r1)},
        "bias": bias, "estado": estado,
        "backtest": {**resumen(trades), "desde": df5.index[0].isoformat()},
        "trades": [{**x, "par": par} for x in trades],
    }


def limpiar(x):
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, dict):
        return {k: limpiar(v) for k, v in x.items()}
    if isinstance(x, list):
        return [limpiar(v) for v in x]
    return x


def supabase(metodo, ruta, cuerpo, extra=None):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + ruta
    key = os.environ["SUPABASE_SERVICE_KEY"]
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), method=metodo, headers={
        "apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal", **(extra or {})})
    urllib.request.urlopen(req, timeout=60).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args()

    pares, errores = [], []
    for par, ticker in PARES.items():
        try:
            pares.append(analizar_par(par, ticker))
            print(par, "ok", pares[-1]["bias"], pares[-1]["estado"]["fase"], pares[-1]["backtest"])
        except Exception as e:  # un par caido no frena al resto
            errores.append(f"{par}: {e}")
            print(par, "ERROR", e)

    senales = [t for p in pares for t in p.pop("trades")]
    total = resumen(senales)
    snap = limpiar({"generado": datetime.now(timezone.utc).isoformat(), "tp_r": TP_R,
                    "pares": pares, "total": total, "errores": errores})

    if args.local:
        os.makedirs("salida", exist_ok=True)
        with open("salida/snapshot.json", "w", encoding="utf-8") as f:
            json.dump({**snap, "senales": limpiar(senales)}, f, ensure_ascii=False, indent=1)
        print("salida/snapshot.json", total)
        return

    supabase("POST", "snapshot", {"id": 1, "generado": snap["generado"], "data": snap})
    filas = [{"id": f"{t['par']}-{t['abierta']}", "par": t["par"], "d": t["d"], "tipo": t["tipo"],
              "tf_ob": t["tf_ob"], "entrada": t["entrada"], "sl": t["sl"], "tp": t["tp"],
              "abierta": t["abierta"], "estado": t["estado"], "r": t["r"], "cierre": t["cierre"]}
             for t in limpiar(senales)]
    if filas:
        supabase("POST", "senales?on_conflict=id", filas)
    print("subido", total, len(filas), "senales")
    if len(errores) == len(PARES):
        sys.exit(1)


if __name__ == "__main__":
    main()
