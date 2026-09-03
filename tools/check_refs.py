"""Dò import trỏ tới module đã bị xoá (không cần cài PySide6/torch)."""
import ast, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
files = [p for p in ROOT.rglob('*.py')
         if '.venv' not in p.parts and 'webapp' not in p.parts and '__pycache__' not in p.parts]

def module_exists(dotted: str) -> bool:
    parts = dotted.split('.')
    base = ROOT.joinpath(*parts)
    return base.with_suffix('.py').exists() or (base / '__init__.py').exists()

broken = []
for path in files:
    try:
        tree = ast.parse(path.read_text(encoding='utf-8', errors='ignore'))
    except SyntaxError as exc:
        broken.append((path, f'SYNTAX: {exc}'))
        continue
    pkg = '.'.join(path.relative_to(ROOT).parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:                       # from .x import y
                base = pkg.rsplit('.', node.level - 1)[0] if node.level > 1 else pkg
                target = f'{base}.{node.module}' if node.module else base
            else:
                target = node.module or ''
            if target.startswith('videotrans') and not module_exists(target):
                broken.append((path, f'line {node.lineno}: from {target} import ...'))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith('videotrans') and not module_exists(alias.name):
                    broken.append((path, f'line {node.lineno}: import {alias.name}'))

if not broken:
    print('KHÔNG có import gãy')
for path, msg in broken:
    print(f'  {path.relative_to(ROOT)}  {msg}')
print(f'\nTổng: {len(broken)} tham chiếu gãy trong {len(files)} file')
