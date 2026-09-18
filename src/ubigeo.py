# -*- coding: utf-8 -*-
"""
Regla de asignación de ubigeo INEI para distritos de creación reciente.

El INEI numera los distritos alfabéticamente dentro de la provincia en la
compilación inicial (el `01` es siempre la capital provincial / cercado), pero
**agrega las creaciones nuevas al final** en vez de reinsertarlas
alfabéticamente.  Precedente confirmado: San Pablo, creado en 1993 en Mariscal
Ramón Castilla, recibió `160404` aunque alfabéticamente precede a Yavarí
(`160403`).

De ahí que la regla sea `max + 1` y **no** `count + 1`.  Las secuencias
provinciales tienen huecos: cuando se crea una provincia nueva, los distritos
que se mudan a ella conservan su código antiguo y el INEI deja el hueco en vez
de renumerar — eso es justamente lo que protege las series históricas.

Ejemplos reales en Loreto, verificados contra la capa `ig_distrito`:

    Alto Amazonas (1602): 160201 160202 160205 160206 160210 160211
                          -> n=6, max=11, huecos en 03 04 07 08 09
    Maynas        (1601): n=11, max=13, huecos en 09 y 11

Usar `count + 1` en Alto Amazonas emitiría `160207`, un código retirado que
pertenece a un distrito que pasó a Datem del Marañón: una colisión que
corrompería en silencio cualquier join contra datos anteriores a 2005.

El código que devuelve esta función es una **predicción** de lo que asignará el
INEI, no una autoridad.  Se marca `provisional` en las propiedades del feature
y puede sobrescribirse a mano desde leyes/registro.yml.
"""

from __future__ import annotations

import csv
import unicodedata
from pathlib import Path

LONGITUD_DISTRITO = 6
LONGITUD_PROVINCIA = 4
SECUENCIA_MAXIMA = 99


class UbigeoError(ValueError):
    """La regla no puede emitir un código con garantías. Nunca se adivina."""


def sin_tildes(s: str) -> str:
    d = unicodedata.normalize('NFKD', s)
    return ''.join(c for c in d if not unicodedata.combining(c))


def normalizar(s: str) -> str:
    """Mayúsculas, sin tildes, espacios colapsados: para cruzar sin sustos."""
    return ' '.join(sin_tildes(str(s or '')).upper().split())


def siguiente_ubigeo(codigo_provincia: str,
                     ubigeos_nacionales,
                     retirados=frozenset()) -> str:
    """
    Devuelve el ubigeo que le correspondería al próximo distrito creado en
    `codigo_provincia`.

    codigo_provincia   '1604'
    ubigeos_nacionales iterable con TODOS los ubigeos distritales del país;
                       se usa para el guard de colisión global, no sólo dentro
                       de la provincia.
    retirados          códigos que estuvieron en uso y ya no lo están.  La
                       regla max+1 nunca cae en un hueco (los huecos están por
                       debajo del máximo por definición), así que este guard
                       sólo muerde si se retiró un código POR ENCIMA del máximo
                       actual.  Ver README.

    Falla ruidosamente en vez de emitir un código dudoso.
    """
    if not isinstance(codigo_provincia, str):
        raise UbigeoError(
            f'el código de provincia debe ser str, no {type(codigo_provincia).__name__}')
    if len(codigo_provincia) != LONGITUD_PROVINCIA or not codigo_provincia.isdigit():
        raise UbigeoError(
            f'código de provincia mal formado: {codigo_provincia!r} '
            f'(se esperan {LONGITUD_PROVINCIA} dígitos)')

    nacionales = {str(u).strip() for u in ubigeos_nacionales}
    malformados = {u for u in nacionales
                   if len(u) != LONGITUD_DISTRITO or not u.isdigit()}
    if malformados:
        raise UbigeoError(
            f'hay ubigeos distritales mal formados en la entrada: '
            f'{sorted(malformados)[:5]}')

    en_provincia = {u for u in nacionales if u[:LONGITUD_PROVINCIA] == codigo_provincia}
    if not en_provincia:
        raise UbigeoError(
            f'la provincia {codigo_provincia} no tiene ningún distrito en la '
            f'entrada; no hay secuencia de la cual derivar max+1')

    secuencia = max(int(u[LONGITUD_PROVINCIA:]) for u in en_provincia)
    siguiente = secuencia + 1

    if siguiente > SECUENCIA_MAXIMA:
        raise UbigeoError(
            f'la provincia {codigo_provincia} agotó la secuencia distrital: '
            f'max={secuencia:02d}, no cabe un {siguiente} en dos dígitos')

    nuevo = f'{codigo_provincia}{siguiente:02d}'

    if nuevo in nacionales:
        raise UbigeoError(
            f'{nuevo} ya está en uso en el país; la secuencia de '
            f'{codigo_provincia} no es lo que parece')

    retirados = {str(r).strip() for r in retirados}
    if nuevo in retirados:
        raise UbigeoError(
            f'{nuevo} figura como código retirado; reutilizarlo rompería los '
            f'joins contra series históricas')

    return nuevo


def cargar_retirados(ruta) -> set:
    """
    Lee la blocklist de códigos retirados.  Devuelve un set vacío si el archivo
    no existe todavía: el guard queda inerte hasta que haya datos que cargarle.
    """
    ruta = Path(ruta)
    if not ruta.exists():
        return set()
    codigos = set()
    with open(ruta, encoding='utf-8-sig', newline='') as f:
        for fila in csv.DictReader(f):
            u = (fila.get('ubigeo') or '').strip()
            if u:
                codigos.add(u.zfill(LONGITUD_DISTRITO))
    return codigos


def cargar_registro_oficial(ruta) -> dict:
    """
    Lee el listado OFICIAL de ubigeos (salida/ubigeos_<version>.csv, que produce
    src/descargar_ubigeos.py desde el SISCONCODE) y devuelve

        {(código de provincia, nombre normalizado): ubigeo}

    para los distritos.  Es lo que permite **usar el código que el INEI ya
    asignó** en vez de predecirlo con `siguiente_ubigeo`.

    Se lee del CSV versionado y no de una consulta en vivo a propósito: el build
    tiene que poder correrse sin red y dar siempre el mismo resultado.  Refrescar
    ese CSV es trabajo de src/descargar_ubigeos.py, el único paso que sale a la
    red.

    Devuelve {} si el archivo no existe: entonces se cae a la regla max+1 y los
    códigos salen marcados provisionales, igual que antes de que este listado
    existiera.
    """
    ruta = Path(ruta)
    if not ruta.exists():
        return {}
    oficiales = {}
    with open(ruta, encoding='utf-8-sig', newline='') as f:
        for fila in csv.DictReader(f):
            if (fila.get('nivel') or '').strip() != 'distrito':
                continue
            u = (fila.get('ubigeo') or '').strip()
            if len(u) != LONGITUD_DISTRITO or not u.isdigit():
                continue
            clave = ((fila.get('ubigeo_provincia') or '').strip(),
                     normalizar(fila.get('nombre_normalizado')
                                or fila.get('nombre')))
            anterior = oficiales.get(clave)
            if anterior and anterior != u:
                raise UbigeoError(
                    f'el listado oficial trae dos códigos para {clave}: '
                    f'{anterior} y {u}. No se puede elegir por nombre.')
            oficiales[clave] = u
    return oficiales


def huecos_de_provincia(codigo_provincia: str, ubigeos_nacionales) -> list:
    """
    Devuelve las posiciones libres por debajo del máximo de la provincia.
    No se usa para asignar — sólo para reportar en el QA por qué `count + 1`
    sería incorrecto en esa provincia.
    """
    en_provincia = sorted(
        int(str(u)[LONGITUD_PROVINCIA:]) for u in ubigeos_nacionales
        if str(u)[:LONGITUD_PROVINCIA] == codigo_provincia)
    if not en_provincia:
        return []
    return [i for i in range(1, max(en_provincia) + 1) if i not in en_provincia]
