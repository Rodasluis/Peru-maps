# -*- coding: utf-8 -*-
"""
Validación de las capas simplificadas para web.

La razón de ser de esta suite: la simplificación por geometría (shapely) movía
cada borde por su cuenta y separaba a los vecinos —4 776 pares solapados y
16 470 agujeros espurios en los distritos a 100 m—.  mapshaper simplifica el
arco compartido una sola vez y eso desaparece.

Las cotas se fijan **contra la resolución completa**, no contra números
absolutos: así el test no se puede ablandar sin querer y detecta cualquier
defecto NUEVO que introduzca este paso.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml
from shapely import STRtree
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from construir import NIVELES, reproyectar  # noqa: E402


@pytest.fixture(scope='module')
def cfg():
    return yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def capas(cfg):
    salida = RAIZ / cfg['rutas']['salida']
    origen, metrico = cfg['crs']['salida'], cfg['crs']['area_nacional']
    out = {}
    for nivel in NIVELES:
        par = {}
        for etq, nombre in (('full', f'{nivel}.geojson'),
                            ('simp', f'{nivel}_simplificado.geojson')):
            ruta = salida / nombre
            if not ruta.exists():
                pytest.skip(f'falta {nombre}; corra construir.py y publicar.py')
            par[etq] = {
                f['properties']['ubigeo']:
                    reproyectar(shape(f['geometry']), origen, metrico)
                for f in json.loads(ruta.read_text(encoding='utf-8'))['features']}
        out[nivel] = par
    return out


def _solapes(geoms) -> tuple:
    gs = list(geoms.values())
    arbol = STRtree(gs)
    total, pares = 0.0, 0
    for i, g in enumerate(gs):
        for j in arbol.query(g):
            if j <= i:
                continue
            a = g.intersection(gs[j]).area
            if a > 1.0:
                total += a
                pares += 1
    return pares, total


def _agujeros(geoms) -> tuple:
    union = unary_union(list(geoms.values()))
    partes = list(union.geoms) if union.geom_type == 'MultiPolygon' else [union]
    areas = [Polygon(a).area for p in partes for a in p.interiors]
    return len(areas), sum(areas)


# ---------------------------------------------------------------------------
def test_mismo_conjunto_de_ubigeos(capas):
    for nivel, par in capas.items():
        assert set(par['simp']) == set(par['full']), (
            f'{nivel}: simplificar cambió el conjunto de ubigeos')


def test_geometrias_validas(capas):
    for nivel, par in capas.items():
        malas = [u for u, g in par['simp'].items() if not g.is_valid or g.is_empty]
        assert not malas, f'{nivel}: geometrías inválidas o vacías: {malas[:8]}'


def test_no_aparecen_solapes_nuevos(capas):
    """
    El defecto que motivó cambiar a mapshaper. Se compara contra la resolución
    completa: departamento y provincia ya traen 1 par solapado en la fuente del
    INEI, así que exigir cero sería exigir que arreglemos su cartografía.
    """
    for nivel, par in capas.items():
        pares_full, area_full = _solapes(par['full'])
        pares_simp, area_simp = _solapes(par['simp'])
        assert pares_simp <= pares_full, (
            f'{nivel}: simplificar creó solapes nuevos '
            f'({pares_full} -> {pares_simp} pares)')
        assert area_simp <= max(area_full * 1.05, area_full + 1000.0), (
            f'{nivel}: el área solapada creció de {area_full:,.1f} a '
            f'{area_simp:,.1f} m²')


def test_no_aparecen_huecos_nuevos(capas):
    """
    Los únicos agujeros legítimos son los cuerpos de agua que el INEI deja
    fuera de la cobertura (~615 km²). La versión por geometría metía 16 470.
    """
    for nivel, par in capas.items():
        n_full, area_full = _agujeros(par['full'])
        n_simp, area_simp = _agujeros(par['simp'])
        assert n_simp <= n_full, (
            f'{nivel}: simplificar abrió agujeros nuevos '
            f'({n_full} -> {n_simp})')
        # 1 km² sobre los ~615 km² de lagos: el borde de un lago también se
        # simplifica, así que su área se mueve un poco.
        assert abs(area_simp - area_full) <= 1e6, (
            f'{nivel}: el área de los agujeros pasó de {area_full / 1e6:,.3f} '
            f'a {area_simp / 1e6:,.3f} km²')


def test_la_desviacion_es_razonable(capas):
    """
    Cota de cordura sobre cuánto deforma: detecta un intervalo mal puesto
    (p. ej. interpretar 100 como grados en vez de metros).
    Observado: 0.030 % departamento, 0.067 % provincia, 0.155 % distrito.
    """
    for nivel, par in capas.items():
        dif = sum(par['simp'][u].symmetric_difference(par['full'][u]).area
                  for u in par['full'])
        total = sum(g.area for g in par['full'].values())
        assert dif / total < 0.005, (
            f'{nivel}: la simplificación mueve el {100 * dif / total:.3f} % '
            f'del área; revise simplificacion.intervalo_m')


def test_la_simplificada_pesa_menos(cfg):
    salida = RAIZ / cfg['rutas']['salida']
    for nivel in NIVELES:
        full = (salida / f'{nivel}.geojson').stat().st_size
        simp = (salida / f'{nivel}_simplificado.geojson').stat().st_size
        assert simp < full * 0.6, (
            f'{nivel}: la simplificada pesa {simp / full:.0%} del original; '
            f'no compensa publicarla')
