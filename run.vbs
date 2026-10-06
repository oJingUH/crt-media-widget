' CRT-MEDIA // silent launcher for the Windows startup folder.
' Put a shortcut to this file in shell:startup (Win+R -> shell:startup).
' It runs the widget with pythonw.exe in a hidden window and returns at once.
Option Explicit

Dim fso, shell, here, pyw, app
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = here & "\.venv\Scripts\pythonw.exe"
app = here & "\app.py"

If Not fso.FileExists(pyw) Or Not fso.FileExists(app) Then
  MsgBox "CRT-MEDIA launcher cannot find the project at:" & vbCrLf & here, 16, "CRT-MEDIA"
  WScript.Quit 2
End If

' 0 = hidden window, False = do not wait for it to exit
shell.Run """" & pyw & """ """ & app & """", 0, False
WScript.Quit 0
