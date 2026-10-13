#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v python3 >/dev/null || { echo "python3 no está disponible"; exit 1; }
python3 --version
if [ "$(printf '%s' "${CSS_MINIFY:-TRUE}" | tr '[:lower:]' '[:upper:]')" = "FALSE" ]; then
  echo "CSS_MINIFY=FALSE: compongo sin minimizar"
  python3 scripts/build.py --minify false
else
  echo "Compongo y minimizo"
  python3 scripts/build.py --minify true
fi

# Check site if CHECK_STRICT is true
CHECK_STRICT=${CHECK_STRICT:-TRUE}
if [ "$(printf '%s' "${CHECK_STRICT}" | tr '[:lower:]' '[:upper:]')" = "TRUE" ]; then
  echo "Ejecutando check_site.py..."
  python3 scripts/check_site.py --strict
fi

echo "Build completado en dist/"
