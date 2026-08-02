# -*- coding: utf-8 -*-
"""
Tests de la regla de asignación de ubigeo.

El caso que importa no es el contiguo: es el de una provincia con huecos.  Ahí
`count + 1` emite un código retirado y `max + 1` no.
"""

import json
import sys
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from ubigeo import (SECUENCIA_MAXIMA, UbigeoError,  # noqa: E402
                    huecos_de_provincia, siguiente_ubigeo)

# Alto Amazonas, Loreto. Los códigos 03, 04, 07, 08 y 09 se retiraron cuando
# esos distritos pasaron a Datem del Marañón (2005) y el INEI dejó los huecos
# en vez de renumerar, que es lo que protege las series históricas.
ALTO_AMAZONAS = {'160201', '160202', '160205', '160206', '160210', '160211'}
MARISCAL_RAMON_CASTILLA = {'160401', '160402', '160403', '160404'}


def test_caso_contiguo():
    assert siguiente_ubigeo('0101', {'010101', '010102', '010103',
                                     '010104'}) == '010105'


def test_provincia_con_huecos_usa_max_mas_uno():
    assert siguiente_ubigeo('1602', ALTO_AMAZONAS) == '160212'


def test_nunca_reutiliza_un_hueco():
    """El fallo concreto que esta regla existe para evitar."""
    huecos = huecos_de_provincia('1602', ALTO_AMAZONAS)
    assert huecos == [3, 4, 7, 8, 9]

    nuevo = siguiente_ubigeo('1602', ALTO_AMAZONAS)
    assert int(nuevo[4:]) not in huecos

    # count + 1 daría 160207: un código retirado que pertenece a un distrito
    # que pasó a Datem del Marañón. Ese es exactamente el bug.
    con_count = f'1602{len(ALTO_AMAZONAS) + 1:02d}'
    assert con_count == '160207'
    assert nuevo != con_count


def test_max_mas_uno_siempre_supera_todo_hueco():
    """Un hueco está por debajo del máximo por definición, así que max+1 no
    puede caer en uno. Se comprueba como propiedad, no como caso suelto."""
    casos = [('1601', {'160101', '160110', '160112', '160113'}),
             ('1602', ALTO_AMAZONAS),
             ('1604', MARISCAL_RAMON_CASTILLA),
             ('0501', {'050101', '050107', '050199'.replace('99', '08')})]
    for prov, codigos in casos:
        nuevo = int(siguiente_ubigeo(prov, codigos)[4:])
        assert all(nuevo > h for h in huecos_de_provincia(prov, codigos))


def test_max_mas_uno_nunca_colisiona_dentro_de_la_provincia():
    """
    El guard de colisión global de siguiente_ubigeo() es defensivo y, tal como
    está implementada la regla, inalcanzable: todo código nacional con el
    prefijo de la provincia entra en el cálculo del máximo, así que max+1 no
    puede existir ya. Se fija esa propiedad para que un refactor que la rompa
    haga fallar el test en vez de emitir un duplicado en silencio.
    """
    nacionales = ALTO_AMAZONAS | MARISCAL_RAMON_CASTILLA | {
        '160101', '160110', '160112', '160113', '150101'}
    for prov in ('1601', '1602', '1604'):
        assert siguiente_ubigeo(prov, nacionales) not in nacionales


def test_guard_secuencia_agotada():
    prov = {f'1601{i:02d}' for i in range(1, SECUENCIA_MAXIMA + 1)}
    with pytest.raises(UbigeoError, match='agotó la secuencia'):
        siguiente_ubigeo('1601', prov)


def test_guard_codigo_retirado():
    with pytest.raises(UbigeoError, match='retirado'):
        siguiente_ubigeo('1602', ALTO_AMAZONAS, retirados={'160212'})


def test_guard_provincia_mal_formada():
    for malo in ('16', '16040', 'abcd', '160a'):
        with pytest.raises(UbigeoError, match='mal formado'):
            siguiente_ubigeo(malo, ALTO_AMAZONAS)


def test_guard_provincia_sin_distritos():
    with pytest.raises(UbigeoError, match='no tiene ningún distrito'):
        siguiente_ubigeo('9999', ALTO_AMAZONAS)


def test_guard_ubigeos_mal_formados():
    with pytest.raises(UbigeoError, match='mal formados'):
        siguiente_ubigeo('1602', ALTO_AMAZONAS | {'16020'})


def test_contra_los_datos_reales():
    """1604 tiene 4 distritos publicados; a Santa Rosa le toca 160405."""
    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    fuente = RAIZ / cfg['rutas']['fuentes'] / 'distrito.geojson'
    if not fuente.exists():
        pytest.skip('fuentes/distrito.geojson no está; corra descargar.py')
    feats = json.loads(fuente.read_text(encoding='utf-8'))['features']
    todos = {f['properties']['ubigeo'] for f in feats}
    assert siguiente_ubigeo('1604', todos) == '160405'
