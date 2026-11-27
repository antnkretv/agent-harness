:; d="$(dirname "$0")"; [ ! -r "$d/../lib/python.sh" ] || exec bash -c '. "$1/../lib/python.sh"; shift; harness_python --exec "$@"' bash "$d" "$@"; export PYTHONUTF8=1 PYTHONIOENCODING=utf-8; case "${OS:-}" in Windows_NT) command -v py >/dev/null 2>&1 && exec py -3 "$@"; exec python "$@";; esac; exec python3 "$@"
@setlocal
@set "PYTHONUTF8=1"
@set "PYTHONIOENCODING=utf-8"
@for %%F in ("%~dp0..\settings.env" "%~dp0..\settings.defaults.env") do @if exist %%F @for /f "usebackq eol=# tokens=1,* delims==" %%A in (%%F) do @if not "%%B"=="" @if not defined %%A @set "%%A=%%B"
@if defined HARNESS_PYTHON goto configured
@where py >nul 2>&1 && goto launcher
@for /f "delims=" %%P in ('where python 2^>nul ^| findstr /v /i /l "WindowsApps"') do @(set "HARNESS_PYTHON=%%P" & goto configured)
@python %*
@exit /b %errorlevel%
:launcher
@py -3 %*
@exit /b %errorlevel%
:configured
@"%HARNESS_PYTHON%" %*
@exit /b %errorlevel%
