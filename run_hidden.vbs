' run_hidden.vbs - Local Tavern hidden launcher
Option Explicit

Dim fso, shell, processEnv, appDir, pythonw, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set processEnv = shell.Environment("PROCESS")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = fso.BuildPath(appDir, ".venv\Scripts\pythonw.exe")
If Len(Trim(processEnv("TAVERN_BASE_DIR"))) = 0 Then
    processEnv("TAVERN_BASE_DIR") = appDir
End If

If Not fso.FileExists(pythonw) Then
    shell.Popup "Project .venv was not found." & vbCrLf & _
                "Run setup.bat first.", 0, "Local Tavern", 16
    WScript.Quit 1
End If

shell.CurrentDirectory = appDir
command = """" & pythonw & """ -X utf8 -m core.launcher serve --hidden --open-browser --workers 1"
shell.Run command, 0, False
