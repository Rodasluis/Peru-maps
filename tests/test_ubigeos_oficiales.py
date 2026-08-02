# -*- coding: utf-8 -*-
"""
Validación del listado oficial de ubigeos (SISCONCODE) y de su cruce con la
cartografía publicada y con los tabulados del Censo.

El registro oficial y la cartografía NO coinciden: el INEI reconoce 1892
distritos y su WFS publica polígono para 1890.  Eso no es un error de este
repo — es el hecho que lo justifica — pero tiene que estar medido y visible.
"""

import csv
import json
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from descargar_ubigeos import normalizar  # noqa: E402

NIVELES = ('departamento', 'provincia', 'distrito')
LARGO = {'departamento': 2, 'provincia': 4, 'distrito': 6}
PREFIJO = {'departamento': 'DEPARTAMENTO', 'provincia': 'PROVINCIA',
           'distrito': 'DISTRITO'}


@pytest.fixture(scope='module')
def cfg():
    return yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def oficiales(cfg):
    ruta = (RAIZ / cfg['rutas']['salida'] /
            f"ubigeos_{cfg['sisconcode']['version']}.csv")
    if not ruta.exists():
        pytest.skip(f'falta {ruta.name}; corra descargar_ubigeos.py')
    with open(ruta, encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))


def por_nivel(filas, nivel):
    return [r for r in filas if r['nivel'] == nivel]


# ---------------------------------------------------------------------------
def test_conteos_oficiales(cfg, oficiales):
    for nivel in NIVELES:
        assert len(por_nivel(oficiales, nivel)) == \
            cfg['ubigeos_oficiales'][nivel], nivel


def test_ubigeos_bien_formados_y_unicos(oficiales):
    codigos = [r['ubigeo'] for r in oficiales]
    assert len(codigos) == len(set(codigos)), 'ubigeos repetidos'
    for r in oficiales:
        u, nivel = r['ubigeo'], r['nivel']
        assert len(u) == LARGO[nivel] and u.isdigit(), f'{u} ({nivel})'


def test_la_jerarquia_del_listado_cierra(oficiales):
    dep = {r['ubigeo'] for r in por_nivel(oficiales, 'departamento')}
    prov = {r['ubigeo'] for r in por_nivel(oficiales, 'provincia')}
    dist = {r['ubigeo'] for r in por_nivel(oficiales, 'distrito')}
    assert {u[:2] for u in prov} == dep
    assert {u[:4] for u in dist} == prov
    assert {u[:2] for u in dist} == dep


def test_formato_de_nombre_para_el_censo(oficiales):
    """Los tabulados del Censo nombran "PROVINCIA CHACHAPOYAS", "DISTRITO
    ASUNCIÓN": el prefijo y las tildes tienen que salir igual."""
    for r in oficiales:
        assert r['nombre_censo'] == f'{PREFIJO[r["nivel"]]} {r["nombre"].upper()}'
        assert r['nombre_censo_normalizado'] == normalizar(r['nombre_censo'])
        assert r['nombre_normalizado'] == normalizar(r['nombre'])


def test_clave_censo_es_unica_en_cada_nivel(oficiales):
    """
    Lo que hace utilizable el cruce. Los nombres de distrito se repiten mucho
    (10 "SANTA ROSA"), así que cruzar por nombre suelto duplica filas; la clave
    jerárquica no.
    """
    for nivel in NIVELES:
        filas = por_nivel(oficiales, nivel)
        claves = Counter(r['clave_censo'] for r in filas)
        repetidas = {k: v for k, v in claves.items() if v > 1}
        assert not repetidas, f'{nivel}: claves repetidas {list(repetidas)[:5]}'


def test_los_nombres_sueltos_si_se_repiten(oficiales):
    """
    Se fija el hecho que justifica clave_censo. Si algún día dejaran de
    repetirse, este test avisa de que la advertencia del README sobra.
    """
    d = por_nivel(oficiales, 'distrito')
    c = Counter(r['nombre_censo_normalizado'] for r in d)
    repetidos = {k: v for k, v in c.items() if v > 1}
    assert len(repetidos) >= 50, (
        'se esperaban muchos nombres de distrito repetidos; '
        f'hay {len(repetidos)}')
    assert repetidos.get('DISTRITO SANTA ROSA', 0) >= 5


def test_la_cartografia_no_inventa_ubigeos(cfg, oficiales):
    """Todo ubigeo publicado tiene que existir en el registro oficial."""
    salida = RAIZ / cfg['rutas']['salida'] / 'distrito.geojson'
    if not salida.exists():
        pytest.skip('falta salida/distrito.geojson; corra construir.py')
    publicados = {f['properties']['ubigeo'] for f in
                  json.loads(salida.read_text(encoding='utf-8'))['features']}
    oficial = {r['ubigeo'] for r in por_nivel(oficiales, 'distrito')}
    inventados = publicados - oficial
    assert not inventados, (
        f'se publican ubigeos que el INEI no reconoce: {sorted(inventados)}')


def test_el_ubigeo_derivado_coincide_con_el_oficial(cfg, oficiales):
    """
    La regla max+1 es una predicción; aquí se contrasta contra el registro
    oficial. Para Santa Rosa de Loreto predijo 160405 y el INEI asignó 160405.
    """
    registro = yaml.safe_load(
        (RAIZ / cfg['rutas']['leyes'] / 'registro.yml').read_text(encoding='utf-8'))
    qa = RAIZ / cfg['rutas']['qa'] / 'reporte.json'
    if not qa.exists():
        pytest.skip('falta qa/reporte.json; corra construir.py')
    cortes = {c['ley']: c for c in
              json.loads(qa.read_text(encoding='utf-8'))['cortes']}
    nombres = {r['ubigeo']: r['nombre_normalizado'] for r in oficiales}

    for e in registro['distritos']:
        c = cortes[e['ley']]
        assert c['ubigeo_calculado_por_la_regla'] == c['ubigeo'], e['nombre']
        oficial = e.get('ubigeo_oficial_confirmado')
        if oficial:
            assert c['ubigeo'] == oficial, (
                f'{e["nombre"]}: publicado {c["ubigeo"]}, oficial {oficial}')
            assert oficial in nombres, f'{oficial} no está en el listado oficial'
            assert normalizar(e['nombre']) == nombres[oficial], (
                f'{oficial}: el registro dice "{e["nombre"]}" y el INEI '
                f'"{nombres[oficial]}"')


def test_se_declara_que_distritos_oficiales_no_tienen_poligono(cfg, oficiales):
    """
    El hueco que justifica el repo, medido y fijado. Hoy: Alto Trujillo
    (130112), que el INEI reconoce pero para el que no hay ni polígono del WFS
    ni ley registrada en leyes/registro.yml.
    """
    salida = RAIZ / cfg['rutas']['salida'] / 'distrito.geojson'
    if not salida.exists():
        pytest.skip('falta salida/distrito.geojson; corra construir.py')
    publicados = {f['properties']['ubigeo'] for f in
                  json.loads(salida.read_text(encoding='utf-8'))['features']}
    oficial = {r['ubigeo'] for r in por_nivel(oficiales, 'distrito')}
    sin_poligono = oficial - publicados

    declarados = set(cfg.get('sin_cartografia', {}).get('distritos', {}))
    nuevos = sin_poligono - declarados
    assert not nuevos, (
        f'hay distritos oficiales sin polígono que no están declarados en '
        f'config.yml (sin_cartografia): {sorted(nuevos)}')
    resueltos = declarados - sin_poligono
    assert not resueltos, (
        f'{sorted(resueltos)} ya tiene polígono; quítelo de sin_cartografia '
        f'en config.yml')
