# -*- coding: utf-8 -*-
"""
mapa_python.py — coropletos de los tres niveles con geopandas + matplotlib.

    py -3.11 ejemplos/mapa_python.py
    py -3.11 ejemplos/mapa_python.py --nivel distrito --medida mujer

Lee la población del Censo 2025 de `ejemplos/datos/censo.csv`, que produce
`cargar_censo.py`. Si ese archivo no está, cae al área en km² calculada de la
geometría, para poder correr sin red.

Además del PNG escribe `ejemplos/datos/indicador.json` con las tres medidas por
los tres niveles: es lo que consume `mapa_web.html`, de modo que un solo sitio
decide qué se mapea.

Decisiones de diseño:
  * Rampa **secuencial de un solo tono**, claro→oscuro. Un arcoíris sobre una
    magnitud continua inventa cortes donde no los hay.
  * **6 clases por cuantiles**, propias de cada nivel. Pasando de ~7 las clases
    vecinas se confunden; y por cuantiles porque la población está muy sesgada
    (Lima Metropolitana contra distritos amazónicos) y con intervalos iguales
    casi todo caería en la clase más baja.
  * Bordes finos y recesivos, más finos cuanto más polígonos: con 1892
    distritos un borde de 1 px sería lo único que se ve.
  * Se leen las capas **simplificadas**, que existen para esto.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

NIVELES = (('departamento', 'Departamentos', 0.6),
           ('provincia', 'Provincias', 0.35),
           ('distrito', 'Distritos', 0.12))

MEDIDAS = {'total': 'Población total', 'hombre': 'Hombres', 'mujer': 'Mujeres'}
CENSO = RAIZ / 'ejemplos' / 'datos' / 'censo.csv'

# Rampa secuencial azul, 6 pasos claro->oscuro.
RAMPA = ['#cde2fb', '#9ec5f4', '#6da7ec', '#3987e5', '#1c5cab', '#0d366b']
SURFACE = '#fcfcfb'
INK = '#0b0b0b'
INK_2 = '#52514e'
MUTED = '#898781'
HAIRLINE = '#c3c2b7'


def rel(p) -> str:
    """Ruta legible. `relative_to` revienta si se pasó una ruta relativa."""
    try:
        return str(Path(p).resolve().relative_to(RAIZ))
    except ValueError:
        return str(p)


def leer_censo(ruta: Path) -> dict:
    """-> {medida: {ubigeo: valor}}. Devuelve {} si el archivo no está."""
    if not ruta.exists():
        return {}
    datos = {m: {} for m in MEDIDAS}
    with open(ruta, encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f):
            for m in MEDIDAS:
                v = (r.get(m) or '').strip()
                if v:
                    try:
                        datos[m][r['ubigeo']] = float(v)
                    except ValueError:
                        pass
    return {m: v for m, v in datos.items() if v}


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--nivel', default='todos',
                   choices=['todos'] + [n[0] for n in NIVELES],
                   help='qué nivel dibujar; por defecto los tres en paralelo')
    p.add_argument('--medida', default='total', choices=list(MEDIDAS),
                   help='qué población mapear; por defecto la total')
    p.add_argument('--salida', type=Path, default=None)
    p.add_argument('--completo', action='store_true',
                   help='usar la resolución completa en vez de la simplificada')
    a = p.parse_args()

    niveles = [n for n in NIVELES if a.nivel in ('todos', n[0])]
    if a.salida is None:
        cola = '' if a.nivel == 'todos' else f'_{a.nivel}'
        cola += '' if a.medida == 'total' else f'_{a.medida}'
        a.salida = RAIZ / 'ejemplos' / f'mapa_python{cola}.png'

    import geopandas as gpd
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Patch

    censo = leer_censo(CENSO)
    if censo:
        etiqueta = f'{MEDIDAS[a.medida]} (Censo 2025)'
        procedencia = CENSO.name
        valores = censo[a.medida]
    else:
        etiqueta, procedencia, valores = 'Área (km²)', 'geometría', None
        print(f'{rel(CENSO)} no está; se mapea el área. '
              f'Para el censo: py -3.11 ejemplos/cargar_censo.py')

    sufijo = '' if a.completo else '_simplificado'
    cmap = ListedColormap(RAMPA)
    ancho = 5.5 * len(niveles) if len(niveles) > 1 else 8.0
    fig, ejes = plt.subplots(1, len(niveles), figsize=(ancho, 7.4),
                             squeeze=False)
    ejes = ejes[0]
    fig.patch.set_facecolor(SURFACE)

    capas = {}
    for eje, (nivel, titulo, grosor) in zip(ejes, niveles):
        ruta = RAIZ / 'salida' / f'{nivel}{sufijo}.geojson'
        if not ruta.exists():
            raise SystemExit(f'falta {ruta.name}; corra construir.py y publicar.py')
        gdf = gpd.read_file(ruta)
        capas[nivel] = gdf

        if valores is None:
            # Área en un CRS equivalente-área; nunca en grados.
            gdf['valor'] = gdf.to_crs('ESRI:102033').area / 1e6
        else:
            gdf['valor'] = gdf['ubigeo'].map(valores)

        con_dato = gdf['valor'].notna().sum()
        cortes = sorted(set(
            gdf['valor'].quantile([i / 6 for i in range(7)]).tolist()))
        norm = BoundaryNorm(cortes, len(cortes) - 1, clip=True)

        gdf.plot(column='valor', ax=eje, cmap=cmap, norm=norm,
                 linewidth=grosor, edgecolor=HAIRLINE,
                 missing_kwds={'color': '#f0efec', 'edgecolor': HAIRLINE,
                               'linewidth': grosor, 'hatch': '///'})
        eje.set_facecolor(SURFACE)
        eje.set_title(f'{titulo}  ({con_dato} de {len(gdf)})',
                      fontsize=12, color=INK, pad=10)
        eje.set_axis_off()

        n = len(cortes) - 1
        parches = [
            Patch(facecolor=RAMPA[i * len(RAMPA) // n if n < len(RAMPA) else i],
                  edgecolor=HAIRLINE, linewidth=0.4,
                  label=f'{cortes[i]:,.0f} – {cortes[i + 1]:,.0f}')
            for i in range(n)]
        leg = eje.legend(handles=parches, loc='lower left', frameon=False,
                         fontsize=8, title=etiqueta, alignment='left')
        leg.get_title().set_color(INK_2)
        leg.get_title().set_fontsize(9)
        for t in leg.get_texts():
            t.set_color(MUTED)

    encabezado = f'Perú — {etiqueta}'
    if len(niveles) > 1:
        encabezado += ' por nivel administrativo'
    fig.suptitle(encabezado, fontsize=15, color=INK, y=0.97)
    fig.text(0.5, 0.025,
             f'Fuente: límites INEI + reconstrucciones · indicador: {procedencia}'
             f' · {"resolución completa" if a.completo else "capas simplificadas"}',
             ha='center', fontsize=8.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.05, 1, 0.94))
    fig.savefig(a.salida, dpi=130, facecolor=SURFACE)
    print(rel(a.salida))

    # --- indicador para el mapa web ----------------------------------------
    medidas = {}
    for m, etq in MEDIDAS.items():
        if censo and m not in censo:
            continue
        por_nivel = {}
        for nivel, _, _ in NIVELES:
            g = capas.get(nivel)
            if g is None:
                g = gpd.read_file(RAIZ / 'salida' / f'{nivel}{sufijo}.geojson')
            if censo:
                serie = g['ubigeo'].map(censo[m])
            else:
                serie = g.to_crs('ESRI:102033').area / 1e6
            por_nivel[nivel] = {
                u: (None if v != v else round(float(v), 2))
                for u, v in zip(g['ubigeo'], serie)}
        medidas[m] = {'etiqueta': f'{etq} (Censo 2025)' if censo else etiqueta,
                      'valores': por_nivel}
        if not censo:
            break                       # sin censo hay una sola medida

    destino = RAIZ / 'ejemplos' / 'datos' / 'indicador.json'
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(
        {'procedencia': procedencia, 'medidas': medidas},
        ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
    print(rel(destino))

    # Vista de tabla: en una escala continua el color no puede ser el único
    # canal de lectura. Se emite el top-10 del nivel más fino dibujado.
    gdf = capas[niveles[-1][0]]
    top = gdf.nlargest(10, 'valor')
    print(f'\nTop 10 ({niveles[-1][0]}) por {etiqueta.lower()}:')
    for _, r in top.iterrows():
        print(f'  {r["ubigeo"]:<7} {r["nombre"][:28]:<28} '
              f'{str(r.get("nombre_departamento", ""))[:14]:<14} '
              f'{r["valor"]:>12,.1f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
