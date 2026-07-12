' run_hidden.vbs - Local Tavern silent launcher
' Launches uvicorn in a hidden window, waits for the port, then opens the browser.
' Stop the server: stop_tavern.bat (only the PID registered by this project).
Option Explicit

Dim fso, shell, port, url, appDir
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

appDir = "C:\local-tavern"
port = "8765"
url = "http://localhost:" & port

shell.CurrentDirectory = appDir

' Launch uvicorn asynchronously in a hidden window (0=no window, False=async).
shell.Run "cmd /c python -X utf8 -m uvicorn server:app --host 127.0.0.1 --port " & port, 0, False

' Poll the port until ready (up to ~40s to cover Ollama cold load + uvicorn start).
Dim ok, waited, http
ok = False
waited = 0
On Error Resume Next
Do While waited < 40
    WScript.Sleep 1000
    waited = waited + 1
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    http.SetTimeouts 1500, 1500, 1500, 1500
    Err.Clear
    http.Open "GET", url, False
    http.Send
    If Err.Number = 0 And http.Status = 200 Then
        ok = True
        Exit Do
    End If
    Set http = Nothing
Loop
On Error GoTo 0

If ok Then
    shell.Run url

    ' Also open the new-session if there is none yet is not needed; just open root.
Else
    ' Service did not come up in 40s: warn the user.
    shell.Popup "Server not ready within 40 s." & vbCrLf & vbCrLf & _
                "Possible: Ollama not running, Python deps missing, or port taken." & vbCrLf & _
                "Run start.bat to see the error log.", 0, "Local Tavern", 48
End If
