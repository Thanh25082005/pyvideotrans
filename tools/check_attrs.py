"""Dò mọi truy cập thuộc tính tới hằng số kênh không còn tồn tại."""
import re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from videotrans import recognition, tts, translator

mods = {'recognition': recognition, 'tts': tts, 'translator': translator}
bad = []
ROOT = Path(__file__).resolve().parent.parent
files = [p for p in ROOT.rglob('*.py')
         if 'webapp' not in p.parts and '.venv' not in p.parts and '__pycache__' not in p.parts]
for f in files:
    for i, line in enumerate(f.read_text(encoding='utf-8', errors='ignore').splitlines(), 1):
        for name, mod in mods.items():
            for m in re.finditer(rf'\b{name}\.([A-Za-z_][A-Za-z_0-9]*)', line):
                attr = m.group(1)
                if not hasattr(mod, attr):
                    bad.append(f'{f.relative_to("/home/thanh/pyvideotrans")}:{i}  {name}.{attr}')
for b in sorted(set(bad)):
    print(' ', b)
print(f'\nTổng: {len(set(bad))} truy cập tới hằng số không còn tồn tại')
