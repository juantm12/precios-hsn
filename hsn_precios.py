"""Precios HSN.

Consulta en hsnstore.com los productos de productos.json, guarda cada consulta
en historial.json, decide si conviene comprar o esperar y genera precios.html.

El PC y GitHub comparten el historial a través del gist secreto de productos.json
("gist"): tras cada consulta se suben ahí historial.json, productos.json y web.json,
que es lo que lee la página del móvil ("web_movil", en GitHub Pages). GitHub también
consulta HSN cada día (.github/workflows/precios.yml, con --nube), así que los precios
se actualizan aunque el PC esté apagado. Todo con el gh de GitHub, sin tokens de Claude.

Uso: python hsn_precios.py                    (o doble clic en actualizar.bat)
     python hsn_precios.py --abrir            (además abre la página al terminar la consulta)
     python hsn_precios.py --diaria           (las consultas programadas: consulta si ya son las 10 y hoy
                                               todavía no se ha hecho; si no, se pone al día con GitHub)
     python hsn_precios.py --solo-pagina      (rehace las páginas sin consultar HSN;
                                               con --subir, también sube los datos al móvil)
     python hsn_precios.py --publicar-pagina  (sube plantilla.html y este script a GitHub tras cambiarlos)
     python hsn_precios.py --nube [--diaria]  (en GitHub Actions: lee y guarda los datos en el gist)
"""
import base64
import gzip
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
PRODUCTOS = CARPETA / "productos.json"
HISTORIAL = CARPETA / "historial.json"
PLANTILLA = CARPETA / "plantilla.html"
PAGINA = CARPETA / "precios.html"
WEB_JSON = CARPETA / "web.json"   # los datos que lee la página del móvil
ESTADO = CARPETA / "estado.json"  # qué datos tiene ya la página del móvil
REGISTRO = CARPETA / "registro.log"

# Días de historial que se suben al móvil (para que la página cargue rápido).
DIAS_WEB = 400

# La consulta diaria se hace la primera vez que la tarea se ejecuta a partir de esta hora.
HORA_DIARIA = 10

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

# Los precios de hace más de un año ya no sirven para decidir (HSN ha subido mucho los precios).
VENTANA_DIAS = 365
# Salto mínimo entre precios para separar "precio bueno" de "precio normal".
SALTO_MINIMO = 0.15
# Si estuvo a precio bueno hace menos de estos días, merece la pena esperar.
DIAS_BAJADA_RECIENTE = 45
# Subidas terminadas que hacen falta para estimar cuándo bajará.
SUBIDAS_MINIMAS = 2


# ---------------------------------------------------------------- utilidades

def normaliza(texto):
    """'Café con Leche' -> 'cafeconleche', para comparar sin tildes, mayúsculas ni espacios."""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


def clave(producto):
    slug = producto["url"].rstrip("/").rsplit("/", 1)[-1]
    return "|".join([slug] + [normaliza(producto[k]) for k in ("formato", "sabor") if producto.get(k)])


def euros(x):
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def minuscula(texto):
    return texto[:1].lower() + texto[1:]


def dia(iso):
    return date.fromisoformat(iso[:10])


def fecha_corta(iso):
    meses = "ene feb mar abr may jun jul ago sept oct nov dic".split()
    d = dia(iso)
    return f"{d.day} {meses[d.month - 1]} {d.year}"


def lee_json(ruta, defecto):
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else defecto


def guarda_json(ruta, datos):
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ruta)


def anota(texto):
    with REGISTRO.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}  {texto}\n")


# ------------------------------------------------------------ leer HSN

def descarga(url):
    cabeceras = {"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9", "Accept-Encoding": "gzip",
                 "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=cabeceras), timeout=40) as r:
            datos = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                datos = gzip.decompress(datos)
            return datos.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code != 403:
            raise
    # Si Cloudflare rechaza a Python, se prueba con el curl de Windows.
    r = subprocess.run(["curl", "-s", "-f", "--compressed", "--max-time", "40", "-A", UA,
                        "-H", "Accept-Language: es-ES,es;q=0.9", url],
                       capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(f"HSN no deja descargar la página (curl terminó con código {r.returncode})")
    return r.stdout.decode("utf-8", errors="replace")


def config_variantes(html):
    """JSON de variantes de Magento: el objeto que trae 'attributes' y 'optionPrices'."""
    decodificador = json.JSONDecoder()
    for m in re.finditer(r'"optionPrices"', html):
        inicio = m.start()
        for _ in range(400):
            inicio = html.rfind("{", 0, inicio)
            if inicio < 0:
                break
            try:
                obj, _fin = decodificador.raw_decode(html, inicio)
            except ValueError:
                continue
            if isinstance(obj, dict) and "optionPrices" in obj and "attributes" in obj:
                return obj
    return None


def producto_jsonld(html):
    for bloque in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            datos = json.loads(bloque)
        except ValueError:
            continue
        for obj in datos if isinstance(datos, list) else [datos]:
            if isinstance(obj, dict) and obj.get("@type") == "Product":
                return obj
    return None


def lee_precio(html, producto):
    """Precio, PVP y stock de la variante (formato/sabor) indicada en productos.json."""
    buscadas = [producto[k] for k in ("formato", "sabor") if producto.get(k)]
    cfg = config_variantes(html)
    if cfg:
        if not buscadas:
            raise ValueError("el producto tiene variantes: indica 'formato' y/o 'sabor' en productos.json")
        opciones = [o for a in cfg["attributes"].values() for o in a.get("options", [])]
        candidatos = None
        for valor in buscadas:
            ids = {pid for o in opciones if normaliza(o.get("label")) == normaliza(valor)
                   for pid in o.get("products", [])}
            if not ids:
                validas = ", ".join(sorted({o["label"] for o in opciones}))
                raise ValueError(f"no existe «{valor}». Opciones en la web: {validas}")
            candidatos = ids if candidatos is None else candidatos & ids
        candidatos = sorted(c for c in candidatos if c in cfg["optionPrices"])
        if len(candidatos) != 1:
            raise ValueError(f"la combinación {' + '.join(buscadas)} no está a la venta")
        pid = candidatos[0]
        precios = cfg["optionPrices"][pid]
        final = precios["finalPrice"]["amount"]
        return {"precio": round(final, 2),
                "pvp": round(precios.get("oldPrice", {}).get("amount", final), 2),
                "stock": (cfg.get("stockQty") or {}).get(pid)}
    oferta = (producto_jsonld(html) or {}).get("offers") or {}
    if isinstance(oferta, list):
        oferta = oferta[0] if oferta else {}
    precio = oferta.get("price") or oferta.get("lowPrice")
    if precio is None:
        raise ValueError("no encuentro el precio en la página")
    agotado = "OutOfStock" in str(oferta.get("availability", ""))
    return {"precio": round(float(precio), 2), "pvp": None, "stock": 0 if agotado else None}


def consulta(productos):
    paginas, precios = {}, {}
    for producto in productos:
        try:
            if producto["url"] not in paginas:
                paginas[producto["url"]] = descarga(producto["url"])
            precios[clave(producto)] = lee_precio(paginas[producto["url"]], producto)
        except Exception as e:  # un producto que falla no tumba al resto
            precios[clave(producto)] = {"error": str(e)}
    return precios


# ------------------------------------------------------------ análisis

def observaciones(historial, k):
    """Todos los precios conocidos de un producto, del más antiguo al más reciente."""
    obs = []
    for reg in historial["registros"]:
        dato = reg["precios"].get(k)
        if dato and dato.get("precio") is not None:
            obs.append({"fecha": reg["fecha"], "precio": dato["precio"], "pvp": dato.get("pvp"),
                        "stock": dato.get("stock"), "tipo": reg.get("tipo", "consulta"),
                        "fuente": reg.get("fuente", ""), "url": reg.get("url"),
                        "nota": dato.get("nota") or reg.get("nota")})
    return sorted(obs, key=lambda o: o["fecha"])


def serie_diaria(obs):
    """Un precio por día consultado (el de la última consulta de ese día)."""
    dias = {}
    for o in obs:
        if o["tipo"] == "consulta":
            dias[o["fecha"][:10]] = o["precio"]
    return sorted(dias.items())


def corte_bajo(precios):
    """Precio más alto del grupo barato: busca el mayor salto entre precios ordenados.

    None si no hay ningún salto de al menos SALTO_MINIMO (precio estable).
    """
    ordenados = sorted(set(precios))
    mejor, corte = 0.0, None
    for a, b in zip(ordenados, ordenados[1:]):
        if (b - a) / a > mejor:
            mejor, corte = (b - a) / a, a
    return corte if mejor >= SALTO_MINIMO else None


def rachas(diaria, es_bueno):
    """Tramos seguidos a precio bueno / no bueno en la serie diaria."""
    tramos = []
    for d, precio in diaria:
        bueno = es_bueno(precio)
        if tramos and tramos[-1]["bueno"] == bueno:
            continue
        if tramos:
            tramos[-1]["fin"] = d
        tramos.append({"bueno": bueno, "inicio": d, "fin": None})
    return tramos


def duracion(tramo):
    return (date.fromisoformat(tramo["fin"]) - date.fromisoformat(tramo["inicio"])).days


def fiabilidad(n):
    return "baja" if n < 3 else "media" if n < 5 else "alta"


def recomienda(obs, ahora):
    """Compara el precio de hoy con los precios del último año y decide: comprar, esperar o normal."""
    desde = (ahora - timedelta(days=VENTANA_DIAS)).isoformat()
    ventana = [o for o in obs if o["fecha"] >= desde]
    consultas = [o for o in ventana if o["tipo"] == "consulta"]
    if not consultas:
        return {"accion": "sin_datos", "titulo": "Sin datos todavía",
                "detalle": ["Aún no hay ninguna consulta reciente de este producto."]}
    actual = consultas[-1]["precio"]
    diaria = serie_diaria(ventana)
    precios = [p for _d, p in diaria] + [o["precio"] for o in ventana if o["tipo"] != "consulta"]
    corte = corte_bajo(precios)
    if corte is None:
        return {"accion": "comprar", "titulo": "Precio estable",
                "detalle": ["En el último año no ha tenido bajadas importantes: da igual cuándo lo compres."]}

    referencia = statistics.median([p for p in precios if p <= corte])
    ultimo_bueno = [o for o in ventana if o["precio"] <= corte][-1]
    dias_desde = (ahora.date() - dia(ultimo_bueno["fecha"])).days
    tramos = rachas(diaria, lambda p: p <= corte)
    rec = {"umbral": corte, "referencia": referencia}

    if actual <= corte:
        bajadas = [duracion(t) for t in tramos if t["bueno"] and t["fin"]]
        rec |= {"accion": "comprar", "titulo": "Buen precio: compra",
                "detalle": [f"Está a precio bueno (hasta {euros(corte)}). Lo más barato del último año fue "
                            f"{euros(min(precios))}."]}
        if len(bajadas) >= SUBIDAS_MINIMAS:
            rec["prevision"] = (f"Las bajadas suelen durar unos {round(statistics.median(bajadas))} días "
                                f"(fiabilidad {fiabilidad(len(bajadas))}).")
        return rec

    rec["ahorro"] = round(actual - referencia, 2)
    if ultimo_bueno["tipo"] == "consulta":
        origen = "en HSN"
    else:
        nota = f", {ultimo_bueno['nota']}" if ultimo_bueno.get("nota") else ""
        origen = f"({minuscula(ultimo_bueno['fuente'])}{nota})"
    cuando = "hoy" if dias_desde == 0 else "ayer" if dias_desde == 1 else f"hace {dias_desde} días"
    if dias_desde <= DIAS_BAJADA_RECIENTE:
        rec |= {"accion": "esperar", "titulo": "Mejor espera",
                "detalle": [f"{cuando.capitalize()} estaba a {euros(ultimo_bueno['precio'])} {origen}. "
                            f"Si puedes esperar, es probable que vuelva a bajar: te ahorrarías unos "
                            f"{euros(rec['ahorro'])}."]}
    else:
        rec |= {"accion": "normal", "titulo": "Precio normal",
                "detalle": [f"Bajó a {euros(ultimo_bueno['precio'])} el {fecha_corta(ultimo_bueno['fecha'])} "
                            f"{origen}, pero desde entonces no se ha vuelto a ver tan barato. Si lo necesitas, "
                            f"cómpralo; si puedes esperar a una promo fuerte, ahorrarías unos "
                            f"{euros(rec['ahorro'])}."]}

    subidas = [duracion(t) for t in tramos if not t["bueno"] and t["fin"]]
    abierta = tramos[-1] if tramos and not tramos[-1]["bueno"] else None
    if len(subidas) >= SUBIDAS_MINIMAS and abierta:
        tipica = round(statistics.median(subidas))
        lleva = (ahora.date() - date.fromisoformat(abierta["inicio"])).days
        falta = tipica - lleva
        if falta > 0:
            estimada = ahora.date() + timedelta(days=falta)
            rec["prevision"] = (f"Las subidas suelen durar unos {tipica} días y esta lleva {lleva}: podría "
                                f"bajar hacia el {fecha_corta(estimada.isoformat())} (fiabilidad "
                                f"{fiabilidad(len(subidas))}, con {len(subidas)} subidas registradas).")
        else:
            rec["prevision"] = (f"Las subidas suelen durar unos {tipica} días y esta ya lleva {lleva}: "
                                f"podría bajar en cualquier momento (fiabilidad {fiabilidad(len(subidas))}).")
    else:
        rec["prevision"] = ("Todavía no hay suficientes consultas diarias para estimar cuándo bajará. "
                            "Con unas semanas de datos lo irá calculando solo.")
    return rec


def analiza(config, historial, ahora):
    consultas = [r for r in historial["registros"] if r.get("tipo", "consulta") == "consulta"]
    productos = []
    for p in config["productos"]:
        k = clave(p)
        obs = observaciones(historial, k)
        propias = [o for o in obs if o["tipo"] == "consulta"]
        ultima_reg = next((r for r in reversed(consultas) if k in r["precios"]), None)
        error = ultima_reg["precios"][k].get("error") if ultima_reg else None
        desde = (ahora - timedelta(days=VENTANA_DIAS)).isoformat()
        ventana = [o for o in obs if o["fecha"] >= desde]
        productos.append({
            "clave": k,
            "nombre": p["nombre"],
            "variante": " · ".join(p[c] for c in ("formato", "sabor") if p.get(c)),
            "cantidad": p.get("cantidad", 1),
            "url": p["url"],
            "actual": propias[-1] if propias else None,
            "anterior": propias[-2] if len(propias) > 1 else None,
            "error": error,
            "minimo": min(ventana, key=lambda o: o["precio"]) if ventana else None,
            "maximo": max(ventana, key=lambda o: o["precio"]) if ventana else None,
            "observaciones": obs,
            "recomendacion": recomienda(obs, ahora),
        })

    cesta = [p for p in productos if p["cantidad"] > 0]
    def total(clave_precio):
        if not cesta or any(p[clave_precio] is None for p in cesta):
            return None
        return round(sum(p["cantidad"] * p[clave_precio]["precio"] for p in cesta), 2)

    serie = []
    for reg in consultas:
        datos = [reg["precios"].get(p["clave"], {}).get("precio") for p in cesta]
        if cesta and all(d is not None for d in datos):
            serie.append({"fecha": reg["fecha"],
                          "total": round(sum(p["cantidad"] * d for p, d in zip(cesta, datos)), 2)})

    extras = config.get("extras", [])
    total_actual = total("actual")
    envio = None
    if total_actual is not None and config.get("envio_gratis_desde"):
        falta = config["envio_gratis_desde"] - total_actual
        envio = ("Envío gratis" if falta <= 0 else
                 f"Te faltan {euros(falta)} para el envío gratis (desde {euros(config['envio_gratis_desde'])})")

    esperar = [p for p in cesta if p["recomendacion"]["accion"] == "esperar"]
    if esperar:
        ahorro = sum(p["cantidad"] * p["recomendacion"]["ahorro"] for p in esperar)
        nombres = " y ".join(p["nombre"] for p in esperar)
        verbo = "vuelva" if len(esperar) == 1 else "vuelvan"
        rec_cesta = {"accion": "esperar", "titulo": "Si puedes, espera",
                     "detalle": [f"Te ahorrarías unos {euros(ahorro)} esperando a que {verbo} a su precio "
                                 f"bueno: {nombres}."]}
    elif cesta and all(p["recomendacion"]["accion"] == "comprar" for p in cesta):
        rec_cesta = {"accion": "comprar", "titulo": "Buen momento para comprar",
                     "detalle": ["Todo lo de tu cesta está a buen precio."]}
    else:
        rec_cesta = {"accion": "normal", "titulo": "Precios normales",
                     "detalle": ["Nada está especialmente barato, pero tampoco hay una bajada reciente que esperar."]}

    return {
        "generado": ahora.isoformat(timespec="seconds"),
        "ultimaConsulta": consultas[-1]["fecha"] if consultas else None,
        "consultaAnterior": consultas[-2]["fecha"] if len(consultas) > 1 else None,
        "productos": productos,
        "cesta": {
            "total": total_actual,
            "anterior": total("anterior"),
            "extras": extras,
            "totalConExtras": (round(total_actual + sum(e["importe"] for e in extras), 2)
                               if total_actual is not None else None),
            "envio": envio,
            "serie": serie,
            "recomendacion": rec_cesta,
        },
    }


# ------------------------------------------------------------ salida

def para_web(analisis, ahora):
    """Copia recortada para el móvil: últimos DIAS_WEB días y una consulta por día."""
    desde = (ahora - timedelta(days=DIAS_WEB)).isoformat()
    web = json.loads(json.dumps(analisis))
    for p in web["productos"]:
        recientes = [o for o in p["observaciones"] if o["fecha"] >= desde]
        por_dia = {o["fecha"][:10]: o for o in recientes if o["tipo"] == "consulta"}
        otras = [o for o in recientes if o["tipo"] != "consulta"]
        p["observaciones"] = sorted(otras + list(por_dia.values()), key=lambda o: o["fecha"])
    web["cesta"]["serie"] = [s for s in web["cesta"]["serie"] if s["fecha"] >= desde]
    return web


def genera_paginas(analisis, ahora):
    if PLANTILLA.exists():  # en GitHub no hay página del PC: solo hacen falta los datos del móvil
        datos = json.dumps(analisis, ensure_ascii=False).replace("</", "<\\/")
        PAGINA.write_text(PLANTILLA.read_text(encoding="utf-8").replace("/*__DATOS__*/null", datos), encoding="utf-8")
    WEB_JSON.write_text(json.dumps(para_web(analisis, ahora), ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------ página del móvil (GitHub)

def gh(*args, entrada=None):
    """Ejecuta el gh de GitHub sin abrir ventana. Devuelve (ok, salida)."""
    exe = shutil.which("gh")
    if not exe:
        return False, "no encuentro gh (GitHub CLI) en este PC"
    try:
        r = subprocess.run([exe, *args], input=entrada, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "GitHub no ha respondido en 1 minuto"
    salida = (r.stdout if r.returncode == 0 else r.stderr or r.stdout).decode("utf-8", errors="replace")
    return r.returncode == 0, salida.strip()


def lee_gist(id_gist):
    """Los archivos del gist secreto, como {nombre: texto}."""
    ok, salida = gh("api", f"gists/{id_gist}")
    if not ok:
        raise RuntimeError((salida or "error desconocido")[:300])
    archivos = {}
    for nombre, f in json.loads(salida)["files"].items():
        if f.get("truncated"):  # a partir de 1 MB la API no trae el contenido: se descarga aparte
            with urllib.request.urlopen(f["raw_url"], timeout=40) as r:
                archivos[nombre] = r.read().decode("utf-8")
        else:
            archivos[nombre] = f["content"]
    return archivos


def sube_gist(id_gist, archivos):
    """Guarda en el gist secreto los archivos {nombre: ruta}. Devuelve None o el error."""
    cuerpo = {"files": {nombre: {"content": ruta.read_text(encoding="utf-8")} for nombre, ruta in archivos.items()}}
    ok, salida = gh("api", "--method", "PATCH", f"gists/{id_gist}", "--input", "-", "--silent",
                    entrada=json.dumps(cuerpo, ensure_ascii=False).encode("utf-8"))
    return None if ok else (salida or "error desconocido")[:300]


def actualiza_movil(config, nube=False):
    """Sube al gist los datos del móvil y el historial; desde el PC, también productos.json."""
    if not config.get("gist"):
        return
    archivos = {"web.json": WEB_JSON, "historial.json": HISTORIAL}
    if not nube:
        archivos["productos.json"] = PRODUCTOS  # GitHub consulta los productos que tengas en el PC
    generado = lee_json(WEB_JSON, {}).get("generado")
    error = sube_gist(config["gist"], archivos)
    if not error:
        guarda_json(ESTADO, {"subido": generado})
    anota("Página del móvil actualizada" if not error else f"ERROR al subir a la página del móvil: {error}")
    print("Página del móvil actualizada." if not error else f"No se pudo actualizar la página del móvil: {error}")


def combina(local, remoto):
    """Une dos historiales sin repetir registros.

    Devuelve (historial, registros que solo tenía el remoto, cuántos solo tenía el local).
    """
    def clave(r):
        return r["fecha"], r.get("tipo", "consulta"), r.get("fuente", "")
    de_local = {clave(r) for r in local["registros"]}
    de_remoto = {clave(r) for r in remoto["registros"]}
    solo_remoto = [r for r in remoto["registros"] if clave(r) not in de_local]
    solo_local = sum(1 for r in local["registros"] if clave(r) not in de_remoto)
    return ({"registros": sorted(local["registros"] + solo_remoto, key=lambda r: r["fecha"])},
            len(solo_remoto), solo_local)


def sincroniza(config, historial):
    """Trae al PC las consultas que haya hecho GitHub.

    Devuelve (historial, consultas traídas, registros del PC que aún no tiene el gist).
    """
    if not config.get("gist"):
        return historial, 0, 0
    try:
        remoto = json.loads(lee_gist(config["gist"]).get("historial.json") or '{"registros": []}')
    except (OSError, RuntimeError, ValueError) as e:  # sin internet se sigue con lo del PC
        anota(f"AVISO: no se pudo leer el historial de GitHub ({e})")
        return historial, 0, 0
    historial, traidas, faltan = combina(historial, remoto)
    if traidas:
        guarda_json(HISTORIAL, historial)
    return historial, traidas, faltan


def subida_pendiente(config):
    """La página del móvil aún no tiene los últimos datos (por ejemplo, porque no había internet)."""
    return (bool(config.get("gist")) and WEB_JSON.exists()
            and lee_json(ESTADO, {}).get("subido") != lee_json(WEB_JSON, {}).get("generado"))


def publica_pagina(config):
    """Sube al repo de GitHub la página del móvil (plantilla.html sin datos, como index.html) y este script,
    que es el que ejecuta la consulta diaria de GitHub. Devuelve None o el error."""
    repo = config.get("repo_pagina")
    if not repo:
        return 'falta "repo_pagina" en productos.json'
    for destino, origen in (("index.html", PLANTILLA), ("hsn_precios.py", Path(__file__).resolve())):
        cuerpo = {"message": f"Actualiza {destino}", "content": base64.b64encode(origen.read_bytes()).decode()}
        ok, sha = gh("api", f"repos/{repo}/contents/{destino}", "--jq", ".sha")
        if ok and sha:
            cuerpo["sha"] = sha  # ya existía: se reemplaza
        ok, salida = gh("api", "--method", "PUT", f"repos/{repo}/contents/{destino}", "--input", "-", "--silent",
                        entrada=json.dumps(cuerpo).encode("utf-8"))
        if not ok:
            return f"{destino}: {(salida or 'error desconocido')[:300]}"
    return None


def toca_consulta(historial, ahora):
    """La consulta del día: a partir de las 10, si hoy todavía no se ha hecho ninguna."""
    hoy = ahora.date().isoformat()
    hecha = any(r["fecha"].startswith(hoy) for r in historial["registros"]
                if r.get("tipo", "consulta") == "consulta")
    return ahora.hour >= HORA_DIARIA and not hecha


def resumen(analisis):
    print(f"Precios HSN · {datetime.now():%d/%m/%Y %H:%M}")
    for p in analisis["productos"]:
        a, b = p["actual"], p["anterior"]
        linea = f"- {p['nombre']} {p['variante']}: "
        if not a:
            print(linea + "sin datos")
            continue
        linea += euros(a["precio"])
        if b:
            dif = round(a["precio"] - b["precio"], 2)
            linea += f" ({'igual' if dif == 0 else ('sube ' if dif > 0 else 'baja ') + euros(abs(dif))})"
        if p["error"]:
            linea += f" [error: {p['error']}]"
        print(f"{linea} -> {p['recomendacion']['titulo']}")
    c = analisis["cesta"]
    if c["total"] is not None:
        print(f"Cesta HSN: {euros(c['total'])}" + (f" (antes {euros(c['anterior'])})" if c["anterior"] else ""))
    if PAGINA.exists():
        print(f"Página: {PAGINA}")


def main():
    if sys.stdout:  # con pythonw (tarea programada) no hay consola
        sys.stdout.reconfigure(encoding="utf-8")
    nube = "--nube" in sys.argv  # en GitHub Actions: productos e historial se leen del gist
    if nube:
        config = {"gist": os.environ.get("ID_GIST", "")}
        try:
            archivos = lee_gist(config["gist"])
            config |= json.loads(archivos["productos.json"])
            historial = json.loads(archivos.get("historial.json") or '{"registros": []}')
        except (KeyError, OSError, RuntimeError, ValueError) as e:
            print(f"No se pudieron leer los datos del gist: {e}")
            return 1
        traidas = faltan = 0
    else:
        config = lee_json(PRODUCTOS, None)
        if not config:
            print(f"Falta {PRODUCTOS.name}")
            return 1
        if "--publicar-pagina" in sys.argv:  # tras cambiar plantilla.html o este script
            error = publica_pagina(config)
            print("Página y script publicados en GitHub." if not error else f"No se pudo publicar: {error}")
            return 1 if error else 0
        historial, traidas, faltan = sincroniza(config, lee_json(HISTORIAL, {"registros": []}))

    # Las consultas programadas (el PC a las 10, al iniciar sesión y al desbloquear; GitHub a las 10:05)
    # solo consultan HSN una vez al día entre las dos.
    if "--diaria" in sys.argv and not toca_consulta(historial, datetime.now()):
        if traidas or faltan:  # GitHub ha consultado, o le falta algo del PC
            ahora = datetime.now()
            genera_paginas(analiza(config, historial, ahora), ahora)
            if faltan:
                actualiza_movil(config)
            else:  # el móvil ya tiene esa consulta: la subió GitHub
                guarda_json(ESTADO, {"subido": lee_json(WEB_JSON, {}).get("generado")})
                anota("Página del PC al día con la consulta de GitHub")
        elif subida_pendiente(config):
            actualiza_movil(config)
        return 0

    if "--solo-pagina" in sys.argv:  # rehace las páginas sin consultar HSN
        ahora = datetime.now()
        analisis = analiza(config, historial, ahora)
        genera_paginas(analisis, ahora)
        anota("Página regenerada sin consultar HSN")
        resumen(analisis)
        if "--abrir" in sys.argv:
            os.startfile(PAGINA)
        if "--subir" in sys.argv or faltan:
            actualiza_movil(config)
        return 0

    precios = consulta(config["productos"])
    errores = {k: v["error"] for k, v in precios.items() if "error" in v}
    if len(errores) == len(precios):
        anota(f"ERROR sin precios: {errores}")
        print("No se ha podido leer ningún precio:", *errores.values(), sep="\n  ")
        return 1
    ahora = datetime.now()
    historial["registros"].append({"fecha": ahora.isoformat(timespec="seconds"), "tipo": "consulta",
                                   "fuente": "hsnstore.com", "desde": "GitHub" if nube else "PC",
                                   "precios": precios})
    guarda_json(HISTORIAL, historial)

    analisis = analiza(config, historial, ahora)
    genera_paginas(analisis, ahora)
    anota("OK" + (f" con errores: {errores}" if errores else ""))
    resumen(analisis)
    if "--abrir" in sys.argv:
        os.startfile(PAGINA)
    actualiza_movil(config, nube)
    return 0


if __name__ == "__main__":
    sys.exit(main())
