' Compatibility entry point; keep run_hidden.vbs for existing shortcuts.
Option Explicit

Dim fso, shell, target
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
target = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "run_hidden.vbs")
shell.Run """" & target & """", 0, False
