# -*- coding: utf-8 -*-
"""
cargar_censo.py — pega el ubigeo a un tabulado del Censo 2025.

    py -3.11 ejemplos/cargar_censo.py Indicadores_demográficos.xlsx
    py -3.11 ejemplos/cargar_censo.py archivo.xlsx --hoja INDDEM06 --col-valor 4

Descarga del archivo de referencia (Indicadores demográficos, ~1.2 MB):
https://sistemas.inei.gob.pe/dir-segmentacion-ci/postcensal/prod/adjuntos/censos-2025/descarga_datos/tabulados/00/poblacion/Indicadores_demogr%C3%A1ficos.xlsx

**Sólo la hoja INDDEM06 baja a provincia y distrito.** Las demás
(INDDEM02…INDDEM05) son nacionales, e INDDEM01 llega a distrito pero con otra
disposición. Por eso `--hoja` está por defecto en INDDEM06.

Los tabulados **no traen ubigeo**: identifican las unidades por nombre y la
jerarquía va implícita en el ORDEN de las filas. El cruce se hace por clave
jerárquica (`departamento|provincia|distrito`) contra `salida/ubigeos_2026.csv`,
nunca por nombre suelto: hay 100 nombres de distrito repetidos que afectan a 257
distritos.

Emite `ubigeo,nivel,nombre,clave_censo,total,hombre,mujer` — las tres medidas de
población total del cuadro, para poder filtrar por sexo sin volver a leer el
Excel.

RESULTADO DEL CRUCE (verificado)
--------------------------------
    departamentos   25 / 25
    provincias     196 / 196
    distritos     1892 / 1892
    suma de los distritos: 34 157 732

Esa suma es exactamente el total nacional que publica el INEI; con las columnas
de población censada da 32 706 028, también exacto. Cero filas sin cruzar.

PARTICULARIDADES DEL ARCHIVO
----------------------------
Tres cosas que no se adivinan y que este script resuelve:

  1. **Los departamentos no llevan prefijo.** Las provincias son
     `PROVINCIA CHACHAPOYAS` y los distritos `DISTRITO ASUNCIÓN`, pero el
     departamento es sólo `AMAZONAS`.

  2. **Lima y Callao no siguen la jerarquía.** El censo abre tres bloques de
     primer nivel —`LIMA METROPOLITANA`, `REGION LIMA` y `PROV. CONSTITUCIONAL
     DEL CALLAO`— y en los dos primeros los distritos cuelgan directamente, sin
     fila `PROVINCIA`. Por eso el archivo trae 194 provincias y no 196: Lima
     (1501) y Callao (0701) están como bloque, no como fila. Se mapean en
     BLOQUES y luego se reagrega provincia y departamento sumando distritos,
     que es lo que devuelve los 196 y los 25 completos y coherentes.

  3. **Cinco nombres no coinciden** con el registro oficial de ubigeos:

         censo                     registro oficial        ubigeo
         ANCO-HUALLO               Anco_Huallo             030602
         SAN FRANCISCO DEL YESO    San Francisco de Yeso   010517
         SANTA CRUZ DE TOLED       Santa Cruz de Toledo    060506
         RAIMONDI                  Raymondi                250201
         SAN PEDRO DE LARAOS       Laraos                  150712

     `SANTA CRUZ DE TOLED` viene truncado en el propio Excel. El `Laraos` de
     Huarochirí no choca con el de Yauyos (151018) porque la clave lleva la
     provincia dentro, que es para lo que sirve. Van en ALIAS, abajo.

COLUMNAS DE INDDEM06
--------------------
    1 población total     2 total hombre      3 total mujer
    4 población censada   5 censada hombre    6 censada mujer
    7 población omitida   8 omitida hombre    9 omitida mujer

    total = censada + omitida  (34 157 732 = 32 706 028 + 1 451 704)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / 'src'))

from descargar_ubigeos import normalizar  # noqa: E402

PREFIJOS = {'DISTRITO': 'distrito', 'PROVINCIA': 'provincia'}

# Bloques de primer nivel que no son un departamento: el censo los abre como
# cabecera y sus distritos cuelgan directamente.
#   bloque -> (departamento, provincia o None si abajo vienen filas PROVINCIA)
BLOQUES = {
    'PROV. CONSTITUCIONAL DEL CALLAO': ('CALLAO', 'PROV. CONST. DEL CALLAO'),
    'LIMA METROPOLITANA': ('LIMA', 'LIMA'),
    'REGION LIMA': ('LIMA', None),
}

# Nombres del censo que no coinciden con el registro oficial de ubigeos.
#   (nivel, nombre en el censo) -> nombre en el catálogo
ALIAS = {
    ('distrito', 'ANCO-HUALLO'): 'ANCO_HUALLO',              # 030602
    ('distrito', 'SAN FRANCISCO DEL YESO'): 'SAN FRANCISCO DE YESO',  # 010517
    ('distrito', 'SANTA CRUZ DE TOLED'): 'SANTA CRUZ DE TOLEDO',      # 060506
    ('distrito', 'RAIMONDI'): 'RAYMONDI',                    # 250201
    # Huarochirí: el censo lo llama "San Pedro de Laraos"; el registro, sólo
    # "Laraos". No choca con el otro Laraos (151018, Yauyos) porque la clave
    # lleva la provincia dentro.
    ('distrito', 'SAN PEDRO DE LARAOS'): 'LARAOS',           # 150712
}


URL_INDICADORES = (
    'https://sistemas.inei.gob.pe/dir-segmentacion-ci/postcensal/prod/'
    'adjuntos/censos-2025/descarga_datos/tabulados/00/poblacion/'
    'Indicadores_demogr%C3%A1ficos.xlsx')


def rel(p) -> str:
    try:
        return str(Path(p).resolve().relative_to(RAIZ))
    except ValueError:
        return str(p)


def descargar(destino: Path) -> Path:
    """Baja el Excel de indicadores demográficos del INEI."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    s = requests.Session()
    s.verify = False
    s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                      'Referer': 'https://censos2025.inei.gob.pe/'})
    r = s.get(URL_INDICADORES, timeout=(30, 600))
    r.raise_for_status()
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(r.content)
    print(f'descargado {len(r.content):,} bytes -> {rel(destino)}')
    return destino


def cargar_catalogo(version: str) -> dict:
    """(nivel, clave_censo) -> ubigeo. Sale de src/descargar_ubigeos.py."""
    ruta = RAIZ / 'salida' / f'ubigeos_{version}.csv'
    if not ruta.exists():
        raise SystemExit(
            f'falta {rel(ruta)}; corra primero src/descargar_ubigeos.py')
    with open(ruta, encoding='utf-8', newline='') as f:
        return {(r['nivel'], r['clave_censo']): r['ubigeo']
                for r in csv.DictReader(f)}


def leer_filas(ruta: Path, hoja):
    if ruta.suffix.lower() in ('.csv', '.txt'):
        with open(ruta, encoding='utf-8-sig', newline='') as f:
            return [list(r) for r in csv.reader(f)]
    try:
        import openpyxl
    except ImportError:
        raise SystemExit('para leer Excel hace falta openpyxl')
    wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
    if hoja not in wb.sheetnames:
        raise SystemExit(f'la hoja {hoja!r} no está; hay: {wb.sheetnames}')
    return [list(f) for f in wb[hoja].iter_rows(values_only=True)]


def a_numero(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r'[^\d,.\-]', '', str(v)).replace(',', '')
    try:
        return float(s) if s not in ('', '-', '.') else None
    except ValueError:
        return None


def clasificar(texto: str):
    """'DISTRITO ASUNCIÓN' -> ('distrito', 'ASUNCION')."""
    n = normalizar(texto)
    for pref, nivel in PREFIJOS.items():
        if n.startswith(pref + ' '):
            return nivel, n[len(pref) + 1:].strip()
    return None, n


def procesar(filas, col_nombre: int, medidas: dict, catalogo: dict):
    """`medidas` es {nombre_de_columna: índice en la hoja}."""
    dep = prov = None
    salida, sin_cruce = [], []
    for i, fila in enumerate(filas, 1):
        if col_nombre >= len(fila) or fila[col_nombre] is None:
            continue
        crudo = str(fila[col_nombre]).strip()
        if not crudo or crudo.upper().startswith('NOTA'):
            continue
        nivel, nombre = clasificar(crudo)
        cifras = {k: (a_numero(fila[j]) if j < len(fila) else None)
                  for k, j in medidas.items()}
        valor = cifras[next(iter(medidas))]

        if nivel is None:
            # Cabecera de bloque: sólo cuenta si trae cifras (así se descartan
            # los títulos y encabezados de la hoja).
            if valor is None or nombre in ('PERU',):
                continue
            if nombre in BLOQUES:
                dep, prov = BLOQUES[nombre]
            else:
                dep, prov = nombre, None
            nivel = 'departamento'
            clave = dep
            # Un bloque tipo LIMA METROPOLITANA no es un departamento: su cifra
            # es parcial y no debe emitirse como tal.
            if nombre in BLOQUES:
                continue
        elif nivel == 'provincia':
            prov = ALIAS.get(('provincia', nombre), nombre)
            clave = f'{dep}|{prov}' if dep else None
        else:
            nombre = ALIAS.get(('distrito', nombre), nombre)
            clave = f'{dep}|{prov}|{nombre}' if dep and prov else None

        if clave is None:
            sin_cruce.append((i, nivel, nombre, 'sin departamento/provincia'))
            continue
        ubigeo = catalogo.get((nivel, clave))
        if ubigeo is None:
            sin_cruce.append((i, nivel, nombre, f'clave sin cruce: {clave}'))
            continue
        salida.append({'ubigeo': ubigeo, 'nivel': nivel, 'nombre': nombre,
                       'clave_censo': clave, **cifras})
    return salida, sin_cruce


def agregar_niveles(reg: list, medidas: dict) -> list:
    """
    Completa provincia y departamento sumando sus distritos.

    Hace falta porque el censo no publica fila propia para Lima (1501) ni para
    Callao (0701) —van como bloque—, y porque `REGION LIMA` y
    `LIMA METROPOLITANA` parten el departamento 15 en dos cifras parciales.
    Sumar desde el distrito da los 25 y 196 completos y coherentes.
    """
    out = [r for r in reg if r['nivel'] == 'distrito']
    nombres = {r['ubigeo']: (r['nombre'], r['clave_censo'])
               for r in reg if r['nivel'] != 'distrito'}
    for largo, nivel in ((4, 'provincia'), (2, 'departamento')):
        acum = {}
        for r in out:
            if r['nivel'] != 'distrito':
                continue
            k = r['ubigeo'][:largo]
            for m in medidas:
                if r[m] is not None:
                    acum.setdefault(k, {}).setdefault(m, 0.0)
                    acum[k][m] += r[m]
        for u, cifras in sorted(acum.items()):
            nom, clave = nombres.get(u, ('', ''))
            out.append({'ubigeo': u, 'nivel': nivel, 'nombre': nom,
                        'clave_censo': clave,
                        **{m: cifras.get(m) for m in medidas}})
    return out


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('tabulado', type=Path, nargs='?',
                   help='xlsx local; si se omite se baja del INEI')
    p.add_argument('--hoja', default='INDDEM06',
                   help='hoja a leer (por defecto INDDEM06, la única que baja '
                        'a provincia y distrito)')
    p.add_argument('--col-nombre', type=int, default=0)
    p.add_argument('--censada', action='store_true',
                   help='usar población censada (col. 4-6) en vez de la total')
    p.add_argument('--version', default='2025')
    p.add_argument('--salida', type=Path,
                   default=RAIZ / 'ejemplos' / 'datos' / 'censo.csv')
    a = p.parse_args()

    # Población total y su desagregación por sexo. Con --censada se toman las
    # columnas de población efectivamente empadronada.
    base = 4 if a.censada else 1
    medidas = {'total': base, 'hombre': base + 1, 'mujer': base + 2}

    if a.tabulado is None:
        a.tabulado = descargar(
            RAIZ / 'ejemplos' / 'datos' / 'Indicadores_demograficos.xlsx')

    catalogo = cargar_catalogo(a.version)
    filas = leer_filas(a.tabulado, a.hoja)
    print(f'{len(filas)} filas de {a.tabulado.name} [{a.hoja}]')
    print(f'medidas: población '
          f'{"censada" if a.censada else "total"} (columnas '
          f'{", ".join(str(v) for v in medidas.values())})')

    reg, sin_cruce = procesar(filas, a.col_nombre, medidas, catalogo)
    dist = [r for r in reg if r['nivel'] == 'distrito']
    print(f'{len(dist)} distritos cruzados directamente')

    reg = agregar_niveles(reg, medidas)

    esperado = {'departamento': 25, 'provincia': 196, 'distrito': 1892}
    print('\nCOBERTURA')
    todo_ok = True
    for nivel, n in esperado.items():
        hay = sum(1 for r in reg if r['nivel'] == nivel)
        con = sum(1 for r in reg if r['nivel'] == nivel and r['total'] is not None)
        marca = '✓' if hay == n else '⚠'
        todo_ok &= hay == n
        print(f'  {marca} {nivel:<14} {hay:>5} de {n:<5} ({con} con cifra)')

    print()
    for m in medidas:
        s = sum(r[m] for r in reg
                if r['nivel'] == 'distrito' and r[m] is not None)
        print(f'  suma de los distritos [{m:<7}] {s:>14,.0f}')

    if sin_cruce:
        print(f'\n⚠ {len(sin_cruce)} filas sin cruzar (primeras 12):')
        for i, niv, nom, por in sin_cruce[:12]:
            print(f'    fila {i:<6} {niv:<13} {nom[:32]:<32} {por}')

    campos = ['ubigeo', 'nivel', 'nombre', 'clave_censo'] + list(medidas)
    a.salida.parent.mkdir(parents=True, exist_ok=True)
    with open(a.salida, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=campos, lineterminator='\n')
        w.writeheader()
        w.writerows(sorted(reg, key=lambda r: (len(r['ubigeo']), r['ubigeo'])))
    print(f'\n{len(reg)} filas -> {rel(a.salida)}')
    return 0 if todo_ok and not sin_cruce else 1


if __name__ == '__main__':
    sys.exit(main())
