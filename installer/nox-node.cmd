@echo off
rem `nox node ...` for this installed copy - the version its launcher would run.
rem Example: nox-node.cmd status   /   nox-node.cmd pair "nox-node://pair?..."
rem The program's own folder as the working folder, and Python isolated (-I).
setlocal
pushd "%~dp0app"
"%~dp0runtime\python.exe" -I "%~dp0app\start_node.pyw" --cli node %*
set "code=%ERRORLEVEL%"
popd
exit /b %code%
