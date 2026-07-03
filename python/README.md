# `python/` — bundled Python runtime (delivered via GitHub Releases, not git)

This folder is intentionally empty in the repo. Ayre bundles the official **Windows
embeddable Python** here so the kit runs on a PC without a system Python. Like the
engine, it ships as a zip on the **GitHub Releases** page, not in git.

## How to populate it

1. Download **`python-win64.zip`** from the Releases page.
2. Verify its SHA256 against the checksums on the Releases page.
3. Extract its contents **into this folder** with PowerShell `Expand-Archive` —
   **not** Explorer's "Extract All" (which nests everything in a wrapper subfolder):

   ```powershell
   Expand-Archive .\python-win64.zip -DestinationPath .\Ayre-USB\python
   ```

   `python.exe` should end up directly in this folder, and `python3XX._pth` must list
   `..\Ayre-UI` and `..\Ayre-Setup` (see `Ayre-USB/USB_PREP.md` step 3).

The launcher (`Start Ayre.cmd`) prefers `python\python.exe`, then a system `python`,
then `py`. Everything here except this README is gitignored.
