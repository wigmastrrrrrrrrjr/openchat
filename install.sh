#!/usr/bin/env bash
# openchat installer — downloads openchat.zip, installs dependencies,
# links the `openchat` command, and starts it.
set -euo pipefail

REPO="${OPENCHAT_REPO:-wigmastrrrrrrrrjr/openchat}"
BRANCH="${OPENCHAT_BRANCH:-main}"
ZIP_URL="${OPENCHAT_ZIP_URL:-https://github.com/$REPO/releases/latest/download/openchat.zip}"
APP_DIR="${OPENCHAT_APP_DIR:-$HOME/.openchat/app}"
PYTHON="${OPENCHAT_PYTHON:-python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

say() { printf '\033[2m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say "openchat installer"
say "archive: $ZIP_URL"
say "target:  $APP_DIR"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  fail "python3 is required but was not found"
fi

say "downloading…"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$ZIP_URL" -o "$TMP/openchat.zip"
elif command -v wget >/dev/null 2>&1; then
  wget -q "$ZIP_URL" -O "$TMP/openchat.zip"
else
  "$PYTHON" - "$ZIP_URL" "$TMP/openchat.zip" <<'PY'
import sys, urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
fi

say "unzipping…"
mkdir -p "$TMP/staging"
if command -v unzip >/dev/null 2>&1; then
  unzip -q "$TMP/openchat.zip" -d "$TMP/staging"
else
  "$PYTHON" -m zipfile -e "$TMP/openchat.zip" "$TMP/staging"
fi

[ -f "$TMP/staging/main.py" ] || fail "archive has no main.py"
[ -f "$TMP/staging/openchat/cli.py" ] || fail "archive is missing the openchat package"

if [ -f "$TMP/staging/libraries.txt" ]; then
  say "checking libraries.txt…"
  while IFS= read -r raw || [ -n "$raw" ]; do
    line="${raw%%#*}"
    line="$(printf '%s' "$line" | tr -d '\r' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    module="$("$PYTHON" - "$line" <<'PY'
import sys
name = sys.argv[1]
for sep in (">=", "<=", "==", "~=", "!=", ">", "<", "[", ";", " "):
    name = name.split(sep, 1)[0]
print(name.strip().replace("-", "_"))
PY
)"
    if ! "$PYTHON" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$module') else 1)" 2>/dev/null; then
      say "installing $line…"
      "$PYTHON" -m pip install --quiet "$line" || fail "could not install $line"
    fi
  done < "$TMP/staging/libraries.txt"
fi

say "installing files…"
mkdir -p "$APP_DIR"
find "$APP_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -R "$TMP/staging"/. "$APP_DIR"/

# Drop legacy single-file installs.
rm -f "$HOME/.openchat/openchat.py" "$HOME/.openchat/openchat/openchat.py"

if [ -n "${OPENCHAT_BIN_DIR:-}" ]; then
  BIN_DIR="$OPENCHAT_BIN_DIR"
elif [ -w /usr/local/bin ]; then
  BIN_DIR=/usr/local/bin
else
  BIN_DIR="$HOME/.local/bin"
fi
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/openchat" <<LAUNCHER
#!/usr/bin/env sh
exec "\${OPENCHAT_PYTHON:-python3}" "$APP_DIR/main.py" "\$@"
LAUNCHER
chmod +x "$BIN_DIR/openchat"
say "command: $BIN_DIR/openchat"
say "installed openchat $(cat "$APP_DIR/VERSION" 2>/dev/null || echo unknown)"

# Make the command reachable in this shell if we installed to ~/.local/bin.
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) PATH="$BIN_DIR:$PATH" ;;
esac

if [ -t 0 ] && [ -z "${OPENCHAT_NO_START:-}" ] && [ -z "${CI:-}" ]; then
  say "starting openchat…"
  exec "$BIN_DIR/openchat"
fi
say "done — run: openchat"
