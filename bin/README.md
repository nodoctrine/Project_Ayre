# `bin/` — engine binaries (delivered via GitHub Releases, not git)

This folder is intentionally empty in the repo. The llama.cpp engine
(`llama-server.exe` + its CUDA DLLs) ships as a zip on the **GitHub Releases** page,
not in git — it's large, platform-specific, and regenerable.

## How to populate it

1. Download **`llama-server-win-cuda.zip`** from the Releases page.
2. Verify its SHA256 against the checksums on the Releases page.
3. Extract its contents **into this folder** with PowerShell `Expand-Archive` —
   **not** Explorer's "Extract All", which buries the files in a wrapper subfolder the
   launcher can't find:

   ```powershell
   Expand-Archive .\llama-server-win-cuda.zip -DestinationPath .\bin
   ```

`llama-server.exe` should end up directly in this folder. Everything here except this
README is gitignored.
