# -*- coding: utf-8 -*-
"""
src/construir.py — arma los tres niveles de límites del Perú a partir de las capas
del INEI más el registro de leyes de creación.

    py -3.11 src/construir.py

Lee    fuentes/{departamento,provincia,distrito}.geojson   (ver src/descargar.py)
       leyes/registro.yml
       config.yml
Escribe salida/   GeoJSON completo + simplificado + GeoPackage
        qa/       reporte del corte y capas antes/después de la provincia tocada

El principio de todo el archivo: nunca se dibuja un límite desde cero.  Cada
distrito derivado sale de CORTAR el polígono de su padre con la línea que la
ley efectivamente crea; el resto del perímetro se hereda tal cual.  Eso hace
que la conservación de área sea exacta por construcción y no una negociación
de tolerancias.
"""

from __future__ import annotations

import json
import math
import sys
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

import yaml
from pyproj import Transformer
from shapely import set_precision
from shapely.geometry import (LineString, MultiPolygon, Point, Polygon,
                              mapping, shape)
from shapely.ops import linemerge, split, transform, unary_union
from shapely.validation import explain_validity

from ubigeo import (cargar_registro_oficial, cargar_retirados,
                    huecos_de_provincia, normalizar, siguiente_ubigeo)

# La raíz del repositorio: un nivel por encima de src/, donde están config.yml
# y las carpetas de datos (salida/, qa/, leyes/, fuentes/).
RAIZ = Path(__file__).resolve().parents[1]
NIVELES = ('departamento', 'provincia', 'distrito')

RAIZ2 = math.sqrt(2) / 2
RUMBOS = {
    'este': (1.0, 0.0), 'oeste': (-1.0, 0.0),
    'norte': (0.0, 1.0), 'sur': (0.0, -1.0),
    'noreste': (RAIZ2, RAIZ2), 'noroeste': (-RAIZ2, RAIZ2),
    'sureste': (RAIZ2, -RAIZ2), 'suroeste': (-RAIZ2, -RAIZ2),
}


class ErrorDeConstruccion(RuntimeError):
    """Algo no cuadra. Se para en seco en vez de publicar geometría dudosa."""


# ---------------------------------------------------------------------------
# Reproyección
# ---------------------------------------------------------------------------
@lru_cache(maxsize=32)
def _transformador(desde: str, hacia: str) -> Transformer:
    return Transformer.from_crs(desde, hacia, always_xy=True)


def reproyectar(geom, desde: str, hacia: str):
    if desde == hacia:
        return geom
    t = _transformador(desde, hacia)
    return transform(lambda x, y, z=None: t.transform(x, y), geom)


def punto_a(crs_origen: str, crs_destino: str, xy) -> Point:
    t = _transformador(crs_origen, crs_destino)
    return Point(*t.transform(xy[0], xy[1]))


# ---------------------------------------------------------------------------
# Construcción de la línea de corte
# ---------------------------------------------------------------------------
def _vector(rumbo: str, anterior, actual):
    """Vector unitario del rumbo. 'continuar' prolonga el último tramo."""
    if rumbo == 'continuar':
        dx, dy = actual[0] - anterior[0], actual[1] - anterior[1]
        n = math.hypot(dx, dy)
        if n == 0:
            raise ErrorDeConstruccion('no se puede continuar un tramo de largo 0')
        return dx / n, dy / n
    if rumbo not in RUMBOS:
        raise ErrorDeConstruccion(
            f'rumbo desconocido: {rumbo!r}; use uno de '
            f'{sorted(RUMBOS)} o "continuar"')
    return RUMBOS[rumbo]


def _puntos_de(geom) -> list:
    if geom.is_empty:
        return []
    if geom.geom_type == 'Point':
        return [geom]
    if hasattr(geom, 'geoms'):
        out = []
        for g in geom.geoms:
            out.extend(_puntos_de(g))
        return out
    return []


def _extender_hasta_borde(p0, v, max_m: float, objetivo: Polygon,
                          holgura: float = 500.0):
    """
    Lanza un rayo desde p0 en dirección v y devuelve un punto justo más allá
    del borde del polígono.  Falla si el rayo no lo corta: eso significaría que
    la memoria no cierra contra la geometría existente, que es exactamente el
    caso en que hay que parar y mirar.
    """
    destino = (p0[0] + v[0] * max_m, p0[1] + v[1] * max_m)
    cortes = _puntos_de(LineString([p0, destino]).intersection(objetivo.exterior))
    if not cortes:
        raise ErrorDeConstruccion(
            f'el tramo desde {p0} rumbo {v} no corta el borde del padre en '
            f'{max_m:,.0f} m; la memoria no cierra contra la cartografía')
    lejano = max(cortes, key=lambda q: math.hypot(q.x - p0[0], q.y - p0[1]))
    d = math.hypot(lejano.x - p0[0], lejano.y - p0[1]) + holgura
    return (p0[0] + v[0] * d, p0[1] + v[1] * d), lejano


def construir_linea(corte: dict, objetivo: Polygon):
    """Arma la LineString de corte y devuelve (línea, notas)."""
    pts = [tuple(map(float, p)) for p in corte['puntos']]
    if len(pts) < 2:
        raise ErrorDeConstruccion('la línea de corte necesita al menos 2 puntos')
    notas = {}

    ini = corte.get('prolongar_inicio')
    if ini:
        v = _vector(ini['rumbo'], pts[1], pts[0])
        nuevo, tocado = _extender_hasta_borde(
            pts[0], v, float(ini['max_m']), objetivo)
        notas['salida_inicio'] = {
            'x': round(tocado.x, 2), 'y': round(tocado.y, 2),
            'm_desde_el_punto_de_la_memoria': round(
                math.hypot(tocado.x - pts[0][0], tocado.y - pts[0][1]), 1)}
        pts = [nuevo] + pts

    fin = corte.get('prolongar_final')
    if fin:
        v = _vector(fin['rumbo'], pts[-2], pts[-1])
        m = float(fin['max_m'])
        extremo = (pts[-1][0] + v[0] * m, pts[-1][1] + v[1] * m)
        # La prolongación es un recurso numérico para que split() cruce
        # limpiamente, no una decisión cartográfica: el área no debe depender
        # de cuánto se prolongue.  Si el rayo cruza el borde más de una vez,
        # sí dependería (se estaría recortando un meandro), así que se para.
        cruces = len(_puntos_de(
            LineString([pts[-1], extremo]).intersection(objetivo.exterior)))
        if cruces != 1:
            raise ErrorDeConstruccion(
                f'la prolongación final de {m:,.0f} m cruza el borde {cruces} '
                f'veces; con {cruces} cruces el área dependería del largo de '
                f'la prolongación. Acorte max_m en el registro.')
        notas['cruces_prolongacion_final'] = cruces
        pts = pts + [extremo]

    linea = LineString(pts)
    if not linea.is_simple:
        raise ErrorDeConstruccion(
            'la línea de corte se cruza a sí misma; revise el orden de los '
            'puntos en el registro')
    return linea, notas


# ---------------------------------------------------------------------------
# El corte
# ---------------------------------------------------------------------------
def densificar(linea: LineString, paso_m: float) -> LineString:
    """
    Mete vértices intermedios cada `paso_m`.

    La memoria define tramos rectos en UTM.  Una recta en UTM no es una recta
    en coordenadas geográficas, así que si se reproyectara sólo los extremos la
    línea se desviaría del trazo legal.  Densificar antes de reproyectar deja
    esa desviación muy por debajo del centímetro que publicamos.
    """
    coords = list(linea.coords)
    pts = []
    for a, b in zip(coords, coords[1:]):
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(math.ceil(d / paso_m)))
        for k in range(n):
            t = k / n
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    pts.append(coords[-1])
    return LineString(pts)


def cortar(padre_geom, entrada: dict, crs_salida: str, tolerancia_m2: float,
           paso_densificado_m: float):
    """
    Corta el polígono del padre y devuelve (geom_nueva, geom_padre_restante,
    reporte).

    El corte se aplica sobre la geometría ORIGINAL en el CRS de salida, no
    sobre una reproyectada de ida y vuelta.  Eso importa: dar el viaje
    4326 -> UTM -> 4326 mueve los vértices unas milésimas de milímetro, y esa
    deriva basta para despegar el borde que el padre comparte con vecinos que
    no tienen nada que ver con el corte.  Se midió: introducía 5 solapes por
    91 m² con distritos como 160105 y 160511, que la fuente del INEI no tiene.
    Reproyectando sólo la LÍNEA, todo vértice heredado queda intacto y el único
    borde nuevo es el que crea la ley.

    El CRS de la memoria se usa igual, pero sólo para construir la línea y para
    medir áreas; nunca para reescribir la geometría del padre.
    """
    corte = entrada['corte']
    crs_m = corte['crs']

    partes = list(padre_geom.geoms) if isinstance(padre_geom, MultiPolygon) \
        else [padre_geom]
    pi = Point(*corte['punto_interior'])   # el registro lo da ya en crs_salida

    contienen = [i for i, g in enumerate(partes) if g.contains(pi)]
    if len(contienen) != 1:
        raise ErrorDeConstruccion(
            f'el punto interior {corte["punto_interior"]} cae en '
            f'{len(contienen)} partes del padre {entrada["padre"]}; se esperaba '
            f'exactamente 1')
    idx = contienen[0]
    objetivo = partes[idx]
    otras = [g for i, g in enumerate(partes) if i != idx]

    # La línea se construye en el CRS de la memoria (recto = recto en UTM)...
    objetivo_m = reproyectar(objetivo, crs_salida, crs_m)
    linea_m, notas = construir_linea(corte, objetivo_m)
    # ...y se lleva al CRS de salida densificada, para cortar el original.
    linea = reproyectar(densificar(linea_m, paso_densificado_m),
                        crs_m, crs_salida)

    piezas = [g for g in split(objetivo, linea).geoms if not g.is_empty]
    if len(piezas) != 2:
        raise ErrorDeConstruccion(
            f'el corte produjo {len(piezas)} piezas y no 2; la línea no cruza '
            f'limpiamente el polígono de {entrada["padre"]}')

    elegidas = [g for g in piezas if g.contains(pi)]
    if len(elegidas) != 1:
        raise ErrorDeConstruccion(
            f'el punto interior cae en {len(elegidas)} piezas; no se puede '
            f'decidir cuál es {entrada["nombre"]}')
    nueva = elegidas[0]
    resto = [g for g in piezas if g is not nueva]

    padre_restante = resto + otras
    padre_nuevo = padre_restante[0] if len(padre_restante) == 1 \
        else MultiPolygon(padre_restante)

    # Las áreas se miden proyectadas, nunca en grados.
    area_antes = objetivo_m.area + sum(
        reproyectar(g, crs_salida, crs_m).area for g in otras)
    area_nueva = reproyectar(nueva, crs_salida, crs_m).area
    area_despues = reproyectar(padre_nuevo, crs_salida, crs_m).area
    deriva = abs((area_nueva + area_despues) - area_antes)
    if deriva > tolerancia_m2:
        raise ErrorDeConstruccion(
            f'el corte no conserva área: {deriva:,.6f} m² de deriva, '
            f'tolerancia {tolerancia_m2} m²')

    reporte = {
        'area_padre_antes_km2': round(area_antes / 1e6, 4),
        'area_nueva_km2': round(area_nueva / 1e6, 4),
        'area_padre_despues_km2': round(area_despues / 1e6, 4),
        'deriva_area_m2': deriva,
        'tolerancia_area_m2': tolerancia_m2,
        'crs_de_la_memoria': crs_m,
        'crs_del_corte': crs_salida,
        'paso_densificado_m': paso_densificado_m,
        'vertices_de_la_linea': len(linea.coords),
        'piezas': len(piezas),
        **notas,
    }
    return nueva, padre_nuevo, reporte


def medir_tramos_reusados(entrada: dict, distritos: dict, crs_salida: str) -> list:
    """
    Mide la discrepancia entre los puntos de referencia que la memoria da para
    los tramos que NO digitalizamos y el arco que el INEI ya publica.  No
    afecta la geometría: existe para que la discrepancia quede publicada en vez
    de quedar escondida en una decisión de diseño.
    """
    out = []
    for tramo in entrada['corte'].get('verificacion_tramos_reusados', []) or []:
        crs_m = entrada['corte']['crs']
        a = reproyectar(distritos[entrada['padre']]['geom'], crs_salida, crs_m)
        b = reproyectar(distritos[tramo['contra']]['geom'], crs_salida, crs_m)
        arco = a.boundary.intersection(b.boundary)
        if arco.is_empty:
            raise ErrorDeConstruccion(
                f'{entrada["padre"]} y {tramo["contra"]} no comparten arco; '
                f'no se puede verificar el tramo "{tramo["nombre"]}"')
        arco = linemerge(arco) if arco.geom_type != 'LineString' else arco
        ds = [arco.distance(Point(float(x), float(y)))
              for x, y in tramo['puntos']]
        out.append({
            'tramo': tramo['nombre'],
            'contra': tramo['contra'],
            # 'coincidente': el arco del INEI ES el límite de la ley y se
            #   reusa; la discrepancia debe ser ruido de generalización.
            # 'sustituido': el arco NO es el límite de la ley, pero se usa
            #   igual porque no hay con qué reconstruirlo. La distancia mide
            #   cuánto se aparta el resultado de lo que dice la norma.
            'tipo': tramo.get('tipo', 'coincidente'),
            'puntos_de_la_memoria': len(ds),
            'distancia_min_m': round(min(ds), 1),
            'distancia_media_m': round(sum(ds) / len(ds), 1),
            'distancia_max_m': round(max(ds), 1),
            'decision': tramo.get('decision', ''),
        })
    return out


# ---------------------------------------------------------------------------
# Propiedades
# ---------------------------------------------------------------------------
METODO_INEI = 'publicado por el INEI (WFS Interoperabilidad)'


def _inei(props: dict, clave, defecto=None):
    v = props.get(clave, defecto) if props else defecto
    return v if v not in ('', None) else defecto


def props_distrito(u: str, reg: dict, nom_prov: dict, nom_dep: dict) -> OrderedDict:
    p = reg.get('props') or {}
    d = reg.get('derivado')
    return OrderedDict([
        ('ubigeo', u),
        ('nombre', reg['nombre']),
        ('ubigeo_provincia', u[:4]),
        ('nombre_provincia', nom_prov.get(u[:4])),
        ('ubigeo_departamento', u[:2]),
        ('nombre_departamento', nom_dep.get(u[:2])),
        ('fuente', 'derivado' if d else 'INEI'),
        ('ley_creacion', d['ley'] if d else None),
        ('fecha_creacion', d['fecha_creacion'] if d else None),
        ('metodo', ' '.join(d['metodo'].split()) if d else METODO_INEI),
        # Sale del registro: no todas las reconstrucciones valen lo mismo.
        # Santa Rosa reusa arcos que SÍ son el límite de su ley; Alto Trujillo
        # sustituye su límite norte por uno que está 541 m fuera de sitio.
        ('confianza', (d.get('confianza') or 'reconstruido') if d else 'oficial'),
        # El ubigeo deja de ser provisional en cuanto se confirma contra el
        # registro oficial, aunque la geometría siga siendo reconstruida: son
        # dos cosas distintas y conviene no mezclarlas.
        ('ubigeo_provisional',
         bool(d) and not (d.get('ubigeo') or d.get('ubigeo_oficial_confirmado'))),
        ('ubigeo_fuente', d.get('fuente_del_ubigeo', '').strip() or None
         if d and d.get('ubigeo_oficial_confirmado') else None),
        ('advertencia', ' '.join(d['advertencia'].split())
         if d and d.get('advertencia') else None),
        # --- campos originales del INEI, sin renombrar salvo `fuente` ---
        ('ccdd', _inei(p, 'ccdd', u[:2])),
        ('ccpp', _inei(p, 'ccpp', u[2:4])),
        ('ccdi', _inei(p, 'ccdi', u[4:6])),
        ('nombdist', _inei(p, 'nombdist', reg['nombre'])),
        ('fuente_inei', _inei(p, 'fuente')),
        ('periodo', _inei(p, 'periodo')),
        ('ccdd_c', _inei(p, 'ccdd_c')),
        ('ccpp_c', _inei(p, 'ccpp_c')),
        ('ccdi_c', _inei(p, 'ccdi_c')),
        ('fec_reg', _inei(p, 'fec_reg')),
    ])


def props_provincia(u: str, p: dict, nom_dep: dict) -> OrderedDict:
    return OrderedDict([
        ('ubigeo', u),
        ('nombre', p.get('nombprov')),
        ('ubigeo_departamento', u[:2]),
        ('nombre_departamento', nom_dep.get(u[:2])),
        ('fuente', 'INEI'),
        ('confianza', 'oficial'),
        ('metodo', METODO_INEI),
        ('ccdd', p.get('ccdd')),
        ('ccpp', p.get('ccpp')),
        ('nombprov', p.get('nombprov')),
        ('fuente_inei', p.get('fuente')),
        ('periodo', p.get('periodo')),
        ('fec_reg', p.get('fec_reg')),
    ])


def props_departamento(u: str, p: dict) -> OrderedDict:
    return OrderedDict([
        ('ubigeo', u),
        ('nombre', p.get('nombdep')),
        ('fuente', 'INEI'),
        ('confianza', 'oficial'),
        ('metodo', METODO_INEI),
        ('ccdd', p.get('ccdd')),
        ('nombdep', p.get('nombdep')),
        ('fuente_inei', p.get('fuente')),
        ('periodo', p.get('periodo')),
        ('fec_reg', p.get('fec_reg')),
    ])


# ---------------------------------------------------------------------------
# Escritura byte-estable
# ---------------------------------------------------------------------------
def _redondear(c, n: int):
    if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
        return [round(float(c[0]), n), round(float(c[1]), n)]
    return [_redondear(x, n) for x in c]


def a_feature(props, geom, decimales: int) -> dict:
    """
    Fija la precisión y serializa.

    Redondear las coordenadas a pelo NO sirve: colapsa los anillos diminutos
    (se midió que rompía el departamento 21, "Too few points in geometry
    component") y separa los bordes compartidos entre vecinos, que dejan de
    coincidir vértice a vértice.  set_precision ajusta a una grilla global
    fija, así que dos polígonos adyacentes siguen compartiendo exactamente los
    mismos vértices y la salida sale válida.  Al caer los vértices sobre la
    grilla, el redondeo posterior a texto es exacto.
    """
    g = set_precision(geom, 10.0 ** -decimales)
    if g.is_empty:
        raise ErrorDeConstruccion(
            f'{props.get("ubigeo")} quedó vacío al fijar la precisión a '
            f'1e-{decimales} grados')
    if not g.is_valid:
        raise ErrorDeConstruccion(
            f'{props.get("ubigeo")} quedó inválido al fijar la precisión: '
            f'{explain_validity(g)}')
    m = mapping(g)
    return {'type': 'Feature',
            'properties': props,
            'geometry': {'type': m['type'],
                         'coordinates': _redondear(m['coordinates'], decimales)}}


def escribir_geojson(ruta: Path, features: list) -> None:
    """
    RFC 7946: sin miembro `crs`, siempre CRS84 (lon, lat).  Un feature por
    línea para que los diffs de git señalen qué distrito cambió.
    """
    lineas = ['{"type":"FeatureCollection","features":[']
    for i, f in enumerate(features):
        coma = ',' if i < len(features) - 1 else ''
        lineas.append(json.dumps(f, ensure_ascii=False,
                                 separators=(',', ':')) + coma)
    lineas.append(']}')
    ruta.write_text('\n'.join(lineas) + '\n', encoding='utf-8')


# ---------------------------------------------------------------------------
def cargar_features(ruta: Path) -> list:
    return json.loads(ruta.read_text(encoding='utf-8'))['features']


def medir_incoherencias(feats: dict, cfg: dict) -> dict:
    """
    Mide en qué provincias la unión de los distritos no reproduce el polígono
    provincial del propio INEI.

    Estas diferencias YA VIENEN en la fuente: se midieron sobre el crudo
    descargado y salen idénticas.  Se remiden y publican en vez de taparlas
    subiendo la tolerancia, que es lo que las volvería invisibles.
    """
    origen, destino = cfg['crs']['salida'], cfg['crs']['area_nacional']
    tol = float(cfg['tolerancias']['cierre_jerarquico_m2'])

    provincias = {f['properties']['ubigeo']:
                  reproyectar(shape(f['geometry']), origen, destino)
                  for f in feats['provincia']}
    grupos = {}
    for f in feats['distrito']:
        u = f['properties']['ubigeo']
        grupos.setdefault(u[:4], []).append(
            reproyectar(shape(f['geometry']), origen, destino))

    fallos = []
    for p, gs in sorted(grupos.items()):
        dif = unary_union(gs).symmetric_difference(provincias[p]).area
        if dif > tol:
            fallos.append({'ubigeo_provincia': p,
                           'diferencia_simetrica_km2': round(dif / 1e6, 4)})
    fallos.sort(key=lambda c: -c['diferencia_simetrica_km2'])
    return {
        'nota': ('Diferencias heredadas de la cartografía del INEI, medidas '
                 'sobre la salida publicada. No las introduce este pipeline: '
                 'se reproducen igual sobre fuentes/ sin tocar.'),
        'tolerancia_m2': tol,
        'crs_de_medicion': destino,
        'cierre_distrito_provincia': fallos,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    registro = yaml.safe_load(
        (RAIZ / cfg['rutas']['leyes'] / 'registro.yml').read_text(encoding='utf-8'))

    fuentes = RAIZ / cfg['rutas']['fuentes']
    salida = RAIZ / cfg['rutas']['salida']
    qa = RAIZ / cfg['rutas']['qa']
    salida.mkdir(parents=True, exist_ok=True)
    qa.mkdir(parents=True, exist_ok=True)

    faltan = [n for n in NIVELES if not (fuentes / f'{n}.geojson').exists()]
    if faltan:
        raise SystemExit(
            f'faltan capas en {fuentes}: {faltan}. Corra primero src/descargar.py')

    crs_salida = cfg['crs']['salida']
    dec = cfg['salida']['decimales']

    # --- departamento y provincia: geometría del INEI, sin tocar ------------
    deps = {}
    for f in cargar_features(fuentes / 'departamento.geojson'):
        p = f['properties']
        deps[p['ccdd']] = {'props': p, 'geom': shape(f['geometry'])}
    nom_dep = {u: r['props'].get('nombdep') for u, r in deps.items()}

    provs = {}
    for f in cargar_features(fuentes / 'provincia.geojson'):
        p = f['properties']
        # ig_provincia NO publica un ubigeo de 4 dígitos: se concatena aquí.
        provs[f"{p['ccdd']}{p['ccpp']}"] = {'props': p,
                                            'geom': shape(f['geometry'])}
    nom_prov = {u: r['props'].get('nombprov') for u, r in provs.items()}

    # --- distritos ----------------------------------------------------------
    distritos = {}
    for f in cargar_features(fuentes / 'distrito.geojson'):
        p = f['properties']
        distritos[p['ubigeo']] = {'props': p, 'geom': shape(f['geometry']),
                                  'nombre': p['nombdist'], 'derivado': None}
    print(f'INEI: {len(deps)} departamentos, {len(provs)} provincias, '
          f'{len(distritos)} distritos')

    retirados = cargar_retirados(
        RAIZ / cfg['rutas']['leyes'] / 'ubigeos_retirados.csv')
    if retirados:
        print(f'blocklist de códigos retirados: {len(retirados)} entradas')

    # El registro OFICIAL de ubigeos, en su versión ya descargada y versionada
    # en salida/. Es la autoridad: la regla max+1 sólo decide para los distritos
    # que el SISCONCODE todavía no liste. Se lee del CSV y no de la red para que
    # el build siga siendo offline y reproducible; refrescarlo es trabajo de
    # src/descargar_ubigeos.py.
    csv_oficial = (RAIZ / cfg['rutas']['salida'] /
                   f"ubigeos_{cfg['sisconcode']['version']}.csv")
    oficiales = cargar_registro_oficial(csv_oficial)
    cita_oficial = (
        'INEI, Sistema de Consulta de Códigos Estandarizados (SISCONCODE), '
        f"versión de ubigeo {cfg['sisconcode']['version']}. "
        f'Ver {csv_oficial.relative_to(RAIZ).as_posix()}.')
    if oficiales:
        print(f'registro oficial de ubigeos: {len(oficiales)} distritos '
              f'({csv_oficial.name})')
    else:
        print(f'sin registro oficial ({csv_oficial.name} no está): los códigos '
              f'nuevos saldrán de la regla max+1, provisionales')

    # --- incorporar las creaciones por ley ---------------------------------
    reportes = []
    tol = float(cfg['tolerancias']['area_conservada_m2'])
    for entrada in registro.get('distritos') or []:
        padre_u = entrada['padre']
        if padre_u not in distritos:
            raise ErrorDeConstruccion(
                f'el padre {padre_u} de {entrada["nombre"]} no está en la capa '
                f'del INEI')

        forzado = entrada.get('ubigeo')

        # La regla se calcula SIEMPRE, aunque el código ya sea oficial: es lo
        # único que avisa de que el INEI asignó algo que no predecía.
        regla = siguiente_ubigeo(
            entrada['provincia'], distritos.keys(), retirados)

        # Consulta al registro oficial ANTES de estimar: si el SISCONCODE ya
        # lista el distrito, manda su código y deja de ser una predicción. Lo
        # declarado a mano en leyes/registro.yml se conserva como aserción.
        oficial = oficiales.get(
            (entrada['provincia'], normalizar(entrada['nombre'])))
        declarado = entrada.get('ubigeo_oficial_confirmado')
        if oficial and declarado and oficial != declarado:
            raise ErrorDeConstruccion(
                f'{entrada["nombre"]}: leyes/registro.yml declara {declarado} '
                f'y {csv_oficial.name} lista {oficial}.')
        confirmado = oficial or declarado

        nuevo_u = forzado or confirmado or regla
        if nuevo_u in distritos:
            raise ErrorDeConstruccion(f'{nuevo_u} ya está en uso')
        if confirmado and confirmado != regla:
            raise ErrorDeConstruccion(
                f'{entrada["nombre"]}: la regla max+1 da {regla} y el INEI '
                f'asignó {confirmado}. Hay que reasignar todo lo publicado con '
                f'{regla} antes de seguir.')
        if forzado and confirmado and forzado != confirmado:
            raise ErrorDeConstruccion(
                f'{entrada["nombre"]}: leyes/registro.yml fuerza {forzado} '
                f'pero el INEI asignó {confirmado}.')

        # Lo resuelto vuelve al registro en memoria: de ahí lo leen las
        # propiedades del feature (ubigeo_provisional y ubigeo_fuente). La cita
        # se arma con la versión de config.yml en vez de escribirse a mano en
        # cada entrada del registro de leyes.
        if confirmado:
            entrada['ubigeo_oficial_confirmado'] = confirmado
            if not (entrada.get('fuente_del_ubigeo') or '').strip():
                entrada['fuente_del_ubigeo'] = cita_oficial

        antes_prov = unary_union(
            [r['geom'] for u, r in distritos.items()
             if u[:4] == entrada['provincia']])

        verificacion = medir_tramos_reusados(entrada, distritos, crs_salida)
        nueva_geom, padre_geom, rep = cortar(
            distritos[padre_u]['geom'], entrada, crs_salida, tol,
            float(cfg['corte']['paso_densificado_m']))

        distritos[padre_u]['geom'] = padre_geom
        distritos[nuevo_u] = {'props': None, 'geom': nueva_geom,
                              'nombre': entrada['nombre'], 'derivado': entrada}

        despues_prov = unary_union(
            [r['geom'] for u, r in distritos.items()
             if u[:4] == entrada['provincia']])
        deriva_prov = abs(
            reproyectar(antes_prov, crs_salida, cfg['crs']['area_nacional']).area
            - reproyectar(despues_prov, crs_salida, cfg['crs']['area_nacional']).area)

        rep.update({
            'nombre': entrada['nombre'],
            'ley': entrada['ley'],
            'ubigeo': nuevo_u,
            'ubigeo_provisional': not (forzado or confirmado),
            'ubigeo_forzado_en_el_registro': bool(forzado),
            'ubigeo_confirmado_oficialmente': confirmado,
            'ubigeo_calculado_por_la_regla': siguiente_ubigeo(
                entrada['provincia'],
                {u for u in distritos if u != nuevo_u}, retirados),
            'padre': padre_u,
            'huecos_en_la_secuencia_de_la_provincia':
                huecos_de_provincia(entrada['provincia'], distritos.keys()),
            'deriva_area_de_la_provincia_m2': round(deriva_prov, 6),
            'tramos_reusados_verificados': verificacion,
        })
        reportes.append(rep)
        print(f'  + {nuevo_u} {entrada["nombre"]}  '
              f'{rep["area_nueva_km2"]:,.2f} km²  (de {padre_u}, '
              f'deriva {rep["deriva_area_m2"]:.2e} m²)')

        # capas antes/después para el mapa de revisión
        escribir_geojson(qa / f'{entrada["provincia"]}_antes.geojson', [
            a_feature({'ubigeo': u, 'nombre': r['nombre']}, r['geom'], dec)
            for u, r in sorted(distritos.items())
            if u[:4] == entrada['provincia'] and u != nuevo_u
            and not (u == padre_u)] + [
            a_feature({'ubigeo': padre_u, 'nombre': distritos[padre_u]['nombre']},
                      unary_union([distritos[padre_u]['geom'], nueva_geom]), dec)])
        escribir_geojson(qa / f'{entrada["provincia"]}_despues.geojson', [
            a_feature({'ubigeo': u, 'nombre': r['nombre'],
                       'fuente': 'derivado' if r['derivado'] else 'INEI'},
                      r['geom'], dec)
            for u, r in sorted(distritos.items())
            if u[:4] == entrada['provincia']])

    # --- escritura ---------------------------------------------------------
    feats = {
        'departamento': [a_feature(props_departamento(u, r['props']), r['geom'], dec)
                         for u, r in sorted(deps.items())],
        'provincia': [a_feature(props_provincia(u, r['props'], nom_dep), r['geom'], dec)
                      for u, r in sorted(provs.items())],
        'distrito': [a_feature(props_distrito(u, r, nom_prov, nom_dep), r['geom'], dec)
                     for u, r in sorted(distritos.items())],
    }
    for nivel in NIVELES:
        escribir_geojson(salida / f'{nivel}.geojson', feats[nivel])
        print(f'  {nivel:<14} {len(feats[nivel]):>6} -> salida/{nivel}.geojson')

    esperado = cfg['conteos_esperados']
    for nivel in NIVELES:
        if len(feats[nivel]) != esperado[nivel]:
            raise ErrorDeConstruccion(
                f'{nivel}: se armaron {len(feats[nivel])} y config.yml espera '
                f'{esperado[nivel]}')

    (qa / 'reporte.json').write_text(
        json.dumps({'cortes': reportes}, ensure_ascii=False, indent=2,
                   sort_keys=True) + '\n', encoding='utf-8')

    # --- incoherencias que vienen del INEI ----------------------------------
    # Se remiden en cada build, no se dan por sabidas: si el INEI corrige (o
    # empeora) su cartografía, aquí se ve.
    inc = medir_incoherencias(feats, cfg)
    (qa / 'incoherencias_inei.json').write_text(
        json.dumps(inc, ensure_ascii=False, indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    conocidas = set(cfg['incoherencias_inei']['cierre_distrito_provincia'])
    observadas = {c['ubigeo_provincia'] for c in inc['cierre_distrito_provincia']}
    print(f'\nIncoherencias heredadas del INEI: {len(observadas)} provincias '
          f'cuya unión de distritos no reproduce su polígono provincial.')
    if observadas - conocidas:
        print(f'  ¡NUEVAS! {sorted(observadas - conocidas)} — '
              f'revise antes de publicar')
    if conocidas - observadas:
        print(f'  ya no aparecen: {sorted(conocidas - observadas)} — '
              f'actualice config.yml')

    print('\nOK. Ejecute `py -3.11 -m pytest -q` para la suite de validación.')
    print('Variantes simplificada y GeoPackage: `py -3.11 src/publicar.py`')
    return 0


if __name__ == '__main__':
    sys.exit(main())
