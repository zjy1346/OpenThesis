!macro NSIS_HOOK_POSTINSTALL
  ; Tauri cannot copy the MSVC runtime DLLs directly on Windows. The packaging
  ; step stages them as .bin files, and the elevated installer restores their
  ; loader-visible names before the application can start.
  IfFileExists "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140.dll" 0 +2
    Delete "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140.dll"
  IfFileExists "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140.bin" 0 +2
    Rename "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140.bin" "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140.dll"

  IfFileExists "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140_1.dll" 0 +2
    Delete "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140_1.dll"
  IfFileExists "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140_1.bin" 0 +2
    Rename "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140_1.bin" "$INSTDIR\bin\openthesis-sidecar\_internal\VCRUNTIME140_1.dll"

  IfFileExists "$INSTDIR\bin\openthesis-sidecar\_internal\MSVCP140.dll" 0 +2
    Delete "$INSTDIR\bin\openthesis-sidecar\_internal\MSVCP140.dll"
  IfFileExists "$INSTDIR\bin\openthesis-sidecar\_internal\MSVCP140.bin" 0 +2
    Rename "$INSTDIR\bin\openthesis-sidecar\_internal\MSVCP140.bin" "$INSTDIR\bin\openthesis-sidecar\_internal\MSVCP140.dll"
!macroend
