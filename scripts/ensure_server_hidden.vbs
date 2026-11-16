' Run ensure_server.ps1 with no console window at all.
' powershell.exe -WindowStyle Hidden still flashes a console for a moment,
' which pops up every 5 minutes when Task Scheduler runs it. wscript.exe has
' no console, and Run(..., 0) starts powershell hidden from the start.
' Usage: wscript.exe ensure_server_hidden.vbs [port]
Dim fso, here, port, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
port = "8848"
If WScript.Arguments.Count > 0 Then port = WScript.Arguments(0)
cmd = "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ _
    & here & "\ensure_server.ps1"" -Port " & port
WScript.Quit CreateObject("WScript.Shell").Run(cmd, 0, True)
