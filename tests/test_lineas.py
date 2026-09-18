# -*- coding: utf-8 -*-
"""
Validación de la capa de arcos (salida/limites_lineas.geojson).

Lo que tiene que cumplir: cada límite guardado UNA sola vez, sin perder nada
respecto de los polígonos, y con la procedencia puesta sobre el arco y no
sobre el distrito.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from shapely import STRtree
from shapely.geometry import shape
from shapely.ops import polygonize, unary_union

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / 'src'))

from construir import reproyectar  # noqa: E402


@pytest.fixture(scope='module')
def cfg():
    return yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def arcos(cfg):
    ruta = RAIZ / cfg['rutas']['salida'] / 'limites_lineas.geojson'
    if not ruta.exists():
        pytest.skip('falta salida/limites_lineas.geojson; corra lineas.py')
    return json.loads(ruta.read_text(encoding='utf-8'))['features']


@pytest.fixture(scope='module')
def distritos(cfg):
    ruta = RAIZ / cfg['rutas']['salida'] / 'distrito.geojson'
    if not ruta.exists():
        pytest.skip('falta salida/distrito.geojson; corra construir.py')
    return {f['properties']['ubigeo']: f
            for f in json.loads(ruta.read_text(encoding='utf-8'))['features']}


# ---------------------------------------------------------------------------
def test_ningun_arco_se_solapa_con_otro(arcos):
    """
    El punto de toda la capa. Si algún tramo estuviera guardado dos veces, la
    unión sería más corta que la suma de longitudes.
    """
    gs = [shape(f['geometry']) for f in arcos]
    suma = sum(g.length for g in gs)
    union = unary_union(gs).length
    # en grados; 1e-9 grados ≈ 0.1 mm
    assert abs(suma - union) < 1e-6, (
        f'hay {(suma - union) * 111_000:,.1f} m de geometría duplicada entre '
        f'arcos')


def test_los_arcos_reconstruyen_cada_distrito(arcos, distritos):
    """No se pierde ni un metro de límite al pasar de polígonos a arcos."""
    de = defaultdict(list)
    for f in arcos:
        g = shape(f['geometry'])
        for u in (f['properties']['ubigeo_a'], f['properties']['ubigeo_b']):
            if u:
                de[u].append(g)

    sin_arcos = [u for u in distritos if not de[u]]
    assert not sin_arcos, f'distritos sin ningún arco: {sin_arcos[:10]}'

    malos = []
    for u, f in distritos.items():
        borde = shape(f['geometry']).boundary.length
        cubierto = unary_union(de[u]).length
        if abs(borde - cubierto) * 111_000 > 0.5:      # medio metro
            malos.append((u, round(abs(borde - cubierto) * 111_000, 3)))
    assert not malos, f'distritos mal cubiertos por sus arcos (m): {malos[:10]}'


def test_la_procedencia_es_del_arco_no_del_distrito(arcos, distritos):
    """
    El borde exterior de un distrito derivado sigue siendo geometría del INEI:
    se hereda intacta del padre. Marcarlo como derivado etiquetaría 25 km de
    frontera internacional del INEI como reconstruidos.
    """
    derivados = [f for f in arcos if f['properties']['fuente'] == 'derivado']
    assert all(f['properties']['tipo'] == 'interdistrital' for f in derivados), (
        'ningún arco exterior puede ser derivado: la frontera se hereda')

    ubigeos_derivados = {u for u, f in distritos.items()
                         if f['properties']['fuente'] == 'derivado'}
    for f in arcos:
        p = f['properties']
        if p['tipo'] == 'exterior' and p['ubigeo_a'] in ubigeos_derivados:
            assert p['fuente'] == 'INEI', (
                f'el arco exterior de {p["ubigeo_a"]} es frontera del INEI')
            assert p['ley_creacion'] is None


def test_un_arco_derivado_por_ley(cfg, arcos):
    """Cada ley dibuja exactamente una línea nueva: la que cierra el distrito."""
    registro = yaml.safe_load(
        (RAIZ / cfg['rutas']['leyes'] / 'registro.yml').read_text(encoding='utf-8'))
    for e in registro['distritos']:
        de_esta_ley = [f for f in arcos
                       if f['properties']['ley_creacion'] == e['ley']]
        assert len(de_esta_ley) == 1, (
            f'la ley {e["ley"]} debería aportar 1 arco, no {len(de_esta_ley)}')
        p = de_esta_ley[0]['properties']
        assert e['padre'] in (p['ubigeo_a'], p['ubigeo_b'])
        assert p['confianza'] == (e.get('confianza') or 'reconstruido')


def test_lado_izquierdo_y_derecho_resueltos(arcos):
    sin_resolver = [f['properties'] for f in arcos
                    if not f['properties']['lado_resuelto']]
    assert not sin_resolver, (
        f'{len(sin_resolver)} arcos sin lado resuelto: '
        f'{[(p["ubigeo_a"], p["ubigeo_b"]) for p in sin_resolver[:6]]}')


def test_izquierda_y_derecha_son_los_dos_vecinos(arcos):
    for f in arcos:
        p = f['properties']
        if p['tipo'] != 'interdistrital':
            continue
        assert {p['izquierda'], p['derecha']} == {p['ubigeo_a'], p['ubigeo_b']}


def test_los_arcos_cierran_el_pais_sin_huecos_nuevos(cfg, arcos, distritos):
    """
    polygonize sobre los arcos devuelve una cara por parte de distrito, más las
    caras de los cuerpos de agua que el INEI deja fuera de la cobertura (Lago
    Junín, Arapa, Parinacochas, Salinas, Langui-Layo, Umayo: ~615 km²).

    Se comprueba que no aparezca un hueco NUEVO, que sí sería un error de este
    pipeline.
    """
    conocido = cfg['incoherencias_inei']['lagos_sin_distrito']
    tol_sliver = float(cfg['incoherencias_inei']['sliver_degenerado_max_m2'])
    origen, metrico = cfg['crs']['salida'], cfg['crs']['area_nacional']

    caras = list(polygonize([shape(f['geometry']) for f in arcos]))
    geoms = {u: shape(f['geometry']) for u, f in distritos.items()}
    claves = list(geoms)
    arbol = STRtree([geoms[u] for u in claves])

    huerfanas = []
    for c in caras:
        p = c.representative_point()
        if not any(geoms[claves[i]].contains(p) for i in arbol.query(p)):
            huerfanas.append(reproyectar(c, origen, metrico))

    # Se separan las dos poblaciones antes de acotarlas: mezclar lagos de
    # cientos de km² con slivers de centímetros cuadrados en un solo conteo
    # esconde justo lo que hay que vigilar.
    lagos = [c for c in huerfanas if c.area > tol_sliver]
    slivers = [c for c in huerfanas if c.area <= tol_sliver]

    assert len(lagos) <= int(conocido['caras_maximas']), (
        f'{len(lagos)} caras de tamaño real sin distrito, más de las '
        f'{conocido["caras_maximas"]} conocidas')

    total_km2 = sum(c.area for c in lagos) / 1e6
    assert abs(total_km2 - float(conocido['area_total_km2'])) <= \
        float(conocido['tolerancia_km2']), (
        f'el área sin distrito es {total_km2:,.2f} km² y se esperaban '
        f'{conocido["area_total_km2"]} km²; apareció o desapareció un hueco')

    # Los slivers se acotan por área total, no por número: cada corte nuevo
    # deja uno o dos en sus nodos, y un hueco de verdad sería miles de veces
    # mayor que esta cota.
    total_slivers = sum(c.area for c in slivers)
    assert total_slivers <= float(
        cfg['incoherencias_inei']['sliver_area_total_max_m2']), (
        f'los slivers degenerados suman {total_slivers:,.3f} m² '
        f'({len(slivers)} caras); eso ya no es ruido de nodo')
