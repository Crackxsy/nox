@echo off
rem `nox node ...` for this installed copy - the version its launcher would run.
rem Example: nox-node.cmd status   /   nox-node.cmd pair "nox-node://pair?..."
"%~dp0runtime\python.exe" "%~dp0app\start_node.pyw" --cli node %*
