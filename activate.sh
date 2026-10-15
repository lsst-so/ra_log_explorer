# Put this repo's dev venv (.venv) on the current shell: `source ./activate.sh`.
#
# Works from bash or zsh, and from inside a conda or Rubin stack shell. The
# stack's `setup` exports PYTHONPATH, which a venv does not isolate against,
# so stack packages (and their versions of pytest, playwright, ...) would
# shadow the venv's own; it is cleared here. `deactivate` restores PATH but
# not PYTHONPATH, so open a fresh shell to get the stack back.

if [ -n "${ZSH_VERSION:-}" ]; then
    _raleSelf="${(%):-%x}"
    case "${ZSH_EVAL_CONTEXT:-}" in *:file*) _raleSourced=1 ;; *) _raleSourced=0 ;; esac
else
    _raleSelf="${BASH_SOURCE[0]:-$0}"
    if [ "$_raleSelf" != "$0" ]; then _raleSourced=1; else _raleSourced=0; fi
fi

if [ "$_raleSourced" != 1 ]; then
    echo "activate.sh must be sourced, not run: source ./activate.sh" >&2
    unset _raleSelf _raleSourced
    exit 1
fi

# CDPATH= : with CDPATH set, bash's cd echoes the directory it lands in,
# which would end up in the captured path.
_raleRoot="$(CDPATH= cd -- "$(dirname -- "$_raleSelf")" && pwd)"
unset _raleSelf _raleSourced

if [ ! -f "$_raleRoot/.venv/bin/activate" ]; then
    cat >&2 <<EOF
No dev venv at $_raleRoot/.venv. Create it with Python 3.13:
    python3.13 -m venv .venv
    .venv/bin/pip install pre-commit black isort flake8 flake8-bugbear mypy mypy-coverage pytest
    .venv/bin/pip install -e '.[ui-test]'
    .venv/bin/playwright install chromium
EOF
    unset _raleRoot
    return 1
fi

if [ -n "${PYTHONPATH:-}" ]; then
    echo "Clearing PYTHONPATH (was set by the stack or conda; it would shadow the venv)." >&2
    unset PYTHONPATH
fi

. "$_raleRoot/.venv/bin/activate"
echo "ra_log_explorer venv active: $(python --version) at $_raleRoot/.venv" >&2
unset _raleRoot
