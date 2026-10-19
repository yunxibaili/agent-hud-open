' Agent HUD - double-click to start silently (no console window).
Option Explicit
Dim shell, fso, here, cmd
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
' Run through cmd so the batch file resolves, with a hidden window (0).
cmd = "cmd /c """"" & here & "\start.bat"" silent"""
shell.Run cmd, 0, False
