# Ayre — Public Testing Preview

> ⚠️ **Pre-release testing build — v1.5 testing phase.**
> Ayre is under active development and is **not production-ready**. Expect rough edges,
> incomplete features, and changes between updates. Please try it, break it, and tell us
> what happened — see **[Reporting issues](#reporting-issues)**.

Ayre is a self-contained, **offline-capable local-AI kit** for Windows. It runs a large
language model entirely on your own machine — no cloud, no account, no data leaving your
PC — behind a simple browser UI, and it auto-tunes itself to your GPU.

<!-- TODO(you): the intro above is accurate scaffolding. Rewrite it as your pitch. -->

## What works today

- **Local chat** with any GGUF chat model you supply, served by `llama-server`.
- **Auto-optimizer** — probes your hardware and chooses a context-window / GPU-layer
  split, with **Max Context / Balanced / Speed** presets plus a manual override.
- **Agentic tools** — the model can read, write, and edit files in a sandboxed workspace.
- **Context meter + handoff** — live token accounting and end-of-session summaries.
- Runs **fully offline** once the engine binaries and a model are in place.

## Requirements

- **Windows 10 / 11**, 64-bit.
- An **NVIDIA GPU with CUDA** (tested on RTX 3070 Ti 8 GB and RTX 5070 Ti 16 GB).
- The **engine binaries** (`llama-server` + CUDA DLLs) and the **bundled Python runtime** —
  too large for git, published on the **[Releases page](https://github.com/nodoctrine/Project_Ayre/releases)**.
  See **[USB_PREP.md](USB_PREP.md)** for how to place them.
- **One chat model** — a `*.gguf` file you add yourself; Ayre ships without one.
  Reasonable starting points: **Qwen3-8B Q4** (8 GB cards) or
  **Qwen3-30B-A3B Q4** (16 GB cards).

## Choosing a model — quantization

The **Q-number** in a GGUF filename (e.g. `…-Q4_K_M.gguf`) is its *quantization
level* — how much the model's weights were compressed. It's the single biggest
lever on the speed / quality / memory tradeoff, so it's worth understanding before
you download a 5 GB file. Ayre also shows a colored quant chip next to each model in
**Setup** — hover it for the same guidance in-app.

| Quant | Size / speed / VRAM | Quality | When to use |
|-------|--------------------|---------|-------------|
| **Q2** | smallest, fastest, lightest | can be incoherent | last resort when nothing larger fits |
| **Q3** | small, fast | noticeably compressed | simple tasks on tight hardware |
| **Q4** | balanced | close to the big quants | **the recommended default — start here** |
| **Q5** | larger, a bit slower | better than Q4 | if it fits with headroom |
| **Q6** | large, heavier | near-full | only with VRAM to spare |
| **Q8** | very large, slow | ~full precision | rarely needed for local chat |
| **F16 / BF16** | largest | uncompressed original | almost never the right pick for inference |

An **`IQ`** prefix (e.g. `IQ4_XS`) is an *imatrix* build — a smarter compression that
fits a little more quality into the same size, sometimes slightly slower on older GPUs.

**Rule of thumb:** on an 8 GB card, a **Q4** 8B model is the safe starting point; on
16 GB you can run a larger model or a higher quant. If answers feel vague or garbled,
your quant is probably too low for that model.

<!-- TODO(you): this section is accurate first-draft scaffolding. Rewrite the prose in
     your voice and trim the table if it's more than testers need. The same content
     lives in config/coaching.json (which drives the in-app chip + tooltip) — if you
     change the guidance, update both so they don't drift. -->

## Quick start

1. Clone this repo.
2. Follow **[USB_PREP.md](USB_PREP.md)** to drop the engine + Python runtime from the
   [Releases page](https://github.com/nodoctrine/Project_Ayre/releases) into `bin/` and `python/`.
3. Put a chat-model `.gguf` file in `models/`.
4. Run **`Start Ayre.cmd`**, then open **http://localhost:2500**.

## Reporting issues

Found a bug or something confusing? Please open a **[GitHub Issue](https://github.com/nodoctrine/Project_Ayre/issues)** with:

- your **GPU + VRAM** and **Windows version**,
- the **model** you loaded,
- **what you did** and **what happened** (screenshots / logs welcome).

<!-- TODO(you): add an issue template or preferred contact if you want more structure. -->

## Status & roadmap

This is the **v1.5 testing phase**: the goal is validating the optimizer, offline
operation, and the core chat/tools loop across a range of real hardware.

<!-- TODO(you): a sentence or two on what you most want testers to exercise. -->

## License

Ayre is licensed under the **[Apache License 2.0](LICENSE)** — see **[NOTICE](NOTICE)**
for attribution. Third-party components fetched from the Releases page (llama.cpp, the
Python runtime, the NVIDIA CUDA runtime) keep their own licenses — see
**[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md)**.
