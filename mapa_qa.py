# -*- coding: utf-8 -*-
"""
mapa_qa.py — dibuja el antes/después de cada provincia tocada por el registro.

    py -3.11 mapa_qa.py       (después de construir.py)

Sale un PNG por provincia en qa/. La idea es que la revisión del corte se haga
mirando, no leyendo números: CI sube estos PNG como artefacto para que el
cambio de geometría se pueda aprobar de un vistazo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent


def anillos(geom):
    """Devuelve la lista de anillos exteriores de un Polygon/MultiPolygon."""
    t = geom['type']
    if t == 'Polygon':
        return [geom['coordinates'][0]]
    if t == 'MultiPolygon':
        return [p[0] for p in geom['coordinates']]
    return []


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print('matplotlib no está instalado; no se dibuja nada.')
        return 0

    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    qa = RAIZ / cfg['rutas']['qa']
    registro = yaml.safe_load(
        (RAIZ / cfg['rutas']['leyes'] / 'registro.yml').read_text(encoding='utf-8'))

    hechos = 0
    for entrada in registro.get('distritos') or []:
        prov = entrada['provincia']
        antes = qa / f'{prov}_antes.geojson'
        despues = qa / f'{prov}_despues.geojson'
        if not (antes.exists() and despues.exists()):
            print(f'faltan las capas de QA de {prov}; corra construir.py')
            continue

        fig, ejes = plt.subplots(1, 2, figsize=(15, 7.5), sharex=True, sharey=True)
        for eje, ruta, titulo in ((ejes[0], antes, f'{prov} — antes'),
                                  (ejes[1], despues, f'{prov} — después')):
            feats = json.loads(ruta.read_text(encoding='utf-8'))['features']
            for f in feats:
                derivado = f['properties'].get('fuente') == 'derivado'
                for anillo in anillos(f['geometry']):
                    xs = [c[0] for c in anillo]
                    ys = [c[1] for c in anillo]
                    eje.fill(xs, ys,
                             facecolor='#d94801' if derivado else '#c6dbef',
                             edgecolor='#2171b5' if not derivado else '#7f2704',
                             linewidth=0.7, alpha=0.9, zorder=2 if derivado else 1)
                # etiqueta en el centroide aproximado del primer anillo
                anillo = anillos(f['geometry'])[0]
                cx = sum(c[0] for c in anillo) / len(anillo)
                cy = sum(c[1] for c in anillo) / len(anillo)
                eje.annotate(f['properties'].get('nombre', ''), (cx, cy),
                             ha='center', va='center', fontsize=7,
                             color='#111111')
            eje.set_title(titulo, fontsize=12)
            eje.set_aspect('equal')
            eje.set_xlabel('lon')
            eje.grid(alpha=0.2, linewidth=0.4)
        ejes[0].set_ylabel('lat')
        fig.suptitle(
            f'{entrada["nombre"]} — Ley {entrada["ley"]} — en naranja lo '
            f'derivado, no oficial', fontsize=13)
        fig.tight_layout()
        salida = qa / f'{prov}_antes_despues.png'
        fig.savefig(salida, dpi=110)
        plt.close(fig)
        print(f'  {salida.relative_to(RAIZ)}')
        hechos += 1

    if not hechos:
        print('nada que dibujar.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
