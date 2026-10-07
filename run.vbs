' CRT-MEDIA // silent launcher for the Windows startup folder.
' Put a shortcut to this file in shell:startup (Win+R -> shell:startup).
' It runs the widget with pythonw.exe in a hidden window and returns at once.
'
' The widget needs the project's virtual environment. If that is missing (a
' fresh clone that has not been through setup.cmd yet) this launcher says so in
' a dialog box instead of failing silently with an obscure error.
Option Explicit

Dim fso, shell, here, pyw, app, q
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
q = Chr(34)

here = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = here & "\.venv\Scripts\pythonw.exe"
app = here & "\app.py"

If Not fso.FileExists(app) Then
  MsgBox "CRT-MEDIA launcher cannot find app.py in:" & vbCrLf & vbCrLf & _
         here & vbCrLf & vbCrLf & _
         "This folder does not look like a complete CRT-MEDIA checkout.", 16, "CRT-MEDIA"
  WScript.Quit 2
End If

If Not fso.FileExists(pyw) Then
  MsgBox "CRT-MEDIA is not set up yet on this machine." & vbCrLf & vbCrLf & _
         "Missing virtual environment:" & vbCrLf & _
         "    " & pyw & vbCrLf & vbCrLf & _
         "Double-click setup.cmd in the project folder once to create it and " & _
         "install the dependencies. When setup.cmd reports SUCCESS, run this " & _
         "launcher again." & vbCrLf & vbCrLf & _
         "Project folder:" & vbCrLf & "    " & here, 16, "CRT-MEDIA"
  WScript.Quit 3
End If

' 0 = hidden window, False = do not wait for it to exit
shell.Run q & pyw & q & " " & q & app & q, 0, False
WScript.Quit 0
