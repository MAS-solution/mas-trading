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
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
from estructura import analizar  # noqa: E402
import metodo  # noqa: E402

PARES = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "USDJPY=X",
    "USDCAD": "USDCAD=X",
    "AUDUSD": "AUDUSD=X",
    "GBPJPY": "GBPJPY=X",
    "XAUUSD": "GC=F",  # oro: futuro de COMEX, sigue al spot
}
TP_R = 2.0  # salida base del tablero en vivo (el curso no define objetivo)


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


def resumen(trades):
    cerrados = [x for x in trades if x["estado"] != "abierta"]
    g = sum(1 for x in cerrados if x["r"] > 0)
    return {"operaciones": len(cerrados), "ganadas": g,
            "acierto_pct": round(g / len(cerrados) * 100, 1) if cerrados else None,
            "r_total": round(float(sum(x["r"] for x in cerrados)), 2),
            "abiertas": len(trades) - len(cerrados)}


def obs_json(r, n=4):
    vivos = [o for o in r.obs if o.invalidado is None][-n:]
    return [{"d": o.d, "bajo": o.bajo, "alto": o.alto, "creado": o.creado.isoformat()} for o in vivos]


def nombre(d):
    return {1: "alcista", -1: "bajista", 0: "rango"}[int(d)]


def a_trade(t):
    """Trade de metodo.simular -> fila para el tablero (salida base 2R)."""
    res = t["salidas"][metodo.BASE]
    riesgo = abs(t["entrada"] - t["sl"])
    r = None if res is None else round(res[0] - t["costo_r"], 3)
    return {"par": t["par"], "d": t["d"], "tipo": t["tipo"], "tf_ob": t["tf_ob"], "entrada": t["entrada"],
            "sl": t["sl"], "tp": t["entrada"] + t["d"] * 2 * riesgo, "abierta": t["abierta"].isoformat(),
            "estado": "abierta" if r is None else ("ganancia" if r > 0 else "perdida"), "r": r,
            "cierre": None if res is None else res[1].isoformat(), "filtro": cumple_filtro(t)}


FILTRO = {}


def cumple_filtro(t):
    if not FILTRO:
        return None
    from estudio import FACTORES
    return all(FACTORES[k](t) in v for k, v in FILTRO.items() if k in FACTORES)


def analizar_par(par, ticker):
    df1 = bajar(ticker, "730d", "1h")
    df5 = bajar(ticker, "60d", "5m")
    df4 = a_4h(df1)
    dfd = df1.resample("1D").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
    rd, r4, r1, r5 = analizar(dfd), analizar(df4), analizar(df1), analizar(df5)
    crudos, estado = metodo.simular(par, df5, df1, df4, r5, r1, r4)
    trades = [a_trade(t) for t in crudos]
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
        "trades": trades,
    }


def limpiar(x):
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, dict):
        return {k: limpiar(v) for k, v in x.items()}
    if isinstance(x, list):
        return [limpiar(v) for v in x]
    return x


def supabase(verbo, ruta, cuerpo, extra=None):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + ruta
    key = os.environ["SUPABASE_SERVICE_KEY"]
    req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), method=verbo, headers={
        "apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal", **(extra or {})})
    urllib.request.urlopen(req, timeout=60).read()


def cargar_filtro():
    global FILTRO
    try:
        url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/snapshot?id=eq.2&select=data->filtro"
        key = os.environ["SUPABASE_SERVICE_KEY"]
        req = urllib.request.Request(url, headers={"apikey": key, "Authorization": "Bearer " + key})
        filas = json.load(urllib.request.urlopen(req, timeout=30))
        FILTRO = (filas[0].get("filtro") if filas else None) or {}
    except Exception as e:
        print("sin filtro del estudio:", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args()

    if not args.local:
        cargar_filtro()
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
              "abierta": t["abierta"], "estado": t["estado"], "r": t["r"], "cierre": t["cierre"],
              "filtro": t["filtro"]}
             for t in limpiar(senales)]
    if filas:
        try:
            supabase("POST", "senales?on_conflict=id", filas)
        except urllib.error.HTTPError:
            # la columna `filtro` se agrega a mano en Supabase; hasta entonces se sube sin ella
            supabase("POST", "senales?on_conflict=id", [{k: v for k, v in f.items() if k != "filtro"} for f in filas])
    print("subido", total, len(filas), "senales")
    if len(errores) == len(PARES):
        sys.exit(1)


if __name__ == "__main__":
    main()
