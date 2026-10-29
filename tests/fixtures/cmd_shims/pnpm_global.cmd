@SETLOCAL
@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\..\global\v11\2de0-1a18976653b-e0436bb23e66faec\node_modules\fake-cli\bin\cli.js" %*
) ELSE (
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\..\global\v11\2de0-1a18976653b-e0436bb23e66faec\node_modules\fake-cli\bin\cli.js" %*
)
