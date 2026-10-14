"""Estudio historico del metodo: desglose por condiciones, salidas y simulador FTMO.

Usa datos/<PAR>_5m.pkl (scanner/historia.py). Separa la historia en dos:
- muestra de aprendizaje (todo lo anterior a los ultimos 18 meses): ahi se eligen filtros y salida;
- muestra de control (ultimos 18 meses): no se usa para elegir nada, solo para ver si se sostiene.

Uso: python scanner/estudio.py [--local]
"""
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import metodo  # noqa: E402

DATOS = os.path.join(os.path.dirname(__file__), "..", "datos")
PARES = ["XAUUSD"]  # foco en oro (para volver a forex: agregar pares y poner REGLA = None)
# Regla de entrada para oro. Se eligio mirando los 5 anios (aprendizaje y control), asi que el control
# ya no es una prueba ciega: la prueba real son las senales en vivo desde el 14/10/2026.
REGLA = {"Dirección": ["Compra"], "Sesión (hora UTC)": ["Cierre / Asia", "Londres", "Londres + NY", "NY tarde"]}
REGLA_DESDE = "2026-10-14"
MESES_CONTROL = 18  # los ultimos 18 meses son la muestra de control
MIN_N = 40
CUENTA = 10_000
RIESGOS = [0.5, 1.0, 1.5, 2.0]
DIAS = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
FTMO = {
    "2-Step": {"fases": [0.10, 0.05], "diaria": 0.05, "total": 0.10, "min_dias": 4, "arrastre": False, "mejor_dia": None},
    "1-Step": {"fases": [0.10], "diaria": 0.03, "total": 0.10, "min_dias": 0, "arrastre": True, "mejor_dia": 0.5},
}


def costo_tramo(x):
    if x < 0.05:
        return "costo < 5 % del riesgo"
    if x < 0.10:
        return "costo 5-10 %"
    if x < 0.20:
        return "costo 10-20 %"
    return "costo > 20 %"


FACTORES = {
    "Par": lambda t: t["par"],
    "Dirección": lambda t: "Compra" if t["d"] == 1 else "Venta",
    "Tipo de entrada": lambda t: f"Tipo {t['tipo']}",
    "OB de": lambda t: t["tf_ob"],
    "4h y 1h alineados": lambda t: "Sí" if t["alineado"] else "No",
    "Sesión (hora UTC)": lambda t: t["sesion"],
    "Día": lambda t: DIAS[t["dia"]],
    "Costo vs stop": lambda t: costo_tramo(t["costo_r"]),
}


def correr_par(par):
    df5 = pd.read_pickle(os.path.join(DATOS, f"{par}_5m.pkl"))
    trades, _, _ = metodo.correr(par, df5)
    return [t for t in trades if t["salidas"][metodo.BASE] is not None]


def r_neto(t, salida):
    s = t["salidas"][salida]
    return None if s is None else s[0] - t["costo_r"]


def stats(trades, salida):
    rs = [r for r in (r_neto(t, salida) for t in trades) if r is not None]
    if not rs:
        return {"n": 0, "acierto": None, "esperanza": None, "r_total": 0}
    return {"n": len(rs), "acierto": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1),
            "esperanza": round(float(np.mean(rs)), 3), "r_total": round(float(np.sum(rs)), 1)}


def elegir_filtro(apr, min_ops=150, max_condiciones=3):
    """Seleccion paso a paso sobre el periodo de aprendizaje.

    En cada paso se prueba restringir cada factor a los niveles que ganan en las DOS mitades del
    aprendizaje; se queda la restriccion que mas mejora, siempre que mejore en ambas mitades y
    deje al menos `min_ops` operaciones. Asi se evita elegir lo que gano por casualidad.
    """
    medio = sorted(t["abierta"] for t in apr)[len(apr) // 2]
    esp = lambda g: stats(g, metodo.BASE)["esperanza"] or -9  # noqa: E731
    filtro, actual = {}, list(apr)
    for _ in range(max_condiciones):
        mitades = ([t for t in actual if t["abierta"] < medio], [t for t in actual if t["abierta"] >= medio])
        base = [esp(m) for m in mitades]
        mejor = None
        for nombre, f in FACTORES.items():
            if nombre in filtro:
                continue
            niveles = {f(t) for t in actual}
            ok = sorted(k for k in niveles
                        if all(len([t for t in m if f(t) == k]) >= 15 and esp([t for t in m if f(t) == k]) > 0
                               for m in mitades))
            if not ok or len(ok) == len(niveles):
                continue
            quedan = [t for t in actual if f(t) in ok]
            nuevas = [esp([t for t in m if f(t) in ok]) for m in mitades]
            if len(quedan) < min_ops or not all(n > b for n, b in zip(nuevas, base)):
                continue
            ganancia = esp(quedan) - esp(actual)
            if mejor is None or ganancia > mejor[0]:
                mejor = (ganancia, nombre, ok, quedan)
        if mejor is None:
            break
        _, nombre, ok, actual = mejor
        filtro[nombre] = ok
    return filtro


def cumple(t, filtro):
    return all(FACTORES[k](t) in v for k, v in filtro.items())


def curva(trades, salida):
    rs = sorted(((t["salidas"][salida][1], r_neto(t, salida)) for t in trades if t["salidas"][salida]),
                key=lambda x: x[0])
    acum, pico, dd, racha, peor_racha, meses = 0.0, 0.0, 0.0, 0, 0, defaultdict(float)
    for cuando, r in rs:
        acum += r
        pico = max(pico, acum)
        dd = max(dd, pico - acum)
        racha = racha + 1 if r < 0 else 0
        peor_racha = max(peor_racha, racha)
        meses[cuando.strftime("%Y-%m")] += r
    return {"caida_max_r": round(dd, 1), "peor_racha": peor_racha,
            "meses": [[m, round(v, 2)] for m, v in sorted(meses.items())]}


def simular_ftmo(trades, salida, regla, riesgo_pct, fin_datos):
    """Un intento por cada lunes. Devuelve conteos de resultado y dias hasta pasar."""
    ops = sorted(((t["abierta"], t["salidas"][salida][1], r_neto(t, salida)) for t in trades
                  if t["salidas"][salida]), key=lambda x: x[1])
    if not ops:
        return None
    praga = "Europe/Prague"
    riesgo = CUENTA * riesgo_pct / 100
    lunes = pd.date_range(min(o[0] for o in ops).normalize(), fin_datos - pd.Timedelta(days=30), freq="W-MON")
    res = defaultdict(int)
    dias_pasar = []
    for inicio in lunes:
        saldo, fase, piso = CUENTA, 0, CUENTA * (1 - regla["total"])
        dia_actual, saldo_inicio_dia, dias_op, ganancia_dia = None, CUENTA, set(), defaultdict(float)
        base_fase = CUENTA
        estado = "sin terminar"
        for abierta, cerrada, r in ops:
            if abierta < inicio:
                continue
            dia = cerrada.tz_convert(praga).date()
            if dia != dia_actual:
                if regla["arrastre"] and dia_actual is not None:
                    piso = min(CUENTA, max(piso, saldo - CUENTA * regla["total"]))
                dia_actual, saldo_inicio_dia = dia, saldo
            dias_op.add(abierta.tz_convert(praga).date())
            saldo += r * riesgo
            ganancia_dia[dia] += r * riesgo
            if saldo < saldo_inicio_dia - CUENTA * regla["diaria"]:
                estado = "falla por pérdida diaria"
                break
            if saldo < piso:
                estado = "falla por pérdida total"
                break
            objetivo = base_fase + CUENTA * regla["fases"][fase]
            ganado = saldo - base_fase
            mejor_ok = regla["mejor_dia"] is None or max(ganancia_dia.values()) <= regla["mejor_dia"] * ganado
            if saldo >= objetivo and len(dias_op) >= regla["min_dias"] and mejor_ok:
                fase += 1
                if fase == len(regla["fases"]):
                    estado = "pasa"
                    dias_pasar.append((cerrada - inicio).days)
                    break
                # nueva fase: cuenta nueva desde cero
                saldo, base_fase, piso = CUENTA, CUENTA, CUENTA * (1 - regla["total"])
                dias_op, ganancia_dia, saldo_inicio_dia = set(), defaultdict(float), CUENTA
        res[estado] += 1
    total = sum(res.values())
    pct = lambda k: round(100 * res[k] / total, 1) if total else None  # noqa: E731
    return {"riesgo_pct": riesgo_pct, "intentos": total, "pasa_pct": pct("pasa"),
            "falla_diaria_pct": pct("falla por pérdida diaria"), "falla_total_pct": pct("falla por pérdida total"),
            "sin_terminar_pct": pct("sin terminar"),
            "dias_mediana": int(np.median(dias_pasar)) if dias_pasar else None}


def main():
    with ProcessPoolExecutor(max_workers=min(7, os.cpu_count() or 2)) as ex:
        por_par = dict(zip(PARES, ex.map(correr_par, PARES)))
    todos = [t for ts in por_par.values() for t in ts]
    fin_datos = max(t["abierta"] for t in todos)
    CORTE = (fin_datos - pd.DateOffset(months=MESES_CONTROL)).normalize()
    apr = [t for t in todos if t["abierta"] < CORTE]
    ctrl = [t for t in todos if t["abierta"] >= CORTE]
    print("operaciones", len(todos), "aprendizaje", len(apr), "control", len(ctrl), flush=True)

    filtro = REGLA if REGLA is not None else elegir_filtro(apr)
    apr_f = [t for t in apr if cumple(t, filtro)]
    ctrl_f = [t for t in ctrl if cumple(t, filtro)]
    salidas = [{"salida": s[0], "aprendizaje": stats(apr_f, s[0]), "control": stats(ctrl_f, s[0])}
               for s in metodo.SALIDAS]
    mejor = metodo.BASE if REGLA is not None else max(salidas, key=lambda s: s["aprendizaje"]["esperanza"] or -9)["salida"]

    factores = {}
    for nombre, f in FACTORES.items():
        niveles = sorted({f(t) for t in todos})
        factores[nombre] = [{"nivel": k,
                             "aprendizaje": stats([t for t in apr if f(t) == k], metodo.BASE),
                             "control": stats([t for t in ctrl if f(t) == k], metodo.BASE),
                             "elegido": nombre not in filtro or k in filtro[nombre]} for k in niveles]

    ftmo = {}
    for nombre, regla in FTMO.items():
        ftmo[nombre] = {
            "control": [simular_ftmo(ctrl_f, mejor, regla, r, fin_datos) for r in RIESGOS],
            "toda_la_historia": [simular_ftmo(apr_f + ctrl_f, mejor, regla, r, fin_datos) for r in RIESGOS],
        }

    datos_desde = min(t["abierta"] for t in todos)
    estudio = {
        "generado": pd.Timestamp.now(tz="UTC").isoformat(),
        "periodo": [datos_desde.isoformat(), fin_datos.isoformat()], "corte": CORTE.isoformat(),
        "costos": metodo.COSTO, "cuenta": CUENTA,
        "filtro": filtro, "salida_elegida": mejor, "pares": PARES,
        "regla_fija": REGLA is not None, "regla_desde": REGLA_DESDE,
        "resumen": {
            "sin_filtro": {"aprendizaje": stats(apr, metodo.BASE), "control": stats(ctrl, metodo.BASE)},
            "con_filtro": {"aprendizaje": stats(apr_f, mejor), "control": stats(ctrl_f, mejor)},
        },
        "riesgo": {"aprendizaje": curva(apr_f, mejor), "control": curva(ctrl_f, mejor)},
        "salidas": salidas, "factores": factores, "ftmo": ftmo,
    }
    print(json.dumps({k: estudio[k] for k in ("filtro", "salida_elegida", "resumen")}, ensure_ascii=False, indent=1))
    for nombre, v in ftmo.items():
        for x in v["control"]:
            print(nombre, x)

    os.makedirs("salida", exist_ok=True)
    with open("salida/estudio.json", "w", encoding="utf-8") as f:
        json.dump(estudio, f, ensure_ascii=False, indent=1, default=str)
    if "--local" not in sys.argv:
        from scan import supabase
        supabase("POST", "snapshot", {"id": 2, "generado": estudio["generado"],
                                      "data": json.loads(json.dumps(estudio, default=str))})
        print("subido a Supabase")


if __name__ == "__main__":
    main()
