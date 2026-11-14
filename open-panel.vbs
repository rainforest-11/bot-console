' 双击即启动面板（无控制台窗口）。
' 路径全部从本文件所在目录推导：面板放在 <AstrBot 根>\panel\ 下时，
' 优先用 <AstrBot 根>\venv\Scripts\pythonw.exe，没有就退回系统 PATH 里的 pythonw。
Option Explicit
Dim fso, sh, here, parent, pyw, script
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
parent = fso.GetParentFolderName(here)
script = fso.BuildPath(here, "server.py")

pyw = fso.BuildPath(parent, "venv\Scripts\pythonw.exe")
If Not fso.FileExists(pyw) Then
    pyw = fso.BuildPath(parent, ".venv\Scripts\pythonw.exe")
End If
If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"
End If

sh.CurrentDirectory = parent
sh.Run """" & pyw & """ """ & script & """", 0, False

WScript.Sleep 1500
sh.Run "http://127.0.0.1:8899/", 1, False
