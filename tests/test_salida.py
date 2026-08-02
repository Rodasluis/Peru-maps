# -*- coding: utf-8 -*-
"""
Suite de validación de las capas publicadas.

Corre sobre salida/*.geojson (resolución completa), no sobre las fuentes ni
sobre la variante simplificada.  Todo cálculo de área se hace en un CRS
proyectado equivalente-área; nunca en grados.

Los conteos salen de config.yml, no de literales: crear el próximo distrito no
debería obligar a tocar este archivo.
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest
import yaml
from shapely.geometry import shape
from shapely.ops import unary_union

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from construir import NIVELES, reproyectar  # noqa: E402

LONGITUDES = {'departamento': 2, 'provincia': 4, 'distrito': 6}


# ---------------------------------------------------------------------------
# Fixtures (module scope: cargar y reproyectar 1891 polígonos no es gratis)
# ---------------------------------------------------------------------------
@pytest.fixture(scope='module')
def cfg():
    return yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def capas(cfg):
    salida = RAIZ / cfg['rutas']['salida']
    faltan = [n for n in NIVELES if not (salida / f'{n}.geojson').exists()]
    if faltan:
        pytest.skip(f'faltan salidas {faltan}; corra construir.py')
    return {n: json.loads((salida / f'{n}.geojson').read_text(encoding='utf-8'))
            ['features'] for n in NIVELES}


@pytest.fixture(scope='module')
def geom_m(cfg, capas):
    """Geometrías por nivel y ubigeo, en el CRS equivalente-área."""
    origen, destino = cfg['crs']['salida'], cfg['crs']['area_nacional']
    return {n: {f['properties']['ubigeo']:
                reproyectar(shape(f['geometry']), origen, destino)
                for f in capas[n]} for n in NIVELES}


@pytest.fixture(scope='module')
def union_provincias(geom_m):
    """Unión de los distritos de cada provincia. Unir por grupos es mucho más
    barato que una sola unión de 1891 polígonos."""
    grupos = defaultdict(list)
    for u, g in geom_m['distrito'].items():
        grupos[u[:4]].append(g)
    return {p: unary_union(gs) for p, gs in grupos.items()}


# ---------------------------------------------------------------------------
# Conteos e identificadores
# ---------------------------------------------------------------------------
def test_conteos(cfg, capas):
    for nivel in NIVELES:
        assert len(capas[nivel]) == cfg['conteos_esperados'][nivel], nivel


def test_ubigeos_unicos_y_bien_formados(capas):
    for nivel, largo in LONGITUDES.items():
        codigos = [f['properties']['ubigeo'] for f in capas[nivel]]
        assert len(codigos) == len(set(codigos)), f'{nivel}: ubigeos repetidos'
        for u in codigos:
            assert isinstance(u, str), f'{nivel}: {u!r} no es texto'
            assert len(u) == largo, f'{nivel}: {u!r} no tiene {largo} dígitos'
            assert u.isdigit(), f'{nivel}: {u!r} no es numérico'
            assert u == u.zfill(largo), f'{nivel}: {u!r} sin cero a la izquierda'


def test_jerarquia_de_codigos_cierra_en_ambos_sentidos(capas):
    dist = {f['properties']['ubigeo'] for f in capas['distrito']}
    prov = {f['properties']['ubigeo'] for f in capas['provincia']}
    dep = {f['properties']['ubigeo'] for f in capas['departamento']}

    assert {u[:4] for u in dist} == prov
    assert {u[:2] for u in dist} == dep
    assert {u[:2] for u in prov} == dep


def test_referencias_de_nombres_resueltas(capas):
    prov = {f['properties']['ubigeo']: f['properties']['nombre']
            for f in capas['provincia']}
    dep = {f['properties']['ubigeo']: f['properties']['nombre']
           for f in capas['departamento']}
    for f in capas['distrito']:
        p = f['properties']
        assert p['nombre'], f'{p["ubigeo"]} sin nombre'
        assert p['nombre_provincia'] == prov[p['ubigeo_provincia']]
        assert p['nombre_departamento'] == dep[p['ubigeo_departamento']]


# ---------------------------------------------------------------------------
# Procedencia
# ---------------------------------------------------------------------------
# 'reconstruido': se reusaron arcos que SÍ son el límite de la ley y todo lo
#   nuevo salió de sus coordenadas (Santa Rosa de Loreto).
# 'aproximado':   parte del perímetro no procede de la ley (Alto Trujillo, cuyo
#   límite norte se sustituyó por el arco del INEI, a 541 m del de la norma).
CONFIANZA_DERIVADA = ('reconstruido', 'aproximado')


def test_todo_feature_declara_su_procedencia(capas):
    for nivel in NIVELES:
        for f in capas[nivel]:
            p = f['properties']
            assert p['fuente'] in ('INEI', 'derivado'), p['ubigeo']
            assert p['confianza'] in ('oficial',) + CONFIANZA_DERIVADA, p['ubigeo']
            assert (p['fuente'] == 'derivado') == \
                (p['confianza'] in CONFIANZA_DERIVADA), p['ubigeo']


def test_un_solo_predicado_filtra_lo_no_oficial(cfg, capas):
    """El requisito de consumo: quedarse sólo con lo que publica el INEI."""
    oficiales = [f for f in capas['distrito']
                 if f['properties']['fuente'] == 'INEI']
    derivados = [f for f in capas['distrito']
                 if f['properties']['fuente'] != 'INEI']
    assert len(oficiales) + len(derivados) == cfg['conteos_esperados']['distrito']
    assert len(derivados) >= 1
    for f in derivados:
        p = f['properties']
        assert p['ley_creacion'], p['ubigeo']
        assert p['fecha_creacion'], p['ubigeo']
        assert p['metodo'], p['ubigeo']
        assert p['advertencia'], f'{p["ubigeo"]} sin advertencia de uso'
    for f in oficiales:
        assert f['properties']['ley_creacion'] is None


# ---------------------------------------------------------------------------
# Geometría
# ---------------------------------------------------------------------------
def test_geometrias_validas_y_no_vacias(capas):
    for nivel in NIVELES:
        for f in capas[nivel]:
            g = shape(f['geometry'])
            u = f['properties']['ubigeo']
            assert not g.is_empty, f'{nivel} {u}: geometría vacía'
            assert g.is_valid, f'{nivel} {u}: geometría inválida ({g.geom_type})'
            assert g.geom_type in ('Polygon', 'MultiPolygon'), f'{nivel} {u}'
            assert g.area > 0, f'{nivel} {u}: área nula'


def test_salida_en_crs84(capas):
    """RFC 7946: sin miembro `crs`, coordenadas lon/lat dentro del Perú."""
    for nivel in NIVELES:
        ruta_json = json.loads(
            (RAIZ / 'salida' / f'{nivel}.geojson').read_text(encoding='utf-8'))
        assert 'crs' not in ruta_json
        for f in capas[nivel]:
            minx, miny, maxx, maxy = shape(f['geometry']).bounds
            assert -82 <= minx <= maxx <= -68, f['properties']['ubigeo']
            assert -19 <= miny <= maxy <= 0.5, f['properties']['ubigeo']


def test_coordenadas_redondeadas(cfg, capas):
    """Lo que hace byte-estable la salida."""
    dec = cfg['salida']['decimales']

    def revisar(c):
        if isinstance(c, (int, float)):
            assert round(float(c), dec) == float(c)
            return
        for x in c:
            revisar(x)

    for f in capas['distrito'][:200]:
        revisar(f['geometry']['coordinates'])


def test_sin_solapes_entre_distritos(cfg, geom_m):
    from shapely import STRtree

    tol = float(cfg['tolerancias']['solape_max_m2'])
    claves = list(geom_m['distrito'])
    gs = [geom_m['distrito'][u] for u in claves]
    arbol = STRtree(gs)
    culpables = []
    for i, g in enumerate(gs):
        for j in arbol.query(g):
            if j <= i:
                continue
            a = g.intersection(gs[j]).area
            if a > tol:
                culpables.append((claves[i], claves[j], round(a, 2)))
    assert not culpables, f'solapes por encima de {tol} m²: {culpables[:10]}'


def test_cierre_geometrico_distrito_a_provincia(cfg, geom_m, union_provincias):
    """
    En 7 provincias la unión de los distritos del INEI no reproduce el polígono
    provincial del INEI, con diferencias de hasta 279 km².  Eso viene en la
    fuente: se midió sobre fuentes/ sin pasar por el pipeline y sale idéntico.

    Se comprueba que NO aparezca ninguna nueva, en vez de subir la tolerancia
    hasta que quepan las conocidas — que las haría invisibles justo cuando
    alguna empeore.
    """
    tol = float(cfg['tolerancias']['cierre_jerarquico_m2'])
    conocidas = set(cfg['incoherencias_inei']['cierre_distrito_provincia'])

    malas = {}
    for p, union in union_provincias.items():
        dif = union.symmetric_difference(geom_m['provincia'][p]).area
        if dif > tol:
            malas[p] = round(dif / 1e6, 2)

    nuevas = {p: v for p, v in malas.items() if p not in conocidas}
    assert not nuevas, (
        f'provincias que no cerraban antes y ahora sí fallan (> {tol} m²): '
        f'{nuevas}')

    desaparecidas = conocidas - set(malas)
    assert not desaparecidas, (
        f'{sorted(desaparecidas)} ya no falla; el INEI corrigió su cartografía. '
        f'Quítelas de incoherencias_inei en config.yml')


def test_cierre_geometrico_provincia_a_departamento(cfg, geom_m):
    tol = float(cfg['tolerancias']['cierre_jerarquico_m2'])
    grupos = defaultdict(list)
    for u, g in geom_m['provincia'].items():
        grupos[u[:2]].append(g)
    malas = []
    for d, gs in grupos.items():
        dif = unary_union(gs).symmetric_difference(geom_m['departamento'][d]).area
        if dif > tol:
            malas.append((d, round(dif, 1)))
    assert not malas, f'departamentos que no cierran (> {tol} m²): {malas[:10]}'


def test_sin_huecos_contra_el_limite_nacional(cfg, geom_m, union_provincias):
    """La unión de los distritos debe reproducir el límite nacional."""
    tol = float(cfg['tolerancias']['cierre_jerarquico_m2'])
    pais_distritos = unary_union(list(union_provincias.values()))
    pais_departamentos = unary_union(list(geom_m['departamento'].values()))
    dif = pais_distritos.symmetric_difference(pais_departamentos).area
    assert dif <= tol * len(geom_m['departamento']), (
        f'la unión de distritos difiere del límite nacional en {dif:,.1f} m²')


# ---------------------------------------------------------------------------
# El corte
# ---------------------------------------------------------------------------
@pytest.fixture(scope='module')
def reporte(cfg):
    ruta = RAIZ / cfg['rutas']['qa'] / 'reporte.json'
    if not ruta.exists():
        pytest.skip('qa/reporte.json no está; corra construir.py')
    return json.loads(ruta.read_text(encoding='utf-8'))


def test_conservacion_de_area_en_el_corte(cfg, reporte):
    """
    Constraint del diseño: la unión del padre restante más el distrito nuevo
    debe igualar el polígono original del padre.

    Cortar un polígono por una línea conserva área a precisión de coma
    flotante, así que la tolerancia (1 m², en config.yml) está unos cuatro
    órdenes de magnitud por encima de la deriva observada. Se mantiene alta
    respecto del error real y baja respecto de cualquier error de geometría
    que importe: un vértice mal puesto mueve miles de m².
    """
    tol = float(cfg['tolerancias']['area_conservada_m2'])
    assert reporte['cortes'], 'no hay cortes registrados'
    for c in reporte['cortes']:
        assert c['deriva_area_m2'] <= tol, c['ubigeo']
        recompuesta = c['area_nueva_km2'] + c['area_padre_despues_km2']
        assert abs(recompuesta - c['area_padre_antes_km2']) < 1e-3, c['ubigeo']


def test_area_conservada_contra_la_fuente(cfg, capas, geom_m):
    """
    Constraint 2, de punta a punta: la unión del padre publicado más el
    distrito derivado debe reproducir el polígono ORIGINAL del padre tal como
    lo publica el INEI.

    Se comprueba contra fuentes/, no contra el reporte que emite el propio
    build: si el pipeline se equivoca, su reporte se equivoca igual.
    """
    fuente = RAIZ / cfg['rutas']['fuentes'] / 'distrito.geojson'
    if not fuente.exists():
        pytest.skip('fuentes/distrito.geojson no está; corra descargar.py')
    registro = yaml.safe_load(
        (RAIZ / cfg['rutas']['leyes'] / 'registro.yml').read_text(encoding='utf-8'))

    origen, destino = cfg['crs']['salida'], cfg['crs']['area_nacional']
    crudos = {f['properties']['ubigeo']: f['geometry']
              for f in json.loads(fuente.read_text(encoding='utf-8'))['features']}

    # La tolerancia NO es una constante: es el suelo de cuantización de la
    # precisión a la que se publica. Al fijar la precisión a 1e-7 grados
    # (~1.1 cm) cada vértice se mueve hasta media celda, así que el área de un
    # polígono puede cambiar del orden de perímetro × tamaño_de_celda. Yavarí
    # es un distrito amazónico con un borde fluvial larguísimo, así que ese
    # producto domina cualquier error real. Derivarla del perímetro hace que
    # escale sola para el próximo distrito en vez de quedar clavada.
    grado_en_m = 111_320.0
    celda_m = (10.0 ** -cfg['salida']['decimales']) * grado_en_m

    for entrada in registro['distritos']:
        padre = entrada['padre']
        derivados = [f['properties']['ubigeo'] for f in capas['distrito']
                     if f['properties']['ley_creacion'] == entrada['ley']]
        assert len(derivados) == 1, entrada['nombre']

        antes = reproyectar(shape(crudos[padre]), origen, destino)
        despues = unary_union([geom_m['distrito'][padre],
                               geom_m['distrito'][derivados[0]]])
        dif = antes.symmetric_difference(despues).area
        tol = antes.length * celda_m

        assert dif <= tol, (
            f'{entrada["nombre"]}: el padre {padre} recompuesto difiere del '
            f'original del INEI en {dif:,.2f} m², por encima del suelo de '
            f'cuantización ({tol:,.2f} m² = {antes.length / 1000:,.0f} km de '
            f'perímetro × {celda_m * 100:.2f} cm)')
        # Techo de cordura, deliberadamente holgado. En relativo el suelo de
        # cuantización NO es constante: depende de perímetro/área, así que un
        # distrito chico sale peor que uno grande aunque ambos estén bien.
        # Medido: Yavarí (14 371 km²) 2.1e-07; El Porvenir (38 km²) 2.0e-06,
        # diez veces más por ser diez veces más "perimetral". La cota buena es
        # la del perímetro, de arriba; ésta sólo atrapa disparates.
        assert dif / antes.area < 1e-4, (
            f'{entrada["nombre"]}: {dif / antes.area:.2e} de diferencia '
            f'relativa es demasiado')


def test_el_corte_no_movio_la_provincia(cfg, reporte):
    """Un corte interno no puede cambiar el polígono de la provincia."""
    tol = float(cfg['tolerancias']['area_conservada_m2'])
    for c in reporte['cortes']:
        assert c['deriva_area_de_la_provincia_m2'] <= tol, c['ubigeo']


def test_el_corte_produjo_exactamente_dos_piezas(reporte):
    for c in reporte['cortes']:
        assert c['piezas'] == 2, c['ubigeo']
        assert c.get('cruces_prolongacion_final') == 1, c['ubigeo']


def test_discrepancia_de_tramos_reusados_documentada(cfg, reporte, capas):
    """
    Todo tramo que se reusa del INEI en vez de digitalizarlo trae su
    discrepancia medida, para que quede publicada y no escondida.

    Y se distingue el caso honesto del caso incómodo:
      - 'coincidente': el arco ES el límite de la ley (Santa Rosa, 83 m máx).
        Si se aleja demasiado, deja de ser cierto que sea el mismo arco.
      - 'sustituido': el arco NO es el límite de la ley y se usa igual (Alto
        Trujillo, 541 m). Se exige que el distrito quede marcado 'aproximado'
        y con advertencia, para que nadie lo confunda con el otro caso.
    """
    props = {f['properties']['ubigeo']: f['properties']
             for f in capas['distrito']}
    for c in reporte['cortes']:
        for t in c.get('tramos_reusados_verificados', []):
            assert t['decision'], t['tramo']
            if t.get('tipo') == 'sustituido':
                p = props[c['ubigeo']]
                assert p['confianza'] == 'aproximado', (
                    f'{c["ubigeo"]} sustituye un tramo pero se publica como '
                    f'{p["confianza"]}')
                assert p['advertencia'], c['ubigeo']
            else:
                assert t['distancia_max_m'] < 200, (
                    f'{t["tramo"]}: {t["distancia_max_m"]} m es demasiado para '
                    f'tratarlo como el mismo arco')
