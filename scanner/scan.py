"""Scanner SabudFX: estructura 4h/1h, order blocks, setups en 5m, backtest y avisos al celular.

Uso:
  python scanner/scan.py --local      # escribe salida/snapshot.json (para probar)
  python scanner/scan.py              # sube a Supabase (SUPABASE_URL + SUPABASE_SERVICE_KEY) y avisa (NTFY_TOPIC)
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
    "XAUUSD": "GC=F",  # oro: futuro de COMEX; los niveles se pasan a precio spot (ver ajuste_spot)
}
TP_R = next(s[1] for s in metodo.SALIDAS if s[0] == metodo.BASE)  # objetivo en R (oro: 1R)
AR = "America/Argentina/Buenos_Aires"
TABLERO = "https://mas-solution.github.io/mas-trading/"


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


def obs_json(r, n=4, aj=0.0):
    vivos = [o for o in r.obs if o.invalidado is None][-n:]
    return [{"d": o.d, "bajo": o.bajo + aj, "alto": o.alto + aj, "creado": o.creado.isoformat()} for o in vivos]


def nombre(d):
    return {1: "alcista", -1: "bajista", 0: "rango"}[int(d)]


def a_trade(t):
    """Trade de metodo.simular -> fila para el tablero (salida base)."""
    res = t["salidas"][metodo.BASE]
    riesgo = abs(t["entrada"] - t["sl"])
    r = None if res is None else round(res[0] - t["costo_r"], 3)
    return {"par": t["par"], "d": t["d"], "tipo": t["tipo"], "tf_ob": t["tf_ob"], "entrada": t["entrada"],
            "sl": t["sl"], "tp": t["entrada"] + t["d"] * TP_R * riesgo, "abierta": t["abierta"].isoformat(),
            "estado": "abierta" if r is None else ("ganancia" if r > 0 else "perdida"), "r": r,
            "cierre": None if res is None else res[1].isoformat(), "filtro": cumple_filtro(t)}


FILTRO = {}


def cumple_filtro(t):
    if not FILTRO:
        return None
    from estudio import FACTORES
    return all(FACTORES[k](t) in v for k, v in FILTRO.items() if k in FACTORES)


def regla_ok(d, sesion):
    """La regla de entrada aplicada a un setup que todavia no se disparo (lado + sesion actual)."""
    t = {"d": d, "sesion": sesion}
    from estudio import FACTORES
    return all(FACTORES[k](t) in v for k, v in FILTRO.items() if k in ("Dirección", "Sesión (hora UTC)"))


def precio_spot():
    """Oro spot (lo que cotiza FTMO). El futuro GC suele estar unos dolares mas arriba."""
    try:
        req = urllib.request.Request("https://api.gold-api.com/price/XAU", headers={"User-Agent": "mas-trading"})
        return float(json.load(urllib.request.urlopen(req, timeout=20))["price"])
    except Exception as e:
        print("sin precio spot:", e)
        return None


def mover(x, aj):
    """Suma `aj` a los precios de un estado o una senal."""
    for k in ("entrada", "sl", "tp", "gatillo"):
        if x.get(k) is not None:
            x[k] = x[k] + aj
    if x.get("ob"):
        x["ob"] = [v + aj for v in x["ob"]]


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
    # pasar los niveles del futuro al precio spot que se opera en FTMO
    aj = 0.0
    if par == "XAUUSD":
        spot = precio_spot()
        if spot and pd.Timestamp.now(tz="UTC") - df5.index[-1] < pd.Timedelta("30min"):
            aj = spot - float(df5.Close.iloc[-1])
    mover(estado, aj)
    for t in trades:
        mover(t, aj)
    sw = lambda v: None if v is None else v + aj  # noqa: E731
    return {
        "par": par, "precio": float(df5.Close.iloc[-1]) + aj, "actualizado": df5.index[-1].isoformat(),
        "ajuste_spot": round(aj, 2),
        "diario": nombre(rd.tendencia[-1]),
        "h4": {"tendencia": nombre(t4), "swing_alto": sw(r4.swing_alto), "swing_bajo": sw(r4.swing_bajo),
               "rompimientos_en_contra": r4.rompimientos_en_contra, "obs": obs_json(r4, aj=aj)},
        "h1": {"tendencia": nombre(t1), "swing_alto": sw(r1.swing_alto), "swing_bajo": sw(r1.swing_bajo),
               "rompimientos_en_contra": r1.rompimientos_en_contra, "obs": obs_json(r1, aj=aj)},
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


def leer(ruta):
    url = os.environ["SUPABASE_URL"].rstrip("/") + "/rest/v1/" + ruta
    key = os.environ["SUPABASE_SERVICE_KEY"]
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": "Bearer " + key})
    return json.load(urllib.request.urlopen(req, timeout=30))


def cargar_filtro():
    global FILTRO
    try:
        filas = leer("snapshot?id=eq.2&select=data->filtro")
        FILTRO = (filas[0].get("filtro") if filas else None) or {}
    except Exception as e:
        print("sin filtro del estudio:", e)


def notificar(titulo, texto, prioridad="high"):
    """Push al celular con la app ntfy. El tema (canal) va en el secreto NTFY_TOPIC, no en el codigo."""
    tema = os.environ.get("NTFY_TOPIC")
    if not tema:
        print("aviso (sin NTFY_TOPIC):", titulo, texto)
        return False
    req = urllib.request.Request("https://ntfy.sh/" + tema, data=texto.encode("utf-8"), method="POST", headers={
        "Title": titulo, "Priority": prioridad, "Tags": "moneybag", "Click": TABLERO})
    try:
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception as e:
        print("aviso no enviado:", e)
        return False


def hora_ar(iso):
    return pd.Timestamp(iso).tz_convert(AR).strftime("%H:%M")


def avisos(pares, senales, previos):
    """Push 'Preparate' (setup listo, falta la vela gatillo) y 'ENTRA YA' (senal nueva que cumple la regla).

    Devuelve las claves ya avisadas, que se guardan en el snapshot para no repetir.
    """
    hechos = set(previos)
    ahora = pd.Timestamp.now(tz="UTC")
    for p in pares:
        e = p["estado"]
        if e.get("fase") != "setup" or e.get("gatillo") is None:
            continue
        clave = f"prep-{p['par']}-{e['desde']}"
        if clave in hechos or (FILTRO and not regla_ok(e["d"], metodo.sesion(ahora.hour))):
            continue
        lado, cond = ("COMPRA", "cierre por ENCIMA de") if e["d"] == 1 else ("VENTA", "cierre por DEBAJO de")
        if notificar(f"Preparate: {p['par']} {lado}",
                     f"Setup tipo {e['tipo']} en OB de {e['tf_ob']}. Entra cuando una vela de 5m haga {cond} "
                     f"{e['gatillo']:.2f} (vela {'verde' if e['d'] == 1 else 'roja'}). "
                     f"Stop aprox {e['sl']:.2f}. Objetivo {TP_R:g}R."):
            hechos.add(clave)
    for t in senales:
        clave = f"entra-{t['par']}-{t['abierta']}"
        if clave in hechos or t["estado"] != "abierta" or (FILTRO and not t.get("filtro")):
            continue
        if ahora - pd.Timestamp(t["abierta"]) > pd.Timedelta("90min"):
            hechos.add(clave)  # ya es vieja: no avisar
            continue
        lado = "COMPRA" if t["d"] == 1 else "VENTA"
        if notificar(f"ENTRA YA: {t['par']} {lado}",
                     f"Senal de las {hora_ar(t['abierta'])} (hora AR). Entrada {t['entrada']:.2f} - "
                     f"Stop {t['sl']:.2f} - Objetivo {t['tp']:.2f}. Si el precio ya recorrio mas de la mitad "
                     f"del camino al objetivo, no entres.", prioridad="urgent"):
            hechos.add(clave)
    return sorted(hechos)[-300:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args()

    previos = []
    if not args.local:
        cargar_filtro()
        try:
            filas = leer("snapshot?id=eq.1&select=data->avisos")
            previos = (filas[0].get("avisos") if filas else None) or []
        except Exception as e:
            print("sin avisos previos:", e)
    pares, errores = [], []
    for par, ticker in PARES.items():
        try:
            pares.append(analizar_par(par, ticker))
            print(par, "ok", pares[-1]["bias"], pares[-1]["estado"], pares[-1]["backtest"])
        except Exception as e:  # un par caido no frena al resto
            errores.append(f"{par}: {e}")
            print(par, "ERROR", e)

    senales = limpiar([t for p in pares for t in p.pop("trades")])
    total = resumen(senales)
    snap = limpiar({"generado": datetime.now(timezone.utc).isoformat(), "tp_r": TP_R, "regla": FILTRO,
                    "pares": pares, "total": total, "errores": errores})

    if args.local:
        os.makedirs("salida", exist_ok=True)
        with open("salida/snapshot.json", "w", encoding="utf-8") as f:
            json.dump({**snap, "senales": senales}, f, ensure_ascii=False, indent=1)
        print("salida/snapshot.json", total)
        return

    snap["avisos"] = avisos(pares, senales, previos)
    supabase("POST", "snapshot", {"id": 1, "generado": snap["generado"], "data": snap})
    filas = [{"id": f"{t['par']}-{t['abierta']}", "par": t["par"], "d": t["d"], "tipo": t["tipo"],
              "tf_ob": t["tf_ob"], "entrada": t["entrada"], "sl": t["sl"], "tp": t["tp"],
              "abierta": t["abierta"], "estado": t["estado"], "r": t["r"], "cierre": t["cierre"],
              "filtro": t["filtro"]}
             for t in senales]
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
