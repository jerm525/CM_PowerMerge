@echo off
REM Task Scheduler wrapper. Run as the service account with "Start in" set to the package folder.
REM Exit codes: 0 ok, 1 error, 2 validation FAIL, 3 WARN (fail_on_warn), 4 CM data not ready.
setlocal
cd /d "%~dp0.."
set PY=python
"%PY%" -m cm_power_merge --config config.toml --quiet run --load-staging
set RC=%ERRORLEVEL%
if %RC%==4 echo CM data not ready - the task's retry settings will try again.
exit /b %RC%
