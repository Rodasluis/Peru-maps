# -*- coding: utf-8 -*-
"""
src/descargar_ubigeos.py — extrae el listado OFICIAL de ubigeos y nombres del
SISCONCODE del INEI (Sistema de Consulta de Códigos Estandarizados).

    py -3.11 src/descargar_ubigeos.py
    py -3.11 src/descargar_ubigeos.py --version 2024

Escribe salida/ubigeos_<version>.csv con los tres niveles y, además de los
nombres oficiales, las columnas que hacen falta para cruzar con los tabulados
del Censo 2025, que **no traen ubigeo**: sólo nombres del tipo
"PROVINCIA CHACHAPOYAS" o "DISTRITO ASUNCIÓN".

Dos avisos sobre ese cruce:

  * Los nombres distritales NO son únicos en el país (hay 14 "SANTA ROSA").
    Cruzar sólo por `nombre_censo` produce filas duplicadas. Use `clave_censo`,
    que lleva departamento y provincia incorporados.
  * `nombre_censo` conserva las tildes tal como las publica el INEI
    ("DISTRITO ASUNCIÓN"); `clave_censo` y `nombre_normalizado` van sin tildes,
    para que un archivo con la codificación estropeada siga cruzando.

Ojo con el HTML: la página mete un <div style="display:none"> por fila con una
ficha de detalle, y esas fichas llevan dentro sus propias <tr>. Si no se quitan
primero, se parsean como filas de datos (se midió: 3 980 "provincias" y ningún
departamento en vez de 196 y 25).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import requests
import urllib3
import yaml

# El normalizador de nombres vive en ubigeo.py, con el resto de la lógica de
# códigos: este módulo lo reexporta porque los ejemplos y los tests lo importan
# de aquí desde antes.
from ubigeo import normalizar, sin_tildes    # noqa: F401

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# La raíz del repositorio: un nivel por encima de src/, donde están config.yml
# y las carpetas de datos (salida/, qa/, leyes/, fuentes/).
RAIZ = Path(__file__).resolve().parents[1]

NIVELES = ('departamento', 'provincia', 'distrito')
LARGO = {'departamento': 2, 'provincia': 4, 'distrito': 6}

_ABRE = re.compile(r'<div\b', re.I)
_CIERRA = re.compile(r'</div\s*>', re.I)
_MODAL = re.compile(
    r'<div\b[^>]*style\s*=\s*"[^"]*display\s*:\s*none[^"]*"[^>]*>', re.I)
_FILA = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.S | re.I)
_CELDA = re.compile(r'<td\b[^>]*>(.*?)</td>', re.S | re.I)
_COD = re.compile(r'^\s*(\d{2}|\d{4}|\d{6})\s+(\S.*?)\s*$', re.S)


def quitar_modales(html: str) -> str:
    """Elimina los <div style="display:none"> con todo su contenido anidado."""
    partes, i = [], 0
    while True:
        m = _MODAL.search(html, i)
        if not m:
            partes.append(html[i:])
            return ''.join(partes)
        partes.append(html[i:m.start()])
        j, prof = m.end(), 1
        while prof > 0:
            a = _ABRE.search(html, j)
            c = _CIERRA.search(html, j)
            if not c:
                j = len(html)
                break
            if a and a.start() < c.start():
                prof += 1
                j = a.end()
            else:
                prof -= 1
                j = c.end()
        i = j


def texto(celda: str) -> str:
    s = re.sub(r'<[^>]+>', ' ', celda).replace('&nbsp;', ' ')
    s = s.replace('&amp;', '&').replace('&#039;', "'").replace('&#034;', '"')
    return ' '.join(s.split())


def descargar(cfg: dict, version: str) -> str:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    sc = cfg['sisconcode']
    # El servicio IGNORA strVersion: los datos los elige versionCategoriaPK. Si
    # se pide una versión sin pk declarado, se para aquí en vez de devolver la
    # de otra versión con este nombre.
    pk = (sc.get('versiones') or {}).get(str(version))
    if pk is None:
        conocidas = ', '.join(sorted(sc.get('versiones') or {})) or '(ninguna)'
        raise SystemExit(
            f'versión {version}: sin versionCategoriaPK en config.yml '
            f'(sisconcode.versiones: {conocidas}). Añádalo antes de descargar; '
            f'sin él el servicio devuelve otra versión sin avisar.')

    s = requests.Session()
    s.verify = False       # el certificado del :8443 del INEI no valida
    s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                      'Accept': 'text/html,application/xhtml+xml'})
    # El servidor del INEI no siempre responde; se reintenta con espera
    # creciente. `connect=` hace falta: urllib3 no reintenta timeouts de
    # conexión si no se le pide.
    adaptador = HTTPAdapter(max_retries=Retry(
        total=5, connect=5, read=3, backoff_factor=3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({'GET'})))
    s.mount('https://', adaptador)
    s.mount('http://', adaptador)
    r = s.get(sc['url'], params={
        'versionCategoriaPK': pk,
        'nivel': '1',                   # con nivel=1 y TODOS devuelve el árbol
        'strVersion': version,          # completo; nivel=2 y 3 vienen vacíos
        'strDpto': 'TODOS', 'strProv': 'TODOS', 'strDist': 'TODOS',
        'flagDpto': '', 'flagProv': '', 'flagDist': '',
    }, timeout=(30, 600))
    r.raise_for_status()
    r.encoding = 'ISO-8859-1'           # lo declara la propia página
    return r.text


def parsear(html: str) -> list:
    """Devuelve [(nivel, ubigeo, nombre)] en el orden de la página."""
    limpio = quitar_modales(html)
    m = re.search(r'<tbody>(.*)</tbody>', limpio, re.S | re.I)
    if not m:
        raise SystemExit('no se encontró el <tbody> de la tabla; '
                         '¿cambió el HTML del SISCONCODE?')
    registros = []
    for fila in _FILA.findall(m.group(1)):
        celdas = [texto(c) for c in _CELDA.findall(fila)]
        if len(celdas) < 4:
            continue
        # columnas: [nº, departamento, provincia, distrito]; la fila describe
        # el nivel más profundo que traiga relleno
        for nivel, celda in (('distrito', celdas[3]),
                             ('provincia', celdas[2]),
                             ('departamento', celdas[1])):
            mm = _COD.match(celda)
            if mm and len(mm.group(1)) == LARGO[nivel]:
                registros.append((nivel, mm.group(1), mm.group(2)))
                break
    return registros


def construir_filas(registros: list) -> list:
    nombres = {u: n for _, u, n in registros}
    prefijo = {'departamento': 'DEPARTAMENTO', 'provincia': 'PROVINCIA',
               'distrito': 'DISTRITO'}
    filas = []
    for nivel, u, nombre in registros:
        dep, prov = u[:2], u[:4] if len(u) >= 4 else ''
        nom_dep = nombres.get(dep, '')
        nom_prov = nombres.get(prov, '') if prov else ''
        # clave jerárquica: los nombres de distrito se repiten en el país
        partes = [normalizar(nom_dep)]
        if nivel in ('provincia', 'distrito'):
            partes.append(normalizar(nom_prov))
        if nivel == 'distrito':
            partes.append(normalizar(nombre))
        filas.append({
            'ubigeo': u,
            'nivel': nivel,
            'nombre': nombre,
            'nombre_normalizado': normalizar(nombre),
            'nombre_censo': f'{prefijo[nivel]} {nombre.upper()}',
            'nombre_censo_normalizado': f'{prefijo[nivel]} {normalizar(nombre)}',
            'clave_censo': '|'.join(partes),
            'ubigeo_departamento': dep,
            'nombre_departamento': nom_dep,
            'ubigeo_provincia': prov,
            'nombre_provincia': nom_prov,
        })
    return filas


CAMPOS = ['ubigeo', 'nivel', 'nombre', 'nombre_normalizado', 'nombre_censo',
          'nombre_censo_normalizado', 'clave_censo', 'ubigeo_departamento',
          'nombre_departamento', 'ubigeo_provincia', 'nombre_provincia']


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', default=str(cfg['sisconcode']['version']),
                   help='año de la versión de ubigeo (por defecto config.yml)')
    p.add_argument('--destino', type=Path, default=None)
    args = p.parse_args()

    print(f'SISCONCODE, versión {args.version} …')
    registros = parsear(descargar(cfg, args.version))

    conteo = {n: sum(1 for lv, _, _ in registros if lv == n) for n in NIVELES}
    print(f'  departamentos {conteo["departamento"]:>5}')
    print(f'  provincias    {conteo["provincia"]:>5}')
    print(f'  distritos     {conteo["distrito"]:>5}')

    codigos = [u for _, u, _ in registros]
    repetidos = {c for c in codigos if codigos.count(c) > 1} if \
        len(codigos) != len(set(codigos)) else set()
    if repetidos:
        raise SystemExit(f'ubigeos repetidos en la fuente: {sorted(repetidos)[:8]}')

    esperado = cfg.get('ubigeos_oficiales', {})
    for nivel in NIVELES:
        if nivel in esperado and conteo[nivel] != esperado[nivel]:
            print(f'  ⚠ {nivel}: se leyeron {conteo[nivel]} y config.yml '
                  f'espera {esperado[nivel]}')

    filas = construir_filas(registros)
    destino = args.destino or (RAIZ / cfg['rutas']['salida'] /
                               f'ubigeos_{args.version}.csv')
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, 'w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS, lineterminator='\n')
        w.writeheader()
        w.writerows(sorted(filas, key=lambda r: (len(r['ubigeo']), r['ubigeo'])))
    try:
        rel = destino.relative_to(RAIZ)
    except ValueError:      # --destino puede apuntar fuera del repo
        rel = destino
    print(f'\n{len(filas)} filas -> {rel}')

    # los nombres distritales que se repiten: por qué hace falta clave_censo
    distritos = [r for r in filas if r['nivel'] == 'distrito']
    vistos = {}
    for r in distritos:
        vistos.setdefault(r['nombre_normalizado'], []).append(r['ubigeo'])
    ambiguos = {k: v for k, v in vistos.items() if len(v) > 1}
    print(f'\nnombres de distrito que se repiten: {len(ambiguos)} '
          f'({sum(len(v) for v in ambiguos.values())} distritos)')
    for nombre, us in sorted(ambiguos.items(),
                             key=lambda kv: -len(kv[1]))[:5]:
        print(f'   {nombre:<28} {len(us)} veces')
    print('   -> cruce por `clave_censo`, no por `nombre_censo`')
    return 0


if __name__ == '__main__':
    sys.exit(main())
