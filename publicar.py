# -*- coding: utf-8 -*-
"""
publicar.py — genera las variantes derivadas de salida/: la simplificada para
web y el GeoPackage.

    py -3.11 publicar.py     (después de construir.py)

La simplificación la hace **mapshaper**, no shapely.  No es un capricho de
herramienta: Douglas-Peucker aplicado geometría por geometría mueve cada borde
por separado, así que dos distritos vecinos dejan de compartir el límite y
aparecen solapes e hilos de hueco.  Medido sobre los 1891 distritos a 100 m:

    shapely (por geometría)   4 776 pares solapados, 344 km² de solape,
                              16 470 agujeros espurios, 968 km² de hueco
    mapshaper (topología)     0 solapes, 0 km², sólo los 8 agujeros reales
                              (los lagos que el INEI deja fuera, 615 km²)

Además mapshaper se desvía menos del original: 1 992 km² de diferencia
simétrica contra los 5 189 km² de la versión por geometría.

Requiere mapshaper en el PATH:  npm install -g mapshaper

Cada nivel se simplifica por separado.  Procesar los tres juntos con
`combine-files` comparte la topología entre niveles, pero como el polígono de
una provincia ocupa el mismo espacio que sus distritos, mapshaper reporta
intersecciones que no puede reparar. Por eso: un nivel por invocación.

Sobre el GeoPackage: SQLite/GPKG embebe metadatos propios, así que NO es
byte-estable entre corridas.  La garantía de estabilidad es sólo para los
GeoJSON.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from shapely.geometry import shape

from construir import NIVELES, a_feature, escribir_geojson

RAIZ = Path(__file__).resolve().parent


def ruta_mapshaper() -> str:
    exe = shutil.which('mapshaper')
    if not exe:
        raise SystemExit(
            'no se encontró mapshaper en el PATH.\n'
            '  Instálelo con:  npm install -g mapshaper\n'
            'No se cae a shapely a propósito: simplificar por geometría rompe '
            'los bordes compartidos, que es justo lo que hay que evitar.')
    return exe


def simplificar(entrada: Path, intervalo_m: float, metodo: str) -> list:
    """Devuelve los features simplificados por mapshaper, con su topología."""
    exe = ruta_mapshaper()
    with tempfile.TemporaryDirectory() as tmp:
        destino = Path(tmp) / 'simplificado.geojson'
        cmd = [exe, str(entrada),
               '-simplify', f'interval={intervalo_m}', metodo,
               'keep-shapes',            # que no desaparezca ningún polígono
               '-o', 'format=geojson', str(destino)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(
                f'mapshaper falló ({r.returncode}) en {entrada.name}:\n'
                f'{r.stderr.strip() or r.stdout.strip()}')

        # mapshaper avisa de "1 intersection could not be repaired" en
        # departamento y provincia.  NO se falla por eso: se comprobó que el
        # aviso sale igual con interval=1, es decir sin simplificar nada, así
        # que la intersección viene en la geometría del INEI y no la crea este
        # paso (distrito, que es el nivel que reconstruimos, no la tiene a
        # ningún intervalo).  Lo que sí se exige es lo que importa: geometría
        # válida, ningún feature perdido y cero solapes nuevos; eso lo verifican
        # a_feature() más abajo y tests/test_simplificado.py.
        aviso = ' '.join((r.stderr or '').split())
        return json.loads(destino.read_text(encoding='utf-8'))['features'], aviso


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    salida = RAIZ / cfg['rutas']['salida']
    dec = cfg['salida']['decimales']
    simp = cfg['simplificacion']
    intervalo = float(simp['intervalo_m'])
    metodo = simp['metodo']

    faltan = [n for n in NIVELES if not (salida / f'{n}.geojson').exists()]
    if faltan:
        raise SystemExit(f'faltan salidas: {faltan}. Corra primero construir.py')

    print(f'simplificando con mapshaper ({metodo}, interval={intervalo:.0f} m)')
    for nivel in NIVELES:
        origen = salida / f'{nivel}.geojson'
        originales = json.loads(origen.read_text(encoding='utf-8'))['features']
        props = {f['properties']['ubigeo']: f['properties'] for f in originales}

        feats, aviso = simplificar(origen, intervalo, metodo)

        # Las propiedades se vuelven a poner desde el original: así se conserva
        # el orden de campos y los tipos, y la salida sigue siendo byte-estable.
        vistos = set()
        nuevos = []
        for f in feats:
            u = f['properties'].get('ubigeo')
            if u is None or u not in props:
                raise SystemExit(
                    f'{nivel}: mapshaper devolvió un feature con ubigeo {u!r} '
                    f'que no está en el original')
            vistos.add(u)
            nuevos.append(a_feature(props[u], shape(f['geometry']), dec))

        perdidos = set(props) - vistos
        if perdidos:
            raise SystemExit(
                f'{nivel}: mapshaper perdió {len(perdidos)} features '
                f'({sorted(perdidos)[:5]}); suba keep-shapes o baje el intervalo')

        ruta = salida / f'{nivel}_simplificado.geojson'
        escribir_geojson(ruta, nuevos)
        mb_o = origen.stat().st_size / 1e6
        mb_s = ruta.stat().st_size / 1e6
        print(f'  {nivel:<14} {mb_o:>7.1f} MB -> {mb_s:>6.1f} MB '
              f'({len(nuevos)} features)')
        if 'could not be repaired' in aviso:
            print(f'      nota de mapshaper (viene de la fuente, no de este '
                  f'paso): {aviso}')

    # --- GeoPackage ---------------------------------------------------------
    import geopandas as gpd
    import pandas as pd

    gpkg = salida / 'peru_limites.gpkg'
    if gpkg.exists():
        gpkg.unlink()   # evita arrastrar capas viejas
    for nivel in NIVELES:
        feats = json.loads(
            (salida / f'{nivel}.geojson').read_text(encoding='utf-8'))['features']
        gdf = gpd.GeoDataFrame(
            pd.DataFrame([f['properties'] for f in feats]),
            geometry=[shape(f['geometry']) for f in feats],
            crs=cfg['crs']['salida'])
        gdf.to_file(gpkg, layer=nivel, driver='GPKG')
        print(f'  capa {nivel:<14} -> {gpkg.name}')
    print(f'\nGeoPackage: {gpkg.stat().st_size / 1e6:.1f} MB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
