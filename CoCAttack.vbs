Option Explicit
Dim shell, files, root, python, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
python = files.BuildPath(root, ".venv\Scripts\pythonw.exe")
If Not files.FileExists(python) Then
    MsgBox "Chua cai dat CoCAttack. Hay chay cai_dat_thu_vien.bat mot lan, sau do mo lai CoCAttack.vbs.", 48, "CoCAttack"
    WScript.Quit 1
End If
command = Chr(34) & python & Chr(34) & " " & Chr(34) & files.BuildPath(root, "main.py") & Chr(34)
On Error Resume Next
shell.Run command, 1, False
If Err.Number <> 0 Then
    MsgBox "Khong the mo CoCAttack: " & Err.Description, 16, "CoCAttack"
    WScript.Quit 1
End If
