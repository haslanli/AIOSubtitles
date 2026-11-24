"""Puts the texts of the licences NOTICE.md names in the image's licence folder, beside NOTICE.md and LICENSE, and stops
the image's build unless every licence NOTICE.md's table names has its text there. A step of the build
(subsync/Dockerfile), run from the build context; it isn't part of the image."""
import importlib.metadata
import re
import shutil
import sys
import sysconfig
from pathlib import Path

FOLDER = Path('/opt/aiosubtitles/licences')
FONTS = Path('/opt/aiosubtitles/subsync/subsync/static/fonts')     # the typefaces, their OFL text beside them
DEBIAN = Path('/usr/share/common-licenses')                         # Debian's base-files
REQUIREMENTS = Path(__file__).with_name('requirements.txt')
# Each licence NOTICE.md's table may name, and words of its text (any run of spaces and line breaks reads as one space)
WORDS = {'GPL-3.0': 'GNU GENERAL PUBLIC LICENSE Version 3, 29 June 2007',
         'GPL-2.0': 'GNU GENERAL PUBLIC LICENSE Version 2, June 1991',
         'LGPL-2.1': 'GNU LESSER GENERAL PUBLIC LICENSE Version 2.1, February 1999',
         'Apache-2.0': 'Apache License Version 2.0, January 2004',
         'MIT': 'Permission is hereby granted, free of charge, to any person obtaining a copy',
         'BSD-3-Clause': 'Neither the name of',
         'SIL Open Font Licence 1.1': 'SIL OPEN FONT LICENSE Version 1.1',
         'PSF': 'PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2'}


def named(notice):
    """The licences NOTICE.md's table names. A row whose licence cell names none of WORDS stops the build: its text
    couldn't be checked."""
    names = set()
    for row in notice.splitlines():
        if row.startswith('| ') and not row.startswith('| Part '):
            cell = row.split(' | ')[1]
            found = {name for name in WORDS if re.search(rf'(?<![\w-]){re.escape(name)}(?![\w.])', cell)}
            if not found:
                sys.exit(f'NOTICE.md: no licence text is known for "{cell}"')
            names |= found
    return names


def package_texts(name, path):
    """The installed package `name` and its licence files, relative to its site-packages folder: those in its
    .dist-info's licenses folder (PEP 639), and any file named LICENSE, LICENCE or COPYING (onnxruntime's is beside its
    code, its .dist-info has none)."""
    dist = next(importlib.metadata.distributions(name=name, path=path), None)
    if dist is None:
        sys.exit(f'{name} is not installed')
    files = [file for file in dist.files or ()
             if file.parts[0].endswith('.dist-info') and file.parts[1:2] == ('licenses',)
             or re.match(r'LICEN[CS]E|COPYING', file.name, re.IGNORECASE)]
    if not files:
        sys.exit(f'{name} has no licence file')
    return dist, files


def collect(folder=FOLDER, debian=DEBIAN, fonts=FONTS, stdlib=None, path=None):
    """Copies the texts into `folder`, which holds NOTICE.md and LICENSE already: Debian's Apache-2.0, GPL-3, GPL-2 and
    LGPL-2.1, Python's own licence (`stdlib`: the standard library's folder), the typefaces' OFL and the licence files
    of requirements.txt's packages (found on `path`: the import path), under site-packages/ as they lie there. A text
    missing from the image raises; a package without a licence file, or a licence the table names without its text in
    the folder, stops the build (SystemExit)."""
    stdlib = Path(stdlib or sysconfig.get_path('stdlib'))
    for name, source in (('Apache-2.0.txt', debian / 'Apache-2.0'), ('GPL-3.0.txt', debian / 'GPL-3'),
                         ('GPL-2.0.txt', debian / 'GPL-2'), ('LGPL-2.1.txt', debian / 'LGPL-2.1'),
                         ('Python.txt', stdlib / 'LICENSE.txt'), ('OFL.txt', fonts / 'OFL.txt')):
        shutil.copyfile(source, folder / name)
    for name in re.findall(r'^([A-Za-z0-9_.-]+)==', REQUIREMENTS.read_text(encoding='utf-8'), re.M):
        dist, files = package_texts(name, sys.path if path is None else path)
        for file in files:
            target = folder / 'site-packages' / file
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(dist.locate_file(file), target)
    texts = [' '.join(file.read_text(encoding='utf-8', errors='replace').split())
             for file in folder.rglob('*') if file.is_file()]
    missing = sorted(name for name in named((folder / 'NOTICE.md').read_text(encoding='utf-8'))
                     if not any(WORDS[name] in text for text in texts))
    if missing:
        sys.exit(f'{folder} has no text for {", ".join(missing)}')


if __name__ == '__main__':
    collect()
