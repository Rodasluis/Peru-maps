# -*- coding: utf-8 -*-
"""
descargar.py — baja las tres capas de límites del WFS del INEI a fuentes/.

    py -3.11 descargar.py

Las capas pesan ~80 MB en total, por eso fuentes/ no se versiona.  El build
(construir.py) lee de ahí.

Nota sobre los esquemas: las tres capas NO son paralelas.  `ig_distrito` trae
un campo `ubigeo` ya concatenado, pero `ig_provincia` sólo trae `ccdd` y `ccpp`
por separado y `ig_departamento` sólo `ccdd`.  Aquí no se toca nada: se guarda
tal cual llega y la concatenación se hace en construir.py, documentada.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests
import urllib3
import yaml

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

RAIZ = Path(__file__).resolve().parent


def sesion():
    """
    Sesión con reintentos.

    El servidor del INEI no siempre responde —desde los runners de GitHub se
    han visto ConnectTimeout—, así que se reintenta con espera creciente en vez
    de caerse al primer intento. `connect=` es lo que importa aquí: urllib3 no
    reintenta timeouts de conexión si no se le pide explícitamente.
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.verify = False       # el certificado del INEI no valida
    s.headers.update({'User-Agent': 'Mozilla/5.0'})
    reintentos = Retry(total=5, connect=5, read=3, backoff_factor=3,
                       status_forcelist=(429, 500, 502, 503, 504),
                       allowed_methods=frozenset({'GET'}))
    adaptador = HTTPAdapter(max_retries=reintentos)
    s.mount('https://', adaptador)
    s.mount('http://', adaptador)
    return s


def contar(s, ows, capa) -> int:
    r = s.get(ows, params={'service': 'WFS', 'version': '2.0.0',
                           'request': 'GetFeature', 'typeNames': capa,
                           'resultType': 'hits'}, timeout=(30, 300))
    r.raise_for_status()
    m = re.search(r'numberMatched="(\d+)"', r.text)
    if not m:
        raise SystemExit(f'el servidor no devolvió numberMatched para {capa}')
    return int(m.group(1))


def bajar(s, ows, capa, pagina) -> list:
    rasgos, inicio = [], 0
    while True:
        r = s.get(ows, params={
            'service': 'WFS', 'version': '2.0.0', 'request': 'GetFeature',
            'typeNames': capa, 'outputFormat': 'application/json',
            'srsName': 'EPSG:4326', 'count': pagina, 'startIndex': inicio,
        }, timeout=(30, 900))
        r.raise_for_status()
        lote = r.json().get('features', [])
        rasgos.extend(lote)
        if len(lote) < pagina:
            break
        inicio += pagina
    return rasgos


def main() -> int:
    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    destino = RAIZ / cfg['rutas']['fuentes']
    destino.mkdir(parents=True, exist_ok=True)

    s = sesion()
    ows = cfg['inei']['ows']
    for nivel, capa in cfg['inei']['capas'].items():
        n = contar(s, ows, capa)
        rasgos = bajar(s, ows, capa, cfg['inei']['pagina'])
        if len(rasgos) != n:
            raise SystemExit(
                f'{nivel}: el servidor anunció {n} features y entregó '
                f'{len(rasgos)}; descarga incompleta')
        (destino / f'{nivel}.geojson').write_text(
            json.dumps({'type': 'FeatureCollection', 'features': rasgos},
                       ensure_ascii=False), encoding='utf-8')
        print(f'  {nivel:<14} {len(rasgos):>6} features -> '
              f'{destino / f"{nivel}.geojson"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
