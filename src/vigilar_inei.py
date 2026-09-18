# -*- coding: utf-8 -*-
"""
src/vigilar_inei.py — chequeo de mantenimiento. No modifica nada.

    py -3.11 src/vigilar_inei.py

Vuelve a consultar el WFS del INEI y avisa de tres cosas:

  1. El INEI ya publica un distrito que este repo está supliendo.
     Es la señal para retirar la reconstrucción y quedarse con el polígono
     oficial: borrar la entrada de leyes/registro.yml y reconstruir.
  2. El ubigeo que asignó el INEI difiere del que predijo la regla max+1.
     El código del registro es una predicción, no una autoridad.
  3. El conteo del INEI ya no coincide con lo que espera config.yml.
     Puede ser un distrito nuevo del que aún no nos enteramos.

Sale con código != 0 si hay algo que revisar, para poder colgarlo de un cron.

Deliberadamente NO es un scraper de El Peruano y no commitea nada: un distrito
nuevo no debe entrar en los datos publicados sin que alguien mire la geometría.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

import yaml

from descargar import bajar, contar, sesion

# La raíz del repositorio: un nivel por encima de src/, donde están config.yml
# y las carpetas de datos (salida/, qa/, leyes/, fuentes/).
RAIZ = Path(__file__).resolve().parents[1]


def normalizar(s: str) -> str:
    """Para comparar nombres: sin tildes, sin dobles espacios, en mayúsculas."""
    s = unicodedata.normalize('NFKD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return ' '.join(s.upper().split())


def main() -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    cfg = yaml.safe_load((RAIZ / 'config.yml').read_text(encoding='utf-8'))
    registro = yaml.safe_load(
        (RAIZ / cfg['rutas']['leyes'] / 'registro.yml').read_text(encoding='utf-8'))
    entradas = registro.get('distritos') or []

    s = sesion()
    ows = cfg['inei']['ows']
    capa = cfg['inei']['capas']['distrito']

    n = contar(s, ows, capa)
    print(f'INEI publica hoy {n} distritos.')

    avisos = []

    # --- 3. el conteo cambió ------------------------------------------------
    esperado_inei = cfg['conteos_esperados']['distrito'] - len(entradas)
    if n != esperado_inei:
        avisos.append(
            f'el INEI pasó de {esperado_inei} a {n} distritos publicados. '
            f'config.yml espera {cfg["conteos_esperados"]["distrito"]} en total '
            f'con {len(entradas)} reconstruido(s).')

    rasgos = bajar(s, ows, capa, cfg['inei']['pagina'])
    por_ubigeo = {f['properties']['ubigeo']: f['properties'] for f in rasgos}
    por_nombre = {}
    for p in por_ubigeo.values():
        por_nombre.setdefault(normalizar(p['nombdist']), []).append(p['ubigeo'])

    # --- 1 y 2. ¿ya lo publicaron? -----------------------------------------
    for e in entradas:
        nombre = normalizar(e['nombre'])
        prov = e['provincia']
        candidatos = [u for u in por_nombre.get(nombre, []) if u[:4] == prov]

        # También por el código que predijimos, por si cambia el nombre.
        # Se recalcula igual que en src/construir.py, contra los datos de hoy.
        from ubigeo import siguiente_ubigeo
        predicho = e.get('ubigeo') or siguiente_ubigeo(prov, por_ubigeo.keys())

        if candidatos:
            oficial = candidatos[0]
            avisos.append(
                f'{e["nombre"]} (Ley {e["ley"]}) YA ESTÁ PUBLICADO por el INEI '
                f'con ubigeo {oficial}. Retire la entrada de '
                f'leyes/registro.yml y reconstruya para quedarse con el '
                f'polígono oficial.')
            if oficial != predicho:
                avisos.append(
                    f'  además, el INEI asignó {oficial} y la regla predijo '
                    f'{predicho}. Todo dato publicado con {predicho} necesita '
                    f'reasignarse.')
        elif predicho in por_ubigeo:
            avisos.append(
                f'el ubigeo {predicho}, reservado para {e["nombre"]}, lo ocupa '
                f'ahora "{por_ubigeo[predicho]["nombdist"]}" en el INEI. '
                f'Fije el código a mano en leyes/registro.yml.')
        else:
            conf = e.get('ubigeo_oficial_confirmado')
            estado = ('código confirmado en el SISCONCODE'
                      if conf == predicho else 'código provisional')
            print(f'  {e["nombre"]}: el INEI aún no publica su polígono; '
                  f'se sigue supliendo con {predicho} ({estado}).')

    # --- 4. el registro oficial de códigos (SISCONCODE) --------------------
    # Va por delante de la cartografía: el INEI asigna el ubigeo bastante antes
    # de publicar el polígono. Es la señal más temprana de que se creó un
    # distrito nuevo.
    try:
        from descargar_ubigeos import descargar, parsear
        registros = parsear(descargar(cfg, str(cfg['sisconcode']['version'])))
        oficiales = {u: n for lv, u, n in registros if lv == 'distrito'}
        print(f'\nSISCONCODE {cfg["sisconcode"]["version"]}: '
              f'{len(oficiales)} distritos oficiales.')

        esperados = cfg['ubigeos_oficiales']['distrito']
        if len(oficiales) != esperados:
            avisos.append(
                f'el registro oficial pasó de {esperados} a {len(oficiales)} '
                f'distritos. Actualice ubigeos_oficiales en config.yml.')

        declarados = set(cfg.get('sin_cartografia', {}).get('distritos', {}))
        registrados = set()
        for e in entradas:
            registrados.add(e.get('ubigeo_oficial_confirmado')
                            or e.get('ubigeo') or '')
        sin_poligono = set(oficiales) - set(por_ubigeo) - registrados
        for u in sorted(sin_poligono - declarados):
            avisos.append(
                f'{u} "{oficiales[u]}" es oficial, no tiene polígono del INEI '
                f'y no está en leyes/registro.yml ni declarado en '
                f'sin_cartografia. Consiga su ley de creación.')
        for u in sorted(declarados - sin_poligono):
            avisos.append(
                f'{u} ya no está pendiente; quítelo de sin_cartografia '
                f'en config.yml.')

        # ¿el INEI cambió el código que traíamos confirmado?
        for e in entradas:
            conf = e.get('ubigeo_oficial_confirmado')
            if conf and conf not in oficiales:
                avisos.append(
                    f'{e["nombre"]}: teníamos confirmado {conf} y el '
                    f'SISCONCODE ya no lo lista.')
    except Exception as e:
        avisos.append(f'no se pudo consultar el SISCONCODE: '
                      f'{type(e).__name__}: {e}')

    if not avisos:
        print('\nSin novedades. Nada que hacer.')
        return 0

    print('\n' + '=' * 70)
    print('HAY QUE REVISAR')
    print('=' * 70)
    for a in avisos:
        print(f'  - {a}')
    return 1


if __name__ == '__main__':
    sys.exit(main())
