Option Explicit
Dim shell, files, root, python, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
python = files.BuildPath(root, ".venv\Scripts\pythonw.exe")
If Not files.FileExists(python) Then python = "pythonw.exe"
command = Chr(34) & python & Chr(34) & " " & Chr(34) & files.BuildPath(root, "main.py") & Chr(34)
On Error Resume Next
shell.Run command, 1, False
If Err.Number <> 0 Then MsgBox "Khong tim thay Python. Hay cai Python va thu lai.", 16, "CoCAttack"
