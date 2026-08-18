#!/usr/bin/env python3
"""Peek at EPUB3 epub:type structure (Standard Ebooks corpus) to ground the
epub:type-aware front/back classifier. Run: python3 spikes/lit-4-segmentation/peek_epubtype.py"""
import glob, posixpath, zipfile
import xml.etree.ElementTree as ET


def ln(t): return t.rsplit('}', 1)[-1].lower() if isinstance(t, str) else ''
def read(zf, n):
    try: return zf.read(n)
    except KeyError: return b''
def join(b, h): return posixpath.normpath(posixpath.join(b, h)).lstrip('/')
def etype(el):
    for k, v in el.attrib.items():
        if ln(k) == 'type': return v
    return ''
def opf_path(zf):
    for el in ET.fromstring(read(zf, 'META-INF/container.xml')).iter():
        if ln(el.tag) == 'rootfile' and el.get('full-path'): return el.get('full-path')


for path in sorted(glob.glob('books/se-*.epub')):
    print('=' * 72); print(path)
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        print('  NOT a zip (download failed?):', e); continue
    op = opf_path(zf); base = posixpath.dirname(op); root = ET.fromstring(read(zf, op))
    print('  version', root.get('version'))
    man = {}; spine = []; nav_id = None
    for el in root.iter():
        t = ln(el.tag)
        if t == 'item':
            man[el.get('id')] = {'href': join(base, el.get('href')), 'props': el.get('properties') or ''}
            if 'nav' in (el.get('properties') or '').split(): nav_id = el.get('id')
        elif t == 'itemref':
            spine.append(el.get('idref'))
    if nav_id and nav_id in man:
        navh = man[nav_id]['href']
        nr = ET.fromstring(read(zf, navh))
        for nav in nr.iter():
            if ln(nav.tag) == 'nav' and 'landmark' in etype(nav).lower():
                print('  LANDMARKS:')
                for a in nav.iter():
                    if ln(a.tag) == 'a' and a.get('href'):
                        print('    ', (etype(a) or '?').ljust(22), '->', a.get('href'))
    print('  SPINE (epub:type tokens per doc):')
    for idref in spine:
        if idref not in man: continue
        href = man[idref]['href']
        try: dr = ET.fromstring(read(zf, href))
        except ET.ParseError:
            print('    ', posixpath.basename(href), '(parse err)'); continue
        types = []
        for el in dr.iter():
            if ln(el.tag) in ('body', 'section', 'article'):
                t = etype(el)
                if t: types.extend(t.split())
            if len(types) > 4: break
        print('    ', posixpath.basename(href)[:36].ljust(36), ' '.join(types[:6]))
