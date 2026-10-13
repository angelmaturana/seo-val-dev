#!/usr/bin/env python3
import os, re, json, html, sys, subprocess
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parent.parent
SITE_URL = 'https://seovaldemoro.com'
OUT_DEFAULT = REPO / 'dist'

MENU_LINKS = [
    ('/', 'Inicio'),
    ('/diseno-web/', 'Diseño web'),
    ('/seo-local/', 'SEO local'),
    ('/soporte-wordpress/', 'Soporte WordPress'),
    ('/precios/', 'Precios'),
    ('/sobre-nosotros/', 'Sobre nosotros'),
    ('/contacto/', 'Contacto'),
]

def parse_front_matter(text):
    if not text.startswith('---'):
        return {}, text
    m = re.match(r'^---\n(.*?)\n---\n(.*)$', text, re.DOTALL)
    if not m:
        return {}, text
    block, content = m.group(1), m.group(2)
    data = {}
    for line in block.splitlines():
        if ':' not in line:
            continue
        key, val = line.split(':', 1)
        data[key.strip()] = val.strip()
    return data, content

def read_text(path):
    return Path(path).read_text(encoding='utf-8')

def write_text(path, text):
    Path(path).write_text(text, encoding='utf-8')

def rel_url_from_page_path(page_path):
    s = str(page_path)
    if s.endswith('index.html'):
        s = s[:-len('index.html')]
    if not s:
        return '/'
    if not s.endswith('/'):
        s += '/'
    return s

def url_from_rel(rel):
    if rel == '/':
        return SITE_URL + '/'
    return SITE_URL + rel

def compute_active(rel, front):
    if 'active' in front:
        return front['active'], ''
    for href, label in MENU_LINKS:
        if rel == href:
            return href, 'page'
    for href, label in MENU_LINKS:
        if href != '/' and rel.startswith(href):
            return href, 'true'
    return '', ''

def apply_active(header_html, active_href, aria_value):
    if not active_href:
        return header_html
    def repl(m):
        attrs = m.group(1)
        hm = re.search(r'\bhref="([^"]*)"', attrs)
        if not hm or hm.group(1) != active_href:
            return m.group(0)
        cm = re.search(r'\bclass="([^"]*)"', attrs)
        if cm:
            if 'active' not in cm.group(1).split():
                attrs = attrs.replace(cm.group(0), f'class="{(cm.group(1) + " active").strip()}"', 1)
        else:
            attrs += ' class="active"'
        if 'aria-current=' not in attrs:
            attrs += f' aria-current="{aria_value}"'
        return f'<a{attrs}>'
    # Solo enlaces del <nav> principal (la clase puede ir antes o despues del href)
    def nav_repl(nm):
        return re.sub(r'<a((?:\s+[a-zA-Z-]+="[^"]*")+)>', repl, nm.group(0))
    return re.sub(r'<nav\b.*?</nav>', nav_repl, header_html, flags=re.DOTALL)

def render_geo(flag):
    if flag is True or str(flag).lower() == 'true':
        return '<meta name="geo.region" content="ES-MD">\n    <meta name="geo.placename" content="Valdemoro">'
    return ''

def render_canonical(rel, canonical_mode):
    if canonical_mode == 'none':
        return ''
    return f'<link rel="canonical" href="{url_from_rel(rel)}">'

def render_og_url(rel, canonical_mode):
    if canonical_mode == 'none':
        return ''
    return f'<meta property="og:url" content="{url_from_rel(rel)}">'

def render_jsonld(page_json, entity_nodes):
    if page_json is None:
        return ''
    graph = []
    seen = set()
    for node in entity_nodes:
        nid = node.get('@id')
        if nid:
            seen.add(nid)
        graph.append(node)
    page_graph = page_json.get('@graph', [])
    if not page_graph and page_json.get('@type'):
        page_graph = [page_json]
    for node in page_graph:
        nid = node.get('@id')
        if nid in seen:
            return ''
        graph.append(node)
    out = {'@context': 'https://schema.org', '@graph': graph}
    return '<script type="application/ld+json">\n' + json.dumps(out, ensure_ascii=False, indent=4) + '\n</script>'

def check_unrendered(html_text, path):
    m = re.search(r'\{\{[^}]+\}\}', html_text)
    if m:
        raise ValueError(f'unrendered marker {m.group(0)} in {path}')

def compose_page(front, content, layout_text, header_text, footer_text, entity_nodes, page_rel):
    title = html.escape(front.get('title', ''), quote=True)
    description = html.escape(front.get('description', ''), quote=True)
    robots = html.escape(front.get('robots', 'index, follow, max-snippet:-1, max-image-preview:large, max-video-preview:-1'), quote=True)
    geo_flag = front.get('geo', 'true')
    canonical_mode = front.get('canonical', 'auto')
    active_href, aria_value = compute_active(page_rel, front)
    header_text = apply_active(header_text, active_href, aria_value)
    geo = render_geo(geo_flag)
    canonical = render_canonical(page_rel, canonical_mode)
    og_url = render_og_url(page_rel, canonical_mode)
    page_json = None
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', content, re.DOTALL)
    if m:
        try:
            page_json = json.loads(m.group(1).strip())
        except Exception as e:
            raise ValueError(f'JSON-LD parse error: {e}')
        content = content[:m.start()] + content[m.end():]
        content = content.strip()
    jsonld = render_jsonld(page_json, entity_nodes)
    page = layout_text
    page = page.replace('{{title}}', title)
    page = page.replace('{{description}}', description)
    page = page.replace('{{robots}}', robots)
    page = page.replace('{{geo}}', geo)
    page = page.replace('{{canonical}}', canonical)
    page = page.replace('{{og_url}}', og_url)
    page = page.replace('{{jsonld}}', jsonld)
    page = page.replace('{{header}}', header_text)
    page = page.replace('{{content}}', content)
    page = page.replace('{{footer}}', footer_text)
    check_unrendered(page, page_rel)
    return page

def validate_page(page_text, rel, out_path):
    errors = []
    if '{{' in page_text:
        errors.append((out_path, 'unrendered marker'))
    return errors

BLOCK_TAGS = re.compile(
    r'<\s*(?:html|head|body|meta|link|title|header|footer|nav|main|section|article|aside|div|p|ul|ol|li|h[1-6]|address|picture|source|script|style|table|form|label|input|button|details|summary|pre|textarea)\b[^>]*>',
    re.IGNORECASE
)

def minify_css(css_text: str) -> str:
    css = css_text

    # 1) Comentarios
    css = re.sub(r'/\*[\s\S]*?\*/', '', css)

    # 2) Proteger strings para que el resto de reglas no altere su contenido
    strings = []
    def protect_string(m):
        strings.append(m.group(0))
        return f'__STR_{len(strings)-1}__'
    css = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', protect_string, css)

    # 3) Espaciado básico
    css = re.sub(r'\s+', ' ', css)
    css = css.replace(' }', '}').replace(' {', '{').replace(': ', ':').replace(' ;', ';')
    css = re.sub(r';\s*}', '}', css)

    # 4) Proteger funciones matemáticas
    placeholders = {}
    def extract_math(match):
        idx = len(placeholders)
        key = f'__MATH_{idx}__'
        func = match.group(1)
        inner = match.group(2)
        inner = re.sub(r'\s+', ' ', inner)
        inner = re.sub(r'\s*([,()])\s*', r'\1', inner)
        inner = re.sub(r'\s*([+-])\s*', r' \1 ', inner)
        inner = re.sub(r'\s{2,}', ' ', inner).strip()
        placeholders[key] = f'{func}({inner})'
        return key
    pattern = r'\b(calc|min|max|clamp)\(([^()]*(?:\([^()]*\)[^()]*)*)\)'
    while re.search(pattern, css):
        css = re.sub(pattern, extract_math, css)

    # 5) 0 con unidad -> 0
    css = re.sub(
        r'(?<![\d.])0(?:px|em|rem|vh|vw|cm|mm|in|pt|pc|ex|ch|vmin|vmax)\b',
        '0', css, flags=re.IGNORECASE
    )

    # 6) Cero a la izquierda: 0.5 -> .5
    css = re.sub(r'(?<![\w.])0+\.(?=\d)', '.', css)

    # 7) Ceros a la derecha: 1.0 -> 1, 1.50 -> 1.5, .50 -> .5
    css = re.sub(r'(\d)\.0+(?=\D|$)', r'\1', css)
    css = re.sub(r'((?:\d+)?\.\d*?)0+(?=\D|$)', r'\1', css)
    css = re.sub(r'(?<![\w.])\.(?![\d\w])', '0', css)  # seguro: '.' huérfano -> 0

    # 8) Colores hex cortos: #AABBCC -> #ABC, #AABBCCDD -> #ABCD
    css = re.sub(
        r'#([0-9a-fA-F])\1([0-9a-fA-F])\2([0-9a-fA-F])\3(?![0-9a-fA-F])',
        r'#\1\2\3', css
    )
    css = re.sub(
        r'#([0-9a-fA-F])\1([0-9a-fA-F])\2([0-9a-fA-F])\3([0-9a-fA-F])\4(?![0-9a-fA-F])',
        r'#\1\2\3\4', css
    )

    # 9) !important compacto
    css = re.sub(r'\s*!\s*important\b', '!important', css)

    # 10) Espaciado alrededor de puntuación restante
    css = re.sub(r'\s*([{}:;,>~()\[\]])\s*', r'\1', css)
    css = re.sub(r'\s*\+\s*', '+', css)

    # 11) Restaurar funciones matemáticas
    for key, val in placeholders.items():
        css = css.replace(key, val)

    # 12) Restaurar strings
    for i, s in enumerate(strings):
        css = css.replace(f'__STR_{i}__', s)

    # 13) Limpieza final
    css = re.sub(r';;+', ';', css)
    css = css.strip()
    return css

def minify_html(html_text: str) -> str:
    protected = []

    def is_ld_json(tag_text: str) -> bool:
        return bool(
            re.match(r'<script[^>]*>', tag_text, re.IGNORECASE)
            and re.search(r'type\s*=\s*["\']application/ld\+json["\']', tag_text, re.IGNORECASE)
        )

    def protect(pattern, flags=0, json_checker=None):
        def replacer(m):
            idx = len(protected)
            original = m.group(0)
            is_json = bool(json_checker and json_checker(original))
            protected.append((original, is_json))
            # \x00 no aparece en HTML normal: colisión prácticamente imposible
            return f'\x00P{idx}\x00'
        return re.sub(pattern, replacer, html_text, flags=flags)

    # 1) Proteger bloques sensibles (scripts, estilos, pre, textarea)
    html_text = protect(r'<script[^>]*>.*?</script>', re.DOTALL | re.IGNORECASE, is_ld_json)
    html_text = protect(r'<style[^>]*>.*?</style>', re.DOTALL | re.IGNORECASE)
    html_text = protect(r'<pre[^>]*>.*?</pre>', re.DOTALL | re.IGNORECASE)
    html_text = protect(r'<textarea[^>]*>.*?</textarea>', re.DOTALL | re.IGNORECASE)

    # 2) Comentarios
    html_text = re.sub(r'<!--[\s\S]*?-->', '', html_text)

    # 3) Proteger valores de atributos entre comillas ANTES de colapsar espacios
    #    Esto corrige el bug: alt="a  b", content="x   y", title="z  w" quedan intactos.
    attr_strings = []
    def protect_attr(m):
        attr_strings.append(m.group(0))
        return f'\x00A{len(attr_strings)-1}\x00'
    html_text = re.sub(r'="[^"]*"|=\'[^\']*\'', protect_attr, html_text)

    # 4) Colapsar espacios entre tags (texto visible)
    html_text = re.sub(r'>\s+<', '><', html_text)

    # 5) Colapsar espacios múltiples (ahora sí: solo espacios estructurales entre atributos)
    html_text = re.sub(r'\s{2,}', ' ', html_text)

    # 6) Limpiar espacios antes de > (sin tocar /> de autocierre)
    html_text = re.sub(r'\s+>', '>', html_text)

    html_text = html_text.strip()

    # 7) Restaurar valores de atributos
    for i, s in enumerate(attr_strings):
        html_text = html_text.replace(f'\x00A{i}\x00', s)

    # 8) Restaurar bloques protegidos (con minify de JSON-LD)
    for i, (original, is_json) in enumerate(protected):
        if is_json:
            match = re.match(r'<script[^>]*>(.*?)</script>', original, re.DOTALL | re.IGNORECASE)
            if match:
                inner = match.group(1)
                try:
                    parsed = json.loads(inner)
                    minified_inner = json.dumps(parsed, separators=(',', ':'))
                    gt_pos = original.find('>')
                    close_pos = original.rfind('</script>')
                    if gt_pos != -1 and close_pos != -1:
                        new_tag = original[:gt_pos + 1] + minified_inner + original[close_pos:]
                    else:
                        new_tag = original
                except Exception:
                    new_tag = original
            else:
                new_tag = original
            html_text = html_text.replace(f'\x00P{i}\x00', new_tag)
        else:
            html_text = html_text.replace(f'\x00P{i}\x00', original)

    return html_text

def validate_e1(pages_data):
    errors = []
    for rel, front, path in pages_data:
        if 'title' not in front or 'description' not in front:
            errors.append((path, 'missing title or description'))
    return errors

def validate_e2(page_text, path):
    errors = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', page_text, re.DOTALL):
        try:
            json.loads(m.group(1).strip())
        except Exception as e:
            errors.append((path, f'JSON-LD parse error: {e}'))
    return errors

def validate_e3(page_text, path):
    errors = []
    ids = set()
    for m in re.finditer(r'"@id"\s*:\s*"([^"]+)"', page_text):
        ids.add(m.group(1))
    for m in re.finditer(r'"@id"\s*:\s*"([^"]+)"', page_text):
        pass
    return errors

def validate_e4(page_text, path):
    errors = []
    ids = []
    for m in re.finditer(r'"@id"\s*:\s*"([^"]+)"', page_text):
        ids.append(m.group(1))
    seen = set()
    for nid in ids:
        if nid in seen:
            errors.append((path, f'duplicate @id {nid}'))
        seen.add(nid)
    return errors

def validate_e5(page_text, path):
    errors = []
    if '{{' in page_text:
        errors.append((path, 'unrendered marker'))
    return errors

def validate_e6(page_text, path, out_root):
    errors = []
    base = str(out_root)
    for m in re.finditer(r'(?:href|src)="(/[^"]+)"', page_text):
        url = m.group(1)
        if url.startswith(('mailto:', 'tel:', '#')):
            continue
        if url.startswith('http://') or url.startswith('https://'):
            continue
        if '?' in url:
            url = url.split('?')[0]
        if '#' in url:
            anchor = url.split('#')[1]
            url = url.split('#')[0]
        else:
            anchor = None
        if url.endswith('/'):
            target = base + url[1:] + 'index.html'
        else:
            target = base + url[1:] + '.html' if not url.endswith('.html') else base + url[1:]
        if not Path(target).exists():
            errors.append((path, f'broken internal link {url}'))
    return errors

def validate_e7(page_text, path):
    errors = []
    if 'noindex' not in page_text:
        h1_count = len(re.findall(r'<h1\b', page_text, re.IGNORECASE))
        if h1_count != 1:
            errors.append((path, f'expected exactly 1 h1, found {h1_count}'))
    return errors

def validate_e8(page_text, path):
    errors = []
    aria_count = len(re.findall(r'aria-current=', page_text))
    if aria_count > 1:
        errors.append((path, f'multiple aria-current ({aria_count})'))
    return errors

def validate_e9(pages_data):
    errors = []
    titles = {}
    descs = {}
    for rel, front, p in pages_data:
        if 'noindex' in front.get('robots', ''):
            continue
        title = front.get('title', '')
        desc = front.get('description', '')
        if title in titles:
            errors.append((p, f'duplicate title {title}'))
        titles[title] = p
        if desc in descs:
            errors.append((p, f'duplicate description {desc}'))
        descs[desc] = p
    return errors

def validate_e10(page_text, path):
    errors = []
    if '404' in path:
        if 'canonical' in page_text or 'og:url' in page_text:
            errors.append((path, '404 page has canonical or og:url'))
    return errors

def validate_a1(page_text, path):
    warnings = []
    if '[CONFIRMAR' in page_text or 'TODO' in page_text:
        warnings.append((path, 'contains TODO or [CONFIRMAR'))
    return warnings

def validate_a2(out_root):
    warnings = []
    sitemap = out_root / 'sitemap.xml'
    if sitemap.exists():
        sitemap_text = sitemap.read_text(encoding='utf-8')
        for m in re.finditer(r'<loc>([^<]+)</loc>', sitemap_text):
            url = m.group(1)
            rel = url.replace(SITE_URL, '')
            if rel.endswith('/'):
                target = out_root / rel[1:] / 'index.html'
            else:
                target = out_root / rel[1:]
            if not target.exists():
                warnings.append((str(sitemap.relative_to(out_root)), f'sitemap url missing: {rel}'))
    return warnings

def validate_all(pages_data, out_root):
    errors = []
    warnings = []
    for rel, front, path in pages_data:
        text = read_text(path)
        errors.extend(validate_e1([(rel, front, path)]))
        errors.extend(validate_e2(text, path))
        errors.extend(validate_e5(text, path))
        errors.extend(validate_e7(text, path))
        errors.extend(validate_e8(text, path))
        errors.extend(validate_e10(text, path))
        warnings.extend(validate_a1(text, path))
    errors.extend(validate_e9(pages_data))
    warnings.extend(validate_a2(out_root))
    return errors, warnings

def print_report(success, errors, warnings, generated, total_bytes, out, minify,
                 git_branch, git_commit, build_time,
                 total_html_before, total_html_after, css_size_before, css_size_after):
    COL = 50
    LBL = 15
    W = 2 + LBL + 2 + COL  # interior width
    BANNER = 'seovaldemoro.com  •  Static Site Generator'

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
        centered('✓  BUILD SUCCESS')
    else:
        centered('✗  BUILD FAILED')
    hr()

    centered('BUILD REPORT')
    hr()

    status = '✓  SUCCESS' if success else '✗  FAILED'
    row('Status', status)
    row('Branch', git_branch)
    row('Commit', git_commit)
    row('Build Time', build_time)
    row('Pages Generated', str(generated))
    row('Total Size', f'{total_bytes:,} bytes')
    row('Output Dir', str(out))
    row('Minify', str(minify))

    if success:
        hr()
        centered('SIZE BREAKDOWN')
        hr()
        html_saved = total_html_before - total_html_after
        html_pct = f'{(html_saved/total_html_before*100):.1f}%' if total_html_before else '0%'
        row('HTML Before', f'{total_html_before:,} bytes')
        row('HTML After', f'{total_html_after:,} bytes')
        row('HTML Saved', f'{html_saved:,} bytes ({html_pct})')
        css_saved = css_size_before - css_size_after
        css_pct = f'{(css_saved/css_size_before*100):.1f}%' if css_size_before else '0%'
        row('CSS Before', f'{css_size_before:,} bytes')
        row('CSS After', f'{css_size_after:,} bytes')
        row('CSS Saved', f'{css_saved:,} bytes ({css_pct})')
        total_before = total_html_before + css_size_before
        total_after = total_html_after + css_size_after
        total_saved = total_before - total_after
        total_pct = f'{(total_saved/total_before*100):.1f}%' if total_before else '0%'
        row('TOTAL Before', f'{total_before:,} bytes')
        row('TOTAL After', f'{total_after:,} bytes')
        row('TOTAL Saved', f'{total_saved:,} bytes ({total_pct})')

    hr()

    if warnings:
        centered('⚠  Warnings')
        for p, msg in warnings:
            line = f'• {p} → {msg}'[:60]
            print(f'║  {line:<{W-2}}║')
    elif success:
        centered('✓  No warnings')
    else:
        centered('✗  Errors')
        for p, msg in errors:
            line = f'• {p} → {msg}'[:60]
            print(f'║  {line:<{W-2}}║')

    empty()
    if success:
        centered('Site ready for deployment')
    else:
        centered('Build aborted — fix errors above')
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


def build(out_dir=None, minify=None, no_validate=False):
    git_branch, git_commit = get_git_info()
    build_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    out = Path(out_dir or OUT_DEFAULT)
    if out.exists():
        for p in out.iterdir():
            if p.is_dir():
                import shutil
                shutil.rmtree(p)
            else:
                p.unlink()
    out.mkdir(parents=True, exist_ok=True)
    static_src = REPO / 'src' / 'static'
    for p in static_src.rglob('*'):
        if p.is_file():
            target = out / p.relative_to(static_src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
    css_src = REPO / 'src' / 'css' / 'site.css'
    css_dst = out / 'css' / 'site.css'
    css_dst.parent.mkdir(parents=True, exist_ok=True)
    css_text = css_src.read_text(encoding='utf-8')
    css_size_before = len(css_text.encode('utf-8'))
    if minify:
        css_text = minify_css(css_text)
    css_size_after = len(css_text.encode('utf-8'))
    css_dst.write_text(css_text, encoding='utf-8')
    layout = read_text(REPO / 'src' / 'layout.html')
    header = read_text(REPO / 'src' / 'partials' / 'header.html')
    footer = read_text(REPO / 'src' / 'partials' / 'footer.html')
    entity = json.loads(read_text(REPO / 'src' / 'data' / 'entity.json'))
    pages = []
    for p in sorted((REPO / 'src' / 'pages').rglob('*.html')):
        pages.append(p)
    generated = 0
    errors = []
    warnings = []
    total_bytes = 0
    total_html_before = 0
    total_html_after = 0
    pages_data = []
    for page_path in pages:
        rel = rel_url_from_page_path(page_path.relative_to(REPO / 'src' / 'pages'))
        text = read_text(page_path)
        front, content = parse_front_matter(text)
        if 'title' not in front or 'description' not in front:
            errors.append((str(page_path.relative_to(REPO)), 'missing title or description'))
            continue
        page_rel = '/' + str(page_path.relative_to(REPO / 'src' / 'pages')).replace('index.html', '').replace('\\', '/').strip('/')
        if page_rel != '/':
            page_rel += '/'
        if str(page_path.relative_to(REPO / 'src' / 'pages')) == '404.html':
            page_rel = '/404.html'
        composed = compose_page(front, content, layout, header, footer, entity, page_rel)
        html_before = len(composed.encode('utf-8'))
        if minify:
            composed = minify_html(composed)
        html_after = len(composed.encode('utf-8'))
        total_html_before += html_before
        total_html_after += html_after
        out_path = out / page_path.relative_to(REPO / 'src' / 'pages')
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(out_path, composed)
        generated += 1
        total_bytes += out_path.stat().st_size
        pages_data.append((page_rel, front, str(out_path)))
    if not no_validate:
        val_errors, val_warnings = validate_all(pages_data, out)
        errors.extend(val_errors)
        warnings.extend(val_warnings)
    success = len(errors) == 0
    print_report(
        success=success,
        errors=errors,
        warnings=warnings,
        generated=generated,
        total_bytes=total_bytes,
        out=out,
        minify=minify,
        git_branch=git_branch,
        git_commit=git_commit,
        build_time=build_time,
        total_html_before=total_html_before,
        total_html_after=total_html_after,
        css_size_before=css_size_before,
        css_size_after=css_size_after,
    )

    if not success:
        raise SystemExit(1)

if __name__ == '__main__':
    out = None
    minify = None
    no_validate = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == '--out':
            i += 1
            out = args[i]
        elif args[i] == '--minify':
            i += 1
            minify = args[i].lower() != 'false'
        elif args[i] == '--no-validate':
            no_validate = True
        i += 1
    build(out_dir=out, minify=minify, no_validate=no_validate)