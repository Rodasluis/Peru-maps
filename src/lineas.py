# -*- coding: utf-8 -*-
"""
src/lineas.py — publica los límites como ARCOS, cada uno una sola vez.

    py -3.11 src/lineas.py       (después de src/construir.py)

Por qué existe: en una capa de polígonos, todo límite interior está guardado
DOS veces, una en cada distrito que lo toca.  Al dibujarla, cada borde
compartido se traza dos veces.  Se midió en Santa Rosa: de sus 82.25 km de
perímetro, 57.2 km (el 70 %) son geometría que también está en Yavarí o en
Ramón Castilla.

Esta capa guarda cada arco una sola vez, con los dos distritos que separa.  El
distrito nuevo aporta UN arco nuevo —el que crea la ley— y todo lo demás son
los arcos del INEI intactos.

Los GeoJSON de polígonos no se tocan: siguen siendo la representación correcta
para análisis por distrito.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import yaml
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Point, shape
from shapely.ops import linemerge, polygonize, unary_union

from construir import a_feature, escribir_geojson

# La raíz del repositorio: un nivel por encima de src/, donde están config.yml
# y las carpetas de datos (salida/, qa/, leyes/, fuentes/).
RAIZ = Path(__file__).resolve().parents[1]

# Desplazamiento perpendicular para decidir qué distrito queda a cada lado.
# 1e-6 grados ≈ 11 cm: más grande que la celda de precisión (1.1 cm) y mucho
# más chico que cualquier rasgo real.
EPS_LADO = 1e-6


def partes_lineales(geom) -> list:
    """Sólo los trozos con longitud: dos distritos pueden tocarse en un punto."""
    if geom.is_empty:
        return []
    if geom.geom_type == 'LineString':
        return [geom] if geom.length > 0 else []
    if geom.geom_type in ('MultiLineString', 'GeometryCollection'):
        out = []
        for g in geom.geoms:
            out.extend(partes_lineales(g))
        return out
    return []


def explotar(geom) -> list:
    """
    Devuelve los tramos conexos, ya fusionados.

    Hay que filtrar los trozos lineales ANTES de fusionar: dos distritos pueden
    tocarse en un solo punto (pasa, p. ej., cerca de -77.745, -5.945) y
    linemerge revienta si le llega un Point.
    """
    partes = partes_lineales(geom)
    if not partes:
        return []
    if len(partes) == 1:
        return partes
    return partes_lineales(linemerge(MultiLineString(partes)))


def lado(arco: LineString, ga, ua: str, gb, ub: str):
    """
    Devuelve (izquierda, derecha) respecto del sentido del arco.

    Se toma el punto medio, se desplaza perpendicularmente a un lado y al otro,
    y se mira qué polígono contiene cada sonda.  Devuelve (None, None) si la
    prueba es ambigua, para que el llamador lo registre en vez de inventar.
    """
    d = arco.length / 2.0
    paso = min(arco.length / 4.0, EPS_LADO * 10)
    p1 = arco.interpolate(max(0.0, d - paso))
    p2 = arco.interpolate(min(arco.length, d + paso))
    vx, vy = p2.x - p1.x, p2.y - p1.y
    n = math.hypot(vx, vy)
    if n == 0:
        return None, None
    medio = arco.interpolate(d)
    # normal a la izquierda del sentido de avance
    izq = Point(medio.x - vy / n * EPS_LADO, medio.y + vx / n * EPS_LADO)
    der = Point(medio.x + vy / n * EPS_LADO, medio.y - vx / n * EPS_LADO)

    if ga.contains(izq) and gb.contains(der):
        return ua, ub
    if gb.contains(izq) and ga.contains(der):
        return ub, ua
    return None, None


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    registro = yaml.safe_load(
        (RAIZ / cfg['rutas']['leyes'] / 'registro.yml').read_text(encoding='utf-8'))
    salida = RAIZ / cfg['rutas']['salida']
    dec = cfg['salida']['decimales']

    ruta = salida / 'distrito.geojson'
    if not ruta.exists():
        raise SystemExit('falta salida/distrito.geojson; corra src/construir.py')
    feats = json.loads(ruta.read_text(encoding='utf-8'))['features']
    props = {f['properties']['ubigeo']: f['properties'] for f in feats}
    geoms = {f['properties']['ubigeo']: shape(f['geometry']) for f in feats}

    # --- qué arco es el que crea la ley ------------------------------------
    # Es exactamente el que separa el distrito nuevo de su padre. Todo lo demás
    # es geometría del INEI, sólo que repartida de otro modo.
    nuevos = {}
    for e in registro.get('distritos') or []:
        hijo = [u for u, p in props.items() if p['ley_creacion'] == e['ley']]
        if len(hijo) != 1:
            raise SystemExit(f'no se pudo identificar el distrito de la ley '
                             f'{e["ley"]}')
        nuevos[frozenset((e['padre'], hijo[0]))] = e

    claves = list(geoms)
    lista = [geoms[u] for u in claves]
    arbol = STRtree(lista)

    arcos = []
    compartido_por = {u: [] for u in claves}
    ambiguos = 0

    for i, g in enumerate(lista):
        for j in arbol.query(g):
            if j <= i:
                continue
            ua, ub = claves[i], claves[j]
            comp = g.boundary.intersection(lista[j].boundary)
            piezas = explotar(comp)
            if not piezas:
                continue
            compartido_por[ua].extend(piezas)
            compartido_por[ub].extend(piezas)

            par = frozenset((ua, ub))
            entrada = nuevos.get(par)
            for arco in piezas:
                izq, der = lado(arco, g, ua, lista[j], ub)
                if izq is None:
                    ambiguos += 1
                    izq, der = sorted((ua, ub))
                arcos.append((arco, {
                    'tipo': 'interdistrital',
                    'izquierda': izq,
                    'derecha': der,
                    'ubigeo_a': min(ua, ub),
                    'ubigeo_b': max(ua, ub),
                    'lado_resuelto': izq is not None,
                    'fuente': 'derivado' if entrada else 'INEI',
                    'ley_creacion': entrada['ley'] if entrada else None,
                    'confianza': (entrada.get('confianza') or 'reconstruido')
                    if entrada else 'oficial',
                }))

    print(f'arcos interdistritales: {len(arcos)}')
    if ambiguos:
        print(f'  ⚠ {ambiguos} con lado ambiguo; se cayó a orden alfabético '
              f'y se marcó lado_resuelto=false')

    # --- lo que no comparte con nadie: frontera internacional y litoral -----
    # No se distingue una de otro: la capa del INEI no lo codifica y cualquier
    # regla inventada se equivocaría en los departamentos que tocan las dos.
    exteriores = 0
    for u, g in geoms.items():
        compartidos = compartido_por[u]
        resto = g.boundary if not compartidos \
            else g.boundary.difference(unary_union(compartidos))
        for arco in explotar(resto):
            exteriores += 1
            # La procedencia es del ARCO, no del distrito. El borde exterior de
            # un distrito derivado sigue siendo geometría del INEI: se hereda
            # intacta de la frontera del padre; lo único que la ley decidió es
            # dónde empieza y dónde termina. Copiar aquí props[u]['fuente']
            # marcaría 25 km de frontera internacional del INEI como derivados.
            arcos.append((arco, {
                'tipo': 'exterior',
                'izquierda': u,
                'derecha': None,
                'ubigeo_a': u,
                'ubigeo_b': None,
                'lado_resuelto': True,
                'fuente': 'INEI',
                'ley_creacion': None,
                'confianza': 'oficial',
            }))
    print(f'arcos exteriores (frontera + litoral): {exteriores}')

    # --- orden estable y escritura -----------------------------------------
    arcos.sort(key=lambda a: (a[1]['ubigeo_a'], a[1]['ubigeo_b'] or '',
                              round(a[0].bounds[0], 7), round(a[0].bounds[1], 7)))
    salida_feats = [a_feature(p, geom, dec) for geom, p in arcos]
    ruta_out = salida / 'limites_lineas.geojson'
    escribir_geojson(ruta_out, salida_feats)

    largo_total = sum(a[0].length for a in arcos)
    largo_poly = sum(g.boundary.length for g in geoms.values())
    print(f'\n{len(salida_feats)} arcos -> salida/{ruta_out.name} '
          f'({ruta_out.stat().st_size / 1e6:.1f} MB)')
    print(f'  longitud de la capa de arcos : {largo_total * 111:>10,.0f} km aprox')
    print(f'  suma de perímetros de polígonos: {largo_poly * 111:>10,.0f} km aprox')
    print(f'  se evita duplicar {(largo_poly - largo_total) * 111:,.0f} km '
          f'({100 * (1 - largo_total / largo_poly):.1f} %)')

    derivados = [(g, p) for g, p in arcos if p['fuente'] == 'derivado']
    print(f'\narcos derivados (los únicos que dibuja una ley): {len(derivados)}')
    for g, p in derivados:
        print(f"  {p['ubigeo_a']} | {p['ubigeo_b']}  ley {p['ley_creacion']}  "
              f"{g.length * 111:,.2f} km aprox")

    # Fragmentos muy cortos: delatarían restos de la resta boundary - compartido.
    cortos = sorted(g.length * 111_000 for g, _ in arcos)
    diminutos = [m for m in cortos if m < 1.0]
    print(f'\narcos de menos de 1 m: {len(diminutos)}'
          + (f'  (el más corto {cortos[0]:.3f} m)' if cortos else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
