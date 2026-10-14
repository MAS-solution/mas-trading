"""Crea un usuario que debe cambiar la clave en su primer ingreso.

Uso: SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python scripts/crear_usuario.py <usuario> <clave_inicial>
La clave no se guarda en ningun archivo.
"""
import json
import os
import sys
import urllib.request

usuario, inicial = sys.argv[1], sys.argv[2]
url = os.environ["SUPABASE_URL"].rstrip("/") + "/auth/v1/admin/users"
key = os.environ["SUPABASE_SERVICE_KEY"]
cuerpo = {"email": f"{usuario.lower()}@mastrading.app", "password": "mt-" + inicial,
          "email_confirm": True, "user_metadata": {"cambiar_clave": True, "usuario": usuario}}
req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), method="POST", headers={
    "apikey": key, "Authorization": "Bearer " + key, "Content-Type": "application/json"})
print(json.load(urllib.request.urlopen(req)).get("email"), "creado")
