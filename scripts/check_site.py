#!/usr/bin/env python3
"""Validador SEO/integridad de la salida compilada (dist/).

Uso:
    python3 scripts/check_site.py                 # revisa dist/
    python3 scripts/check_site.py --dir .dev-dist # otra carpeta
    python3 scripts/check_site.py --strict        # los avisos también hacen fallar

También lo ejecuta scripts/build.py al terminar cada compilación.
Código de salida: 0 = correcto, 1 = hay errores (o avisos con --strict).
"""
import os
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
import subprocess
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
OUT_DEFAULT = REPO / 'dist'
SITE_URL = 'https://seovaldemoro.com'

# Umbrales (ajustables)
TITLE_MAX = 60
TITLE_MIN = 20
DESC_MIN = 70
DESC_MAX = 160
ALT_MAX = 125
MIN_WORDS_INDEXABLE = 250
MIN_CONTEXTUAL_INLINKS = 1  # enlaces entrantes desde el <main> de otras páginas

REQUIRED_FILES = ['index.html', '404.html', 'sitemap.xml', 'robots.txt']
RECOMMENDED_FILES = ['llms.txt']
VOID = {'meta', 'link', 'img', 'br', 'hr', 'input', 'source', 'base', 'col', 'area', 'wbr', 'embed', 'track', 'param'}

# Valores válidos de twitter:card (los 4 oficiales)
TWITTER_CARDS = {'summary', 'summary_large_image', 'app', 'player'}


# --------------------------------------------------------------------------- parsing

class PageParser(HTMLParser):
    """Extrae de una página todo lo que necesitan las reglas."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lang = None
        self.title = ''
        self.metas = {}            # name|property -> content
        self.canonical = None
        self.links = []            # (href, in_main, kind)
        self.a_meta = []           # dicts: href, target, rel, in_main
        self.resources = []        # (url, kind) para link/script/img/source
        self.ids = set()
        self.h = []                # (nivel, texto)
        self.imgs = []
        self.ld_raw = []
        self.aria_current = 0
        self.main_text = []
        self.all_text = []
        self.faq = []              # (pregunta, respuesta) visibles en <details>
        self._stack = []
        self._in_title = False
        self._in_script = None
        self._in_style = False
        self._cur_h = None
        self._in_main = 0
        self._details = []         # pila de dicts {'q': str, 'a': [..], 'in_summary': bool}

    # -- helpers
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'html':
            self.lang = a.get('lang')
        if 'id' in a:
            self.ids.add(a['id'])
        if 'aria-current' in a:
            self.aria_current += 1
        if tag == 'title':
            self._in_title = True
        elif tag == 'meta':
            key = a.get('name') or a.get('property')
            if key:
                self.metas[key] = a.get('content', '')
        elif tag == 'link':
            rel = (a.get('rel') or '').lower()
            if rel == 'canonical':
                self.canonical = a.get('href')
            if a.get('href'):
                self.resources.append((a['href'], 'link:' + rel))
        elif tag == 'script':
            if a.get('type') == 'application/ld+json':
                self._in_script = 'ld'
                self.ld_raw.append('')
            else:
                self._in_script = 'js'
            if a.get('src'):
                self.resources.append((a['src'], 'script'))
        elif tag == 'style':
            self._in_style = True
        elif tag == 'main':
            self._in_main += 1
        elif tag == 'a':
            if a.get('href') is not None:
                self.links.append((a['href'], self._in_main > 0, 'a'))
                self.a_meta.append({
                    'href': a['href'],
                    'target': a.get('target'),
                    'rel': a.get('rel') or '',
                    'in_main': self._in_main > 0,
                })
        elif tag in ('img', 'source'):
            if tag == 'img':
                self.imgs.append(a)
            if a.get('src'):
                self.resources.append((a['src'], 'img'))
            for part in (a.get('srcset') or '').split(','):
                u = part.strip().split(' ')[0]
                if u:
                    self.resources.append((u, 'img'))
        elif re.fullmatch(r'h[1-6]', tag):
            self._cur_h = [int(tag[1]), '']
        elif tag == 'details':
            self._details.append({'q': '', 'a': [], 'in_summary': False})
        elif tag == 'summary' and self._details:
            self._details[-1]['in_summary'] = True
        if tag not in VOID:
            self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        elif tag == 'script':
            self._in_script = None
        elif tag == 'style':
            self._in_style = False
        elif tag == 'main':
            self._in_main = max(0, self._in_main - 1)
        elif re.fullmatch(r'h[1-6]', tag) and self._cur_h:
            self._cur_h[1] = re.sub(r'\s+', ' ', self._cur_h[1]).strip()
            self.h.append(tuple(self._cur_h))
            self._cur_h = None
        elif tag == 'summary' and self._details:
            self._details[-1]['in_summary'] = False
        elif tag == 'details' and self._details:
            d = self._details.pop()
            self.faq.append((re.sub(r'\s+', ' ', d['q']).strip(),
                             re.sub(r'\s+', ' ', ' '.join(d['a'])).strip()))
        if tag in self._stack:
            # cierra hasta la etiqueta (tolerante con HTML mal anidado)
            while self._stack and self._stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self._in_script == 'ld':
            self.ld_raw[-1] += data
            return
        if self._in_script or self._in_style:
            return
        if self._in_title:
            self.title += data
        if self._cur_h is not None:
            self._cur_h[1] += data
        if self._details:
            d = self._details[-1]
            if d['in_summary']:
                d['q'] += data
            else:
                d['a'].append(data)
        self.all_text.append(data)
        if self._in_main:
            self.main_text.append(data)


def parse_page(path: Path) -> PageParser:
    p = PageParser()
    p.feed(path.read_text(encoding='utf-8'))
    p.close()
    return p


def norm(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


# --------------------------------------------------------------------------- utilidades

def page_url(rel_file: str, site_url: str) -> str:
    """index.html -> /, x/index.html -> /x/, 404.html -> /404.html"""
    if rel_file.endswith('index.html'):
        rel = '/' + rel_file[:-len('index.html')]
    else:
        rel = '/' + rel_file
    return site_url + rel


def is_indexable(parser: PageParser) -> bool:
    return 'noindex' not in parser.metas.get('robots', '').lower()


def resolve_internal(url: str, out: Path, site_url: str):
    """Devuelve (ruta_archivo | None, fragmento, error | None) para una URL interna."""
    if url.startswith(site_url):
        url = url[len(site_url):] or '/'
    parts = urlsplit(url)
    path, frag = parts.path, parts.fragment
    if not path:
        return None, frag, None  # solo ancla en la misma página
    rel = path.lstrip('/')
    if path.endswith('/'):
        target = out / rel / 'index.html'
    else:
        target = out / rel
        if target.is_dir():
            return None, frag, f'falta la barra final en {path}'
    return target, frag, None


# --------------------------------------------------------------------------- reglas por página

def check_page(rel_file, p: PageParser, out: Path, site_url: str, errors, warnings):
    E = lambda msg: errors.append((rel_file, msg))
    W = lambda msg: warnings.append((rel_file, msg))
    is404 = rel_file == '404.html'
    indexable = is_indexable(p)
    url = page_url(rel_file, site_url)

    # <head> básico
    if not p.lang:
        E('<html> sin atributo lang')
    elif not re.fullmatch(r'[a-z]{2,3}(-[A-Za-z0-9]{2,8})*', p.lang):
        W(f'valor de lang poco habitual: {p.lang!r}')
    if 'viewport' not in p.metas:
        E('falta <meta name="viewport">')
    else:
        vp = p.metas['viewport'].lower().replace(' ', '')
        if 'width=device-width' not in vp:
            W('viewport sin width=device-width')
    title = norm(p.title)
    desc = norm(p.metas.get('description', ''))
    if not title:
        E('falta <title>')
    elif indexable:
        if len(title) > TITLE_MAX:
            W(f'título de {len(title)} caracteres (> {TITLE_MAX})')
        if len(title) < TITLE_MIN:
            W(f'título muy corto ({len(title)} caracteres)')
    if not desc:
        E('falta meta description')
    elif indexable:
        if len(desc) > DESC_MAX:
            W(f'description de {len(desc)} caracteres (> {DESC_MAX})')
        if len(desc) < DESC_MIN:
            W(f'description muy corta ({len(desc)} caracteres)')
    if 'robots' not in p.metas:
        E('falta <meta name="robots">')

    # canonical
    if is404:
        if p.canonical or 'og:url' in p.metas:
            E('la 404 no debe tener canonical ni og:url')
    elif indexable:
        if not p.canonical:
            E('página indexable sin canonical')
        elif p.canonical != url:
            E(f'canonical {p.canonical} no coincide con la URL esperada {url}')
    if p.canonical and not p.canonical.startswith('https://'):
        E(f'canonical sin HTTPS: {p.canonical}')
    if p.canonical and p.metas.get('og:url') and p.metas['og:url'] != p.canonical:
        E('og:url distinto del canonical')

    # Open Graph / Twitter
    common_og = (
        'og:title', 'og:description', 'og:image', 'og:type', 'og:site_name', 'og:locale',
        'twitter:card', 'twitter:title', 'twitter:description', 'twitter:image',
    )
    for key in common_og:
        if key not in p.metas:
            W(f'falta {key}')
    if not is404 and 'og:url' not in p.metas:
        W('falta og:url')
    if p.metas.get('og:title') and norm(p.metas['og:title']) != title:
        W('og:title distinto del <title>')
    if 'og:locale' in p.metas and not re.fullmatch(r'[a-z]{2,3}_[A-Z]{2}', p.metas['og:locale']):
        W(f'og:locale con formato no habitual: {p.metas["og:locale"]}')
    if 'twitter:card' in p.metas and p.metas['twitter:card'] not in TWITTER_CARDS:
        W(f'twitter:card no reconocido: {p.metas["twitter:card"]}')
    og_img = p.metas.get('og:image', '')
    if og_img and not og_img.startswith('https://'):
        E(f'og:image sin HTTPS: {og_img}')
    if og_img and 'og:image:alt' not in p.metas:
        W('og:image sin og:image:alt')
    og_url = p.metas.get('og:url', '')
    if og_url and not og_url.startswith('https://'):
        E(f'og:url sin HTTPS: {og_url}')
    if og_img.startswith(site_url):
        t, _, _ = resolve_internal(og_img, out, site_url)
        if t is not None and not t.exists():
            E(f'og:image no existe en la salida: {og_img}')

    # encabezados
    h1s = [t for lvl, t in p.h if lvl == 1]
    if len(h1s) != 1:
        E(f'se esperaba 1 <h1> y hay {len(h1s)}')
    if p.h and p.h[0][0] != 1:
        W(f'el primer encabezado es h{p.h[0][0]} ("{p.h[0][1][:40]}"), se esperaba h1')
    prev = 0
    for lvl, t in p.h:
        if prev and lvl > prev + 1:
            W(f'salto de encabezado h{prev} → h{lvl} ("{t[:40]}")')
        prev = lvl
    for lvl, t in p.h:
        if not t:
            E(f'encabezado h{lvl} vacío')

    # imágenes
    for img in p.imgs:
        src = img.get('src', '?')
        if not img.get('src') and not img.get('srcset'):
            W('<img> sin src ni srcset')
        if 'alt' not in img:
            E(f'<img> sin alt: {src}')
        elif len(img['alt']) > ALT_MAX:
            W(f'alt de {len(img["alt"])} caracteres (> {ALT_MAX}): {src}')
        if not img.get('width') or not img.get('height'):
            W(f'<img> sin width/height (riesgo de CLS): {src}')

    # enlaces con target="_blank" deben llevar rel="noopener"/"noreferrer"
    for meta in p.a_meta:
        if meta['target'] == '_blank':
            rel = (meta['rel'] or '').lower()
            if 'noopener' not in rel and 'noreferrer' not in rel:
                W(f'<a target="_blank"> sin rel="noopener"/"noreferrer": {meta["href"][:60]}')

    # aria-current
    if p.aria_current > 1:
        E(f'{p.aria_current} elementos con aria-current')

    # restos de plantilla / marcadores
    text = ' '.join(p.all_text)
    if '{{' in text or '{{' in str(p.canonical or '') or '{{' in title:
        E('marcador {{...}} sin renderizar')
    if re.search(r'\bTODO\b|\[CONFIRMAR', text):
        W('contiene TODO o [CONFIRMAR')

    # contenido
    if indexable and not is404:
        words = len(norm(' '.join(p.main_text)).split())
        if words < MIN_WORDS_INDEXABLE:
            W(f'poco contenido: {words} palabras en <main> (< {MIN_WORDS_INDEXABLE})')

    check_jsonld(rel_file, p, url, title, indexable, is404, errors, warnings)
    check_faq_matches_schema(rel_file, p, warnings, errors)


def walk_nodes(obj):
    """Itera todos los dicts anidados de una estructura JSON."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_nodes(v)


def check_jsonld(rel_file, p, url, title, indexable, is404, errors, warnings):
    E = lambda msg: errors.append((rel_file, msg))
    W = lambda msg: warnings.append((rel_file, msg))
    if not p.ld_raw:
        if indexable and not is404:
            E('página indexable sin JSON-LD')
        return
    defined, refs, graph_nodes = {}, [], []
    for raw in p.ld_raw:
        try:
            data = json.loads(raw)
        except Exception as e:
            E(f'JSON-LD no válido: {e}')
            continue
        if data.get('@context') not in ('https://schema.org', 'https://schema.org/', 'http://schema.org', 'http://schema.org/'):
            E('JSON-LD sin @context de schema.org')
        nodes = data.get('@graph') or [data]
        for n in nodes:
            graph_nodes.append(n)
            nid = n.get('@id')
            if nid:
                if nid in defined:
                    E(f'@id duplicado en la página: {nid}')
                defined[nid] = n
        for sub in walk_nodes(nodes):
            # una referencia es un dict que solo contiene @id
            if set(sub.keys()) == {'@id'}:
                refs.append(sub['@id'])
    for r in refs:
        if r not in defined:
            E(f'referencia @id sin definir en la página: {r}')
    if indexable:
        types = {n.get('@type') for n in graph_nodes}
        for needed in ('ProfessionalService', 'WebSite'):
            if needed not in types:
                E(f'falta el nodo {needed} en el JSON-LD')
    for n in graph_nodes:
        t = n.get('@type')
        if t in ('WebPage', 'CollectionPage', 'AboutPage', 'ContactPage'):
            if n.get('url') != url:
                E(f'{t}.url ({n.get("url")}) distinto de la URL de la página ({url})')
            if n.get('name') and norm(n['name']) != title:
                W(f'{t}.name distinto del <title>')
            dm = n.get('dateModified')
            if dm and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', dm):
                W(f'dateModified con formato no ISO: {dm}')
        if t == 'FAQPage':
            if not n.get('mainEntity'):
                E('FAQPage sin mainEntity')
            else:
                for q in n.get('mainEntity', []):
                    if q.get('@type') != 'Question':
                        W(f'FAQPage.mainEntity con @type {q.get("@type")!r}, se esperaba "Question"')
                    ans = q.get('acceptedAnswer') or {}
                    if ans.get('@type') != 'Answer':
                        W(f'FAQPage Question "{norm(q.get("name", ""))[:40]}" sin acceptedAnswer @type="Answer"')


def check_faq_matches_schema(rel_file, p, warnings, errors):
    """Las FAQ del JSON-LD deben coincidir con las preguntas visibles."""
    schema_faq = []
    for raw in p.ld_raw:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for n in data.get('@graph') or [data]:
            if n.get('@type') == 'FAQPage':
                for q in n.get('mainEntity', []):
                    schema_faq.append((norm(q.get('name', '')),
                                       norm(q.get('acceptedAnswer', {}).get('text', ''))))
    if not schema_faq:
        return
    visible = {q: a for q, a in p.faq}
    for q, a in schema_faq:
        if q not in visible:
            errors.append((rel_file, f'FAQ del JSON-LD no visible en la página: "{q[:60]}"'))
        elif visible[q] != a:
            errors.append((rel_file, f'respuesta FAQ distinta entre JSON-LD y texto visible: "{q[:60]}"'))


# --------------------------------------------------------------------------- reglas del sitio

def check_links(pages, out: Path, site_url: str, errors, warnings):
    """Enlaces y recursos internos: existencia de archivo y de ancla."""
    inlinks = {rel: set() for rel in pages}
    for rel_file, p in pages.items():
        E = lambda msg, r=rel_file: errors.append((r, msg))
        W = lambda msg, r=rel_file: warnings.append((r, msg))
        for href, in_main, _ in p.links:
            href = href.strip()
            if not href:
                E('enlace con href vacío')
                continue
            if href.startswith('mailto:'):
                if not re.fullmatch(r'mailto:[^@\s]+@[^@\s]+\.[^@\s?]+(\?.*)?', href):
                    E(f'mailto mal formado: {href[:60]}')
                continue
            if href.startswith('tel:'):
                if not re.fullmatch(r'tel:\+?[\d\s().-]{6,}', href):
                    E(f'tel mal formado: {href}')
                continue
            if href.startswith(('http://', 'https://')) and not href.startswith(site_url):
                if href.startswith('http://'):
                    W(f'enlace externo sin HTTPS: {href[:60]}')
                continue
            if href.startswith('http://' + site_url.split('://', 1)[1]):
                E(f'enlace interno con http en lugar de https: {href}')
                continue
            if href.startswith('//'):
                continue
            if href.startswith('#'):
                frag = href[1:]
                if frag and frag not in p.ids:
                    E(f'ancla inexistente en la propia página: {href}')
                continue
            if not (href.startswith('/') or href.startswith(site_url)):
                W(f'enlace relativo (mejor ruta absoluta): {href[:60]}')
                continue
            target, frag, err = resolve_internal(href, out, site_url)
            if err:
                E(err)
                continue
            if target is None or not target.exists():
                E(f'enlace interno roto: {href}')
                continue
            if frag:
                key = str(target.relative_to(out))
                tp = pages.get(key)
                if tp is not None and frag not in tp.ids:
                    E(f'ancla inexistente: {href}')
            key = str(target.relative_to(out)) if target.exists() else None
            if key in inlinks and key != rel_file and in_main:
                inlinks[key].add(rel_file)
        for url, kind in p.resources:
            if url.startswith(('data:', 'http://', 'https://')) and not url.startswith(site_url):
                continue
            if kind.startswith('link:') and kind not in ('link:stylesheet', 'link:icon', 'link:apple-touch-icon', 'link:preload'):
                # alternate, describedby, canonical, sitemap... se comprueban igual si son internos
                pass
            target, _, err = resolve_internal(url, out, site_url)
            if err:
                continue
            if target is not None and not target.exists():
                E(f'recurso inexistente ({kind}): {url}')
    # huérfanas: indexables sin enlaces contextuales entrantes
    for rel_file, p in pages.items():
        if rel_file in ('404.html', 'index.html') or not is_indexable(p):
            continue
        if len(inlinks[rel_file]) < MIN_CONTEXTUAL_INLINKS:
            warnings.append((rel_file, 'sin enlaces contextuales entrantes desde otras páginas (solo menú/pie)'))


def check_duplicates(pages, errors):
    seen_t, seen_d = {}, {}
    for rel_file, p in sorted(pages.items()):
        if not is_indexable(p) or rel_file == '404.html':
            continue
        t, d = norm(p.title), norm(p.metas.get('description', ''))
        if t in seen_t:
            errors.append((rel_file, f'título duplicado con {seen_t[t]}'))
        seen_t[t] = rel_file
        if d in seen_d:
            errors.append((rel_file, f'description duplicada con {seen_d[d]}'))
        seen_d[d] = rel_file


def check_sitemap(pages, out: Path, site_url: str, errors, warnings):
    sm = out / 'sitemap.xml'
    if not sm.exists():
        return
    try:
        root = ET.fromstring(sm.read_text(encoding='utf-8'))
    except ET.ParseError as e:
        errors.append(('sitemap.xml', f'XML no válido: {e}'))
        return
    ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    in_sitemap, lastmods = {}, {}
    for u in root.findall('s:url', ns):
        loc = (u.findtext('s:loc', default='', namespaces=ns) or '').strip()
        lm = (u.findtext('s:lastmod', default='', namespaces=ns) or '').strip()
        if loc in in_sitemap:
            errors.append(('sitemap.xml', f'URL duplicada: {loc}'))
        in_sitemap[loc] = True
        lastmods[loc] = lm
        if not loc.startswith(site_url + '/'):
            errors.append(('sitemap.xml', f'URL fuera del dominio: {loc}'))
            continue
        target, _, err = resolve_internal(loc, out, site_url)
        if err or target is None or not target.exists():
            errors.append(('sitemap.xml', f'URL del sitemap sin página generada: {loc}'))
        if lm and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', lm):
            warnings.append(('sitemap.xml', f'lastmod con formato no ISO: {loc} → {lm}'))
    indexable = {page_url(rel, site_url): p for rel, p in pages.items()
                 if is_indexable(p) and rel != '404.html'}
    for u in sorted(set(indexable) - set(in_sitemap)):
        errors.append(('sitemap.xml', f'página indexable que falta en el sitemap: {u}'))
    for u in sorted(set(in_sitemap) - set(indexable)):
        if u.startswith(site_url + '/'):
            errors.append(('sitemap.xml', f'el sitemap incluye una página noindex o inexistente: {u}'))
    # coherencia lastmod ↔ dateModified del JSON-LD
    for u, p in indexable.items():
        for raw in p.ld_raw:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            for n in data.get('@graph') or [data]:
                dm = n.get('dateModified')
                if dm and lastmods.get(u) and dm != lastmods[u]:
                    warnings.append(('sitemap.xml', f'lastmod {lastmods[u]} ≠ dateModified {dm} en {u}'))


def check_robots(out: Path, site_url: str, errors, warnings):
    rb = out / 'robots.txt'
    if not rb.exists():
        return
    text = rb.read_text(encoding='utf-8')
    if not re.search(r'^\s*Sitemap:\s*' + re.escape(site_url) + r'/sitemap\.xml\s*$', text, re.M | re.I):
        warnings.append(('robots.txt', f'no declara "Sitemap: {site_url}/sitemap.xml"'))
    current_all = False
    for line in text.splitlines():
        line = line.split('#')[0].strip()
        if re.match(r'user-agent:\s*\*$', line, re.I):
            current_all = True
        elif re.match(r'user-agent:', line, re.I):
            current_all = False
        elif current_all and re.fullmatch(r'disallow:\s*/', line, re.I):
            errors.append(('robots.txt', 'Disallow: / para todos los rastreadores (bloquea el sitio entero)'))


# --------------------------------------------------------------------------- API

def run_checks(out_dir, site_url: str = SITE_URL):
    """Devuelve (errores, avisos): listas de tuplas (archivo, mensaje)."""
    out = Path(out_dir)
    errors, warnings = [], []
    if not out.is_dir():
        return [(str(out), 'la carpeta de salida no existe: compila primero')], []
    files = sorted(out.rglob('*.html'))
    if not files:
        return [(str(out), 'no hay ninguna página .html: la compilación está vacía')], []
    for req in REQUIRED_FILES:
        if not (out / req).exists():
            errors.append((req, 'archivo obligatorio ausente'))
    for rec in RECOMMENDED_FILES:
        if not (out / rec).exists():
            warnings.append((rec, 'archivo recomendado ausente'))
    pages = {}
    for f in files:
        rel = str(f.relative_to(out)).replace('\\', '/')
        try:
            pages[rel] = parse_page(f)
        except Exception as e:
            errors.append((rel, f'no se pudo leer/parsear: {e}'))
    for rel, p in pages.items():
        check_page(rel, p, out, site_url, errors, warnings)
    check_duplicates(pages, errors)
    check_links(pages, out, site_url, errors, warnings)
    check_sitemap(pages, out, site_url, errors, warnings)
    check_robots(out, site_url, errors, warnings)
    return errors, warnings


def print_results(errors, warnings, n_pages_hint=None):
    def group(items):
        g = {}
        for f, m in items:
            g.setdefault(f, []).append(m)
        return g
    for label, items, tag in (('ERRORES', errors, 'ERROR'), ('AVISOS', warnings, 'AVISO')):
        if not items:
            continue
        print(f'\n{label} ({len(items)})')
        for f, msgs in sorted(group(items).items()):
            print(f'  {f}')
            for m in msgs:
                print(f'    [{tag}] {m}')


def print_check_report(success, errors, warnings, n_pages, check_time, git_branch, git_commit, out_dir, strict):
    COL = 50
    LBL = 15
    W = 2 + LBL + 2 + COL  # interior width
    BANNER = 'seovaldemoro.com  •  Site Checker'

    def hr(): print('╠' + '═' * W + '╣')
    def row(label, value): print(f'║  {label:<{LBL}}: {str(value):<{COL}}║')
    def centered(text): print(f'║{text.center(W)}║')
    def empty(): print(f'║{" " * W}║')

    print('╔' + '═' * W + '╗')
    empty()
    centered(BANNER)
    empty()
    hr()

    if success:
        centered('✓  CHECK PASSED')
    else:
        centered('✗  CHECK FAILED')
    hr()

    centered('CHECK REPORT')
    hr()

    status = '✓  PASSED' if success else '✗  FAILED'
    row('Status', status)
    row('Branch', git_branch)
    row('Commit', git_commit)
    row('Check Time', check_time)
    row('Pages Checked', str(n_pages))
    row('Output Dir', str(out_dir))
    row('Strict Mode', str(strict))

    if success:
        hr()
        centered('✓  No issues')
    else:
        hr()
        if errors:
            centered('✗  Errors')
            for f, msg in errors:
                line = f'• {f} → {msg}'[:60]
                print(f'║  {line:<{W-2}}║')
        if warnings and strict:
            centered('⚠  Warnings (treated as errors)')
            for f, msg in warnings:
                line = f'• {f} → {msg}'[:60]
                print(f'║  {line:<{W-2}}║')
        elif warnings and not strict:
            centered('⚠  Warnings')
            for f, msg in warnings:
                line = f'• {f} → {msg}'[:60]
                print(f'║  {line:<{W-2}}║')

    empty()
    if success:
        centered('Site checks passed')
    else:
        centered('Site checks failed — fix issues above')
    empty()
    print('╚' + '═' * W + '╝')


def get_git_info():
    git_branch = os.environ.get('RENDER_GIT_BRANCH')
    git_commit = os.environ.get('RENDER_GIT_COMMIT')
    if not git_branch:
        try:
            git_branch = subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=REPO, text=True).strip()
            if git_branch == 'HEAD':
                git_branch = None
        except Exception:
            pass
    if not git_branch:
        git_branch = 'unknown'
    if not git_commit:
        try:
            git_commit = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=REPO, text=True).strip()
        except Exception:
            git_commit = 'unknown'
    else:
        git_commit = git_commit[:7]
    return git_branch, git_commit


def main(argv=None):
    ap = argparse.ArgumentParser(description='Validador SEO de la salida compilada.')
    ap.add_argument('--dir', default=str(OUT_DEFAULT), help='carpeta compilada (por defecto dist/)')
    ap.add_argument('--site-url', default=SITE_URL)
    ap.add_argument('--strict', action='store_true', help='los avisos también hacen fallar')
    args = ap.parse_args(argv)

    out = Path(args.dir)
    errors, warnings = run_checks(out, args.site_url)
    n_pages = len(list(out.rglob('*.html'))) if out.is_dir() else 0
    success = len(errors) == 0 and (not args.strict or len(warnings) == 0)
    check_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    git_branch, git_commit = get_git_info()

    print_check_report(success, errors, warnings, n_pages, check_time, git_branch, git_commit, out, args.strict)
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())