#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AUDITORÍA DE LAS CAPAS DE LÍMITES DEL INEI
GeoServer "Interoperabilidad" — https://geoespacial.inei.gob.pe

No solo cuenta: reconstruye la jerarquía de ubigeos y verifica que las tres
capas sean consistentes entre sí.

    pip install requests
    python src/auditar_limites_inei.py                 # auditoría + guarda GeoJSON
    python src/auditar_limites_inei.py --hits          # solo los conteos, rápido
    python src/auditar_limites_inei.py --referencia distritos_censo2025.csv

La opción --referencia recibe un CSV con una columna de ubigeos de 6 dígitos
(por ejemplo el listado del Censo 2025) y te dice exactamente qué distritos
están en el censo y no en la cartografía, y viceversa.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

try:
    import requests
    import urllib3
except ImportError:
    sys.exit('Falta requests.  Instálalo con:  pip install requests')

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

OWS = 'https://geoespacial.inei.gob.pe/geoserver/Interoperabilidad/ows'

CAPAS = {
    'departamento': ('Interoperabilidad:ig_departamento', 2, 25),
    'provincia':    ('Interoperabilidad:ig_provincia',    4, 196),
    'distrito':     ('Interoperabilidad:ig_distrito',     6, 1891),
}

# Los esquemas de las tres capas no son paralelos: la de distrito publica un
# campo 'ubigeo' ya concatenado, pero la de provincia solo trae 'ccdd' y 'ccpp'
# por separado.  Estos son los componentes con que se arma el ubigeo cuando no
# existe un campo único de la longitud esperada.
COMPONENTES = {
    'departamento': ['ccdd'],
    'provincia':    ['ccdd', 'ccpp'],
    'distrito':     ['ccdd', 'ccpp', 'ccdi'],
}

PAGINA = 1000


def sesion():
    s = requests.Session()
    s.verify = False
    s.headers.update({'User-Agent': 'Mozilla/5.0'})
    return s


# ---------------------------------------------------------------------------
# Consultas al WFS
# ---------------------------------------------------------------------------
def contar(s, capa):
    """resultType=hits: devuelve el total sin descargar geometría."""
    r = s.get(OWS, params={'service': 'WFS', 'version': '2.0.0',
                           'request': 'GetFeature', 'typeNames': capa,
                           'resultType': 'hits'}, timeout=(30, 300))
    r.raise_for_status()
    m = re.search(r'numberMatched="(\d+)"', r.text)
    if not m:
        print(r.text[:500])
        raise SystemExit('El servidor no devolvió numberMatched.')
    return int(m.group(1))


def bajar(s, capa):
    """Descarga la capa completa paginando; devuelve la lista de features."""
    rasgos, inicio = [], 0
    while True:
        r = s.get(OWS, params={
            'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature',
            'typeNames': capa, 'outputFormat': 'application/json',
            'srsName': 'EPSG:4326', 'count': PAGINA, 'startIndex': inicio,
        }, timeout=(30, 900))
        r.raise_for_status()
        datos = r.json()
        lote = datos.get('features', [])
        rasgos.extend(lote)
        if len(lote) < PAGINA:
            break
        inicio += PAGINA
    return rasgos


# ---------------------------------------------------------------------------
# Detección del campo de ubigeo
# ---------------------------------------------------------------------------
def detectar_campo_ubigeo(rasgos, digitos):
    """
    Los nombres de campo varían entre capas del INEI (IDDIST, CCDI, UBIGEO,
    IDPROV...).  Se elige el campo cuyos valores sean códigos numéricos de la
    longitud esperada y que además sean casi todos distintos.
    """
    if not rasgos:
        return None
    candidatos = {}
    for clave in rasgos[0].get('properties', {}):
        valores = [str(f['properties'].get(clave, '')).strip()
                   for f in rasgos]
        validos = [v for v in valores if v.isdigit() and len(v) == digitos]
        if len(validos) < len(rasgos) * 0.95:
            continue
        candidatos[clave] = len(set(validos))
    if not candidatos:
        return None
    # el mejor candidato es el más cercano a ser identificador único
    return max(candidatos, key=candidatos.get)


def componer_ubigeo(rasgos, nivel, digitos):
    """
    Arma el ubigeo concatenando los campos componentes del INEI.
    Devuelve (etiqueta_del_campo, lista_de_ubigeos) o None si no se puede.
    """
    campos = COMPONENTES.get(nivel, [])
    if not campos or not rasgos:
        return None
    if not all(c in rasgos[0].get('properties', {}) for c in campos):
        return None
    lista = [''.join(str(f['properties'].get(c, '')).strip() for c in campos)
             for f in rasgos]
    if not all(u.isdigit() and len(u) == digitos for u in lista):
        return None
    return '+'.join(campos), lista


def ubigeos(rasgos, campo):
    return [str(f['properties'].get(campo, '')).strip() for f in rasgos]


# ---------------------------------------------------------------------------
# Auditoría
# ---------------------------------------------------------------------------
def auditar(datos):
    """datos = {nivel: (campo, lista_de_ubigeos)}"""
    print('\n' + '=' * 66)
    print('AUDITORÍA')
    print('=' * 66)

    codigos = {}
    for nivel, (campo, lista) in datos.items():
        esperado = CAPAS[nivel][2]
        unicos = set(lista)
        codigos[nivel] = unicos
        repetidos = [u for u, n in Counter(lista).items() if n > 1]

        print(f'\n{nivel.upper()}   (campo de ubigeo: {campo})')
        print(f'  features                 {len(lista)}')
        print(f'  ubigeos únicos           {len(unicos)}   '
              f'(esperado {esperado})')

        if repetidos:
            print(f'  ⚠ ubigeos repetidos      {len(repetidos)}  '
                  f'→ {sorted(repetidos)[:8]}')
            print('    Probablemente son geometrías multiparte explotadas '
                  '(islas, enclaves).\n'
                  '    Disuélvelas por ubigeo antes de hacer cualquier join.')
        if len(unicos) != esperado:
            signo = 'faltan' if len(unicos) < esperado else 'sobran'
            print(f'  ⚠ {signo} {abs(len(unicos) - esperado)} respecto '
                  f'del valor esperado')

    # --- consistencia jerárquica -------------------------------------------
    if 'distrito' in codigos:
        dist = codigos['distrito']
        prov_derivadas = {u[:4] for u in dist}
        dept_derivados = {u[:2] for u in dist}

        print('\nJERARQUÍA RECONSTRUIDA DESDE LOS DISTRITOS')
        print(f'  provincias implícitas    {len(prov_derivadas)}   (esperado 196)')
        print(f'  departamentos implícitos {len(dept_derivados)}   '
              f'(esperado 25, incluye Callao = 07)')

        if 'provincia' in codigos:
            solo_capa = codigos['provincia'] - prov_derivadas
            solo_dist = prov_derivadas - codigos['provincia']
            if solo_capa or solo_dist:
                print('\n  ⚠ Las capas de provincia y distrito NO son coherentes')
                if solo_capa:
                    print(f'    en provincia pero sin distritos: {sorted(solo_capa)}')
                if solo_dist:
                    print(f'    en distritos pero sin provincia: {sorted(solo_dist)}')
            else:
                print('  ✓ provincia y distrito son coherentes entre sí')

        if 'departamento' in codigos:
            solo_capa = codigos['departamento'] - dept_derivados
            solo_dist = dept_derivados - codigos['departamento']
            if solo_capa or solo_dist:
                print('\n  ⚠ Las capas de departamento y distrito NO son coherentes')
                if solo_capa:
                    print(f'    en departamento pero sin distritos: {sorted(solo_capa)}')
                if solo_dist:
                    print(f'    en distritos pero sin departamento: {sorted(solo_dist)}')
            else:
                print('  ✓ departamento y distrito son coherentes entre sí')

    return codigos


def comparar_referencia(codigos, ruta: Path):
    """Contrasta los ubigeos distritales contra un listado externo."""
    referencia = set()
    with open(ruta, encoding='utf-8-sig', newline='') as f:
        for fila in csv.reader(f):
            for celda in fila:
                celda = celda.strip().strip('"')
                if celda.isdigit() and len(celda) in (5, 6):
                    referencia.add(celda.zfill(6))
    if not referencia:
        print(f'\nNo se encontraron ubigeos de 6 dígitos en {ruta.name}.')
        return

    capa = codigos.get('distrito', set())
    faltan = referencia - capa
    sobran = capa - referencia

    print('\n' + '=' * 66)
    print(f'CONTRASTE CONTRA {ruta.name}')
    print('=' * 66)
    print(f'  ubigeos en la referencia  {len(referencia)}')
    print(f'  ubigeos en la cartografía {len(capa)}')
    print(f'  en referencia y NO en cartografía: {len(faltan)}')
    for u in sorted(faltan):
        print(f'      {u}')
    print(f'  en cartografía y NO en referencia: {len(sobran)}')
    for u in sorted(sobran):
        print(f'      {u}')
    if faltan:
        print('\n  Estos son los distritos que quedarían sin polígono al hacer\n'
              '  el join. Revisa si son creaciones recientes: en ese caso su\n'
              '  población está contada aparte, pero el distrito madre sigue\n'
              '  existiendo con su geometría antigua, que ya no le corresponde.')


# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description='Audita las capas de límites del WFS del INEI.')
    p.add_argument('--destino', type=Path, default=Path('limites_inei'))
    p.add_argument('--hits', action='store_true',
                   help='solo pedir los conteos, sin descargar geometría')
    p.add_argument('--referencia', type=Path,
                   help='CSV con ubigeos de 6 dígitos para contrastar')
    p.add_argument('--sin-guardar', action='store_true',
                   help='no escribir los GeoJSON en disco')
    args = p.parse_args()

    s = sesion()

    print('CONTEOS (resultType=hits)')
    for nivel, (capa, _, esperado) in CAPAS.items():
        try:
            n = contar(s, capa)
            marca = '✓' if n == esperado else '⚠'
            print(f'  {marca} {nivel:<14} {n:>6}   (esperado {esperado})')
        except Exception as e:
            print(f'  ✗ {nivel:<14} error: {e}')

    if args.hits:
        print('\nOjo: un conteo correcto no garantiza que los ubigeos lo sean.\n'
              'Corre sin --hits para la auditoría de jerarquía.')
        return

    destino = args.destino.expanduser().resolve()
    if not args.sin_guardar:
        destino.mkdir(parents=True, exist_ok=True)

    print('\nDESCARGA COMPLETA')
    datos = {}
    for nivel, (capa, digitos, _) in CAPAS.items():
        try:
            rasgos = bajar(s, capa)
        except Exception as e:
            print(f'  ✗ {nivel}: {e}')
            continue
        # La geometría se guarda siempre: que no sepamos identificar el campo
        # de ubigeo no es razón para tirar una descarga que ya salió bien.
        if not args.sin_guardar:
            salida = destino / f'{nivel}.geojson'
            salida.write_text(json.dumps(
                {'type': 'FeatureCollection', 'features': rasgos},
                ensure_ascii=False), encoding='utf-8')

        campo = detectar_campo_ubigeo(rasgos, digitos)
        if campo is not None:
            datos[nivel] = (campo, ubigeos(rasgos, campo))
        else:
            compuesto = componer_ubigeo(rasgos, nivel, digitos)
            if compuesto is None:
                claves = list(rasgos[0]['properties']) if rasgos else []
                print(f'  ⚠ {nivel}: no se detectó campo de ubigeo de {digitos} '
                      f'dígitos. Campos disponibles: {claves}')
                continue
            datos[nivel] = compuesto
            campo = compuesto[0]
        print(f'  {nivel:<14} {len(rasgos):>6} features   campo: {campo}')

    if not datos:
        sys.exit('No se pudo descargar ninguna capa.')

    codigos = auditar(datos)

    if args.referencia:
        comparar_referencia(codigos, args.referencia)

    if not args.sin_guardar:
        print(f'\nGeoJSON guardados en {destino}')


if __name__ == '__main__':
    main()
