"""Bootstrap dos testes: coloca a raiz do runtime (FATIGUE_20260601) no sys.path.

Todos os módulos de teste importam este arquivo antes de importar código do
runtime. Rode a suíte a partir da raiz do 601:

    .venv/bin/python -m unittest discover -s tests -v

Origem e racional de cada teste: docs/NOTAS_TESTE_CODIGO_2026-07-01.md.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
