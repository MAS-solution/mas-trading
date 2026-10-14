"""Baja historia de 1 minuto de HistData y la guarda como velas de 5m por par.

HistData usa hora EST fija (UTC-5, sin horario de verano). Se pasa a UTC.
Ventana movil: se guardan solo los ultimos ANIOS anios. Los anios cerrados se bajan una vez
(datos/crudo queda en cache); el mes en curso y el anterior se vuelven a bajar en cada corrida.
Uso: python scanner/historia.py [anios]
Salida: datos/<PAR>_5m.pkl (gitignored).
"""
import io
import os
import sys
import zipfile
from datetime import date

import pandas as pd
from histdata import download_hist_data as dl
from histdata.api import Platform, TimeFrame

PARES = ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "AUDUSD", "GBPJPY", "XAUUSD"]
RAIZ = os.path.join(os.path.dirname(__file__), "..", "datos")
CRUDO = os.path.join(RAIZ, "crudo")


def bajar(par, anio, mes=None, refrescar=False):
    os.makedirs(CRUDO, exist_ok=True)
    nombre = f"DAT_ASCII_{par}_M1_{anio}{'' if mes is None else f'{mes:02d}'}.zip"
    ruta = os.path.join(CRUDO, nombre)
    if os.path.exists(ruta):
        if not refrescar:
            return ruta
        os.remove(ruta)
    previo = os.getcwd()
    os.chdir(CRUDO)
    try:
        kw = {"year": str(anio), "pair": par.lower(), "platform": Platform.GENERIC_ASCII,
              "time_frame": TimeFrame.ONE_MINUTE, "verbose": False}
        if mes is not None:
            kw["month"] = str(mes)
        dl(**kw)
    finally:
        os.chdir(previo)
    return ruta if os.path.exists(ruta) else None


def leer(ruta):
    with zipfile.ZipFile(ruta) as z:
        csv = next(n for n in z.namelist() if n.endswith(".csv"))
        df = pd.read_csv(io.BytesIO(z.read(csv)), sep=";", header=None,
                         names=["t", "Open", "High", "Low", "Close", "v"])
    df.index = pd.to_datetime(df.t, format="%Y%m%d %H%M%S") + pd.Timedelta(hours=5)
    df.index = df.index.tz_localize("UTC")
    return df[["Open", "High", "Low", "Close"]]


def main():
    anios = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    hoy = date.today()
    desde = pd.Timestamp(hoy, tz="UTC") - pd.DateOffset(years=anios)
    # borrar del cache lo que ya quedo fuera de la ventana
    if os.path.isdir(CRUDO):
        for f in os.listdir(CRUDO):
            if int(f.split("_")[-1][:4]) < desde.year:
                os.remove(os.path.join(CRUDO, f))
    for par in PARES:
        partes = []
        for anio in range(desde.year, hoy.year):
            r = bajar(par, anio)
            if r:
                partes.append(leer(r))
        for mes in range(1, hoy.month + 1):  # el anio en curso viene por mes
            try:
                r = bajar(par, hoy.year, mes, refrescar=mes >= hoy.month - 1)
            except Exception:
                r = None
            if r:
                partes.append(leer(r))
        m1 = pd.concat(partes).sort_index()
        m1 = m1[~m1.index.duplicated()]
        m1 = m1[m1.index >= desde]
        m5 = m1.resample("5min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
        m5.to_pickle(os.path.join(RAIZ, f"{par}_5m.pkl"))
        print(par, len(m5), m5.index[0], m5.index[-1], flush=True)


if __name__ == "__main__":
    main()
