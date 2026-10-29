@SETLOCAL
@IF NOT DEFINED NODE_PATH (
  @SET "NODE_PATH=C:\Users\tester\pm\pnstore\v11\links\@\fake-cli\directory\3a228e6c8c880bddbfe1a479f537f93c98a5b8501bd5f00b0c2682483012fc87\node_modules\fake-cli\node_modules;C:\Users\tester\pm\pnstore\v11\links\@\fake-cli\directory\3a228e6c8c880bddbfe1a479f537f93c98a5b8501bd5f00b0c2682483012fc87\node_modules;C:\Users\tester\pm\pnhome\global\v11\2de0-1a18976653b-e0436bb23e66faec\node_modules\.pnpm\node_modules"
) ELSE (
  @SET "NODE_PATH=C:\Users\tester\pm\pnstore\v11\links\@\fake-cli\directory\3a228e6c8c880bddbfe1a479f537f93c98a5b8501bd5f00b0c2682483012fc87\node_modules\fake-cli\node_modules;C:\Users\tester\pm\pnstore\v11\links\@\fake-cli\directory\3a228e6c8c880bddbfe1a479f537f93c98a5b8501bd5f00b0c2682483012fc87\node_modules;C:\Users\tester\pm\pnhome\global\v11\2de0-1a18976653b-e0436bb23e66faec\node_modules\.pnpm\node_modules;%NODE_PATH%"
)
@IF EXIST "%~dp0\node.exe" (
  "%~dp0\node.exe"  "%~dp0\..\..\..\..\..\..\pnstore\v11\links\@\fake-cli\directory\3a228e6c8c880bddbfe1a479f537f93c98a5b8501bd5f00b0c2682483012fc87\node_modules\fake-cli\bin\cli.js" %*
) ELSE (
  @SET PATHEXT=%PATHEXT:;.JS;=;%
  node  "%~dp0\..\..\..\..\..\..\pnstore\v11\links\@\fake-cli\directory\3a228e6c8c880bddbfe1a479f537f93c98a5b8501bd5f00b0c2682483012fc87\node_modules\fake-cli\bin\cli.js" %*
)
