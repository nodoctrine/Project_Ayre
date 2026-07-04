# USB Prep — assembling a shippable Ayre USB drive

Audience: the **USB preparer** — someone with internet who clones this repo, adds
the large binary files, then copies the result onto a USB drive to ship to an
**offline destination machine**.

## Why this step exists (the two containers)

The git repo and the USB drive are different containers:

- **The repo** holds source and config (e.g. `config/rerankers.json`) — but
  **not** the large binaries (`llama-server` + CUDA DLLs, the `.gguf` files) **nor
  the bundled Python runtime**. The binaries exceed git's 100MB/file limit and the
  Python runtime is a platform-specific blob; all are deliberately `.gitignore`d and
  the required ones ship via the GitHub Releases page instead.
- **The USB drive** holds *everything* — it's a plain file copy of the populated
  cloned repo folder, so it runs fully offline on the destination with no
  internet, no git, and no fetching.

> **Required vs. bundled vs. the chat model (three-tier doctor).** Only the
> **engine + config** (binary + CUDA DLLs, `tiers.json`/`runtime.json`) are
> REQUIRED — Setup hard-fails without them. The **rerankers** ship in the
> download but are **non-blocking**: absent → a "RAG degraded" note, Setup still
> exits 0. The **chat model is the one thing the end user adds** and is also not
> required (Setup exits 0 with an "add a model to boot" alert). As a preparer you
> may drop a recommended model onto the drive for a ready-to-chat kit — a
> convenience, not a requirement, and the user can swap in any GGUF.

This guide is the bridge: it tells you which large files to acquire and where to
put them so that the repo folder becomes a complete, offline-ready kit before you
copy it to the stick.

> The destination machine never needs internet. All network access happens here,
> on the preparer's machine, while assembling the drive.

## What must be present (the doctor decides — there is no `manifest.json`)

There is **no `manifest.json`** — it was refactored away on 2026-06-24. The
authoritative check is the **three-tier doctor** (`python -m ayre_setup.cli check`),
which reads:
- **REQUIRED** (hardcoded): the `llama-server` binary in `bin/` + `config/tiers.json`
  + `config/runtime.json`. Missing → the kit fails.
- **BUNDLED RAG**: the reranker filenames registered in **`config/rerankers.json`**
  (the registry that replaced the old manifest). Missing → non-blocking "RAG degraded."
- **CHAT MODEL**: **any non-reranker `.gguf`** in `models/`, auto-discovered by
  content — **not by a fixed name**. Drop in whatever GGUF you want.

| Artifact | Required? | Goes in | Filename | Source of truth |
|----------|-----------|---------|----------|-----------------|
| llama.cpp server (CUDA build) + CUDA runtime DLLs | **required** | `bin/` | `llama-server.exe` (+ its `.dll`s) | hardcoded (platform layer) |
| Python runtime (Windows embeddable package, 64-bit) | **required (Windows)** | `python/` | `python.exe` (+ stdlib zip, `.pyd`s, `.dll`s) | launcher (`Start Ayre.cmd`) |
| Reranker — High/Ultra/Max | bundled (non-blocking) | `models/` | `bge-reranker-v2-m3.gguf` | `config/rerankers.json` `file` field |
| Reranker — Low/Mid | bundled (non-blocking) | `models/` | `bge-reranker-base.gguf` | `config/rerankers.json` `file` field |
| Chat model — High/Ultra recommended | optional | `models/` | any name (a Qwen3-30B-A3B Q4_K_M `.gguf`) | auto-discovered |
| Chat model — Mid recommended | optional | `models/` | any name (a Qwen3-8B Q4_K_M `.gguf`) | auto-discovered |

Reranker filenames **must match `config/rerankers.json`** (either name your download
to match, or edit the `file` field there) — that registry is how Ayre tells a
reranker apart from a chat model. Chat models need **no specific name**: any
non-reranker `.gguf` is detected. Engine + config gate Setup; rerankers and the
chat model do not. Bundling a recommended model is optional convenience.

> **Python is the one prerequisite the doctor can't report on** — because the
> doctor (`python -m ayre_setup.cli check`) is *itself* a Python program, so nothing
> runs at all if Python is missing. On a shipped drive the launcher (`Start Ayre.cmd`)
> uses the bundled `python/python.exe`; it only falls back to a system Python on a
> developer machine. So for an end-user kit, treat `python/` as required even though
> no doctor tier names it.

## Minimum drive size

Measured sizes (2026-06-23): the **product without a chat model** is the engine
(`bin/` ≈ **1.2 GB**) + both rerankers (≈ **0.9 GB**) + bundled Python
(`python/` ≈ **0.03 GB**) + code/config (< 0.05 GB) ≈ **~2.1 GB**. The chat model
dominates everything after that:

| Kit | Approx. used | Drive |
|-----|--------------|-------|
| Product only (no chat model) | ~2.1 GB | 8 GB |
| + Qwen3-8B Q4 (Mid model) | ~6.8 GB | 8 GB tight / 16 GB comfortable |
| + Qwen3-30B-A3B Q4 (High/Ultra model) | ~20 GB | 32 GB |
| + Kiwix ZIMs (user-supplied) | varies hugely | 64–128 GB |

## Steps

### 1. Get the repo
```
git clone <repo-url>
cd Project_Ayre
```
At this point `bin/` does not exist yet and `models/` contains only
`.gitignore`. That's expected.

### 2. Acquire `llama-server` → `bin/`
Download a **prebuilt Windows CUDA build** from the official llama.cpp releases
(github.com/ggml-org/llama.cpp → Releases). You need both:
- the main build zip (contains `llama-server.exe` and its DLLs), and
- the matching **CUDA runtime** (`cudart`) zip for that release.

Extract **all** the `.exe` and `.dll` files into `bin/`. (The CUDA build
won't start without the cudart DLLs alongside the exe.)

> Pin the exact release/CUDA version you ship and record it — see "Recording what
> you shipped" below. Match the CUDA build to the GPUs you expect at the
> destination.

### 3. Acquire the bundled Python runtime → `python/` (Windows)
The destination PC may not have Python. Ayre is **stdlib-only** (no pip packages,
no venv), so the official **Windows embeddable package** runs it as-is.

1. From python.org's downloads, grab **"Windows embeddable package (64-bit)"** for a
   recent 3.x (e.g. 3.12 or 3.13). It's a ~10 MB zip.
2. Unzip it into `python/` so you end up with `python/python.exe`.
3. In that folder, open `python3XX._pth` (e.g. `python312._pth`) and add **three**
   path lines so the launcher, the standalone doctor, and the RAG index-builder
   resolve their packages under the embeddable runtime (which sets `sys.path` from
   this file and does *not* add the working dir the way a normal Python does):
   ```
   ..\Ayre-UI
   ..\Ayre-Setup
   ..\Ayre-RAG
   ```
   (Leave the rest of the file as-is.) `..\Ayre-UI` makes `python -m ayre_ui` — the
   launch the kit actually uses — resolve the UI package. `..\Ayre-Setup` lets you
   run the doctor CLI directly under the bundled Python
   (`python\python.exe -m ayre_setup.cli check`, step 7). `..\Ayre-RAG` lets the user
   build the local Wikipedia index directly under the bundled Python
   (`python\python.exe -m ayre_rag ingest --dump <dump.xml.bz2>`). The end-user chat
   path doesn't strictly need the last two (the UI imports the doctor + RAG retrieve
   in-process and `server.py` adds both folders to `sys.path` at runtime), but listing
   them keeps "any Python 3 works" honest for the bundled one and makes the index-build
   command work out of the box.

No `pip install` step exists or is needed — every import in the kit is stdlib or
internal. The launcher (`Start Ayre.cmd`) prefers this bundled `python\python.exe`
and only falls back to a system Python on a developer machine.

> Pin the exact Python version you ship and record it (see "Recording what you
> shipped"). The embeddable package is 64-bit; match it to a 64-bit destination.

### 4. (Optional) Acquire chat model GGUFs → `models/`
Download the Qwen3 GGUFs (Hugging Face hosts official + community `Q4_K_M`
quantizations) and place them in `models/`. **Chat models keep whatever filename
they ship with** — Ayre auto-discovers any non-reranker `.gguf`, so no renaming is
needed (community casing like `Qwen3-30B-A3B-Q4_K_M.gguf` is fine):
```
models/Qwen3-30B-A3B-Q4_K_M.gguf
models/Qwen3-8B-Q4_K_M.gguf
```
(Just don't give a chat model the same name as a registered reranker, or it'll be
treated as the reranker and hidden from the chat-model list.)

### 5. Acquire the reranker GGUFs → `models/`
Download the BGE reranker GGUFs from Hugging Face and place them in `models/`
matching the filenames registered in `config/rerankers.json`
(`bge-reranker-v2-m3.gguf`, `bge-reranker-base.gguf`). These names matter — the
registry is how Ayre identifies a reranker vs. a chat model; rename the file or
edit its `file` field in `rerankers.json` so the two agree.

### 6. (Recommended) Verify integrity
Record a checksum of each large file so the destination can confirm nothing was
corrupted in transit:
```
# PowerShell
Get-FileHash .\models\qwen3-30b-a3b-q4_k_m.gguf -Algorithm SHA256
```
Compare against the source's published checksum where available. (A future
`rerankers.json` / Updater field could carry these `sha256` values so a presence/
integrity check can be automated — see the plan's provisioning discussion.)

### 7. Verify the kit assembles correctly (before copying)
From `Ayre-Setup/`, run the three-tier doctor — it lists the REQUIRED
artifacts (and fails the kit if any are missing), notes the non-blocking
rerankers, and separately reports any detected chat model:
```
python -m ayre_setup.cli check
```
A required-complete kit exits 0 even with no chat model (it just reports "add a
model"). If you bundled a recommended model it shows up under CHAT MODEL.
The dry-run additionally prints the exact launch command and confirms the
selected model + binary exist:
```
python -m ayre_setup.cli start --dry-run            # mid tier
python -m ayre_setup.cli start --tier low --dry-run # low tier
```
If you have a compatible GPU here, optionally do a real launch to confirm the
model actually loads:
```
python -m ayre_setup.cli start
```
(Without a seeded layer count this runs CPU-only and is slow — fine just to prove
it loads. See "Seed the layer split" below.)

### 8. Assemble the USB
The USB's contents = the **populated repo folder**. Copy it to the drive,
excluding dev-only cruft:
```
# PowerShell — copy the repo folder to drive E:\ , skipping git + python caches
robocopy . E:\Ayre /E /XD .git __pycache__
```
Run that from inside the repo folder. The drive then holds `Ayre-Setup/`, `config/`,
`models/` (with the GGUFs), `bin/` (with the exe + DLLs), etc. — a complete kit.

### 9. Offline smoke test (the honest check)
On a clean machine with its **network adapter disabled**, plug in the drive and
launch from it. If it runs a model with no internet, the kit is shippable. (This
mirrors the plan's clean-room VM test: "installs clean + offline.")

## Seed the layer split (optional but recommended)
By default the Mid tier runs CPU-only (`n_gpu_layers` unset → fallback 0). To ship
a GPU-ready kit, measure the good layer count on representative hardware and set
it in `config/tiers.json`:
```
"mid": { "offload_split_seed": { "n_gpu_layers": <measured value> } }
```
This is a config edit, not code — and it's a stopgap until the auto-detector
(Setup wrapper #1) is built.

## Recording what you shipped
Keep a note (in the destination's docs, or a `bin/VERSIONS.txt` you add) of the
exact llama.cpp release, CUDA version, model quant, and checksums you put on the
drive. Offline machines can't look this up later, so the drive should carry its
own provenance.

## What stays in git vs. what you add here

| In the repo (tracked) | You add (untracked, USB-only) |
|-----------------------|-------------------------------|
| Source code, config (incl. `rerankers.json`), this guide | `bin/*.exe`, `bin/*.dll`, `python/**` (bundled runtime), `models/*.gguf` |
