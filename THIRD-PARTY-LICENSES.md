# Third-Party Licenses

**Ayre** itself is licensed under the **Apache License 2.0** (see [`LICENSE`](LICENSE)).

The components below are **not part of this git repository**. They are downloaded
from the project's **GitHub Releases page** and placed into the app folder during
setup (see [`USB_PREP.md`](USB_PREP.md)). Each is distributed under its own license,
which continues to govern that component. When you redistribute a populated copy of
Ayre (e.g. on a USB drive), keep these notices with it.

> TODO(you): when you cut a Release, copy each component's exact upstream `LICENSE`
> text into this file (or alongside the binary), and confirm the copyright years /
> holders match the specific build you ship. The texts below are the standard
> upstream licenses as of this writing.

---

## 1. llama.cpp — `llama-server` + ggml / CUDA libraries
- **Upstream:** https://github.com/ggml-org/llama.cpp
- **Installed to:** `bin/`
- **License:** MIT

```
MIT License

Copyright (c) 2023-2024 The ggml authors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 2. Python — Windows embeddable runtime
- **Upstream:** https://www.python.org/
- **Installed to:** `python/`
- **License:** Python Software Foundation (PSF) License Agreement

The full PSF License Agreement ships inside the runtime itself (see
`python/LICENSE.txt` once the runtime is placed) and is published at
https://docs.python.org/3/license.html. The PSF License is a permissive,
BSD-style license compatible with redistribution.

---

## 3. NVIDIA CUDA runtime — `cudart` and related DLLs
- **Source:** bundled with the llama.cpp CUDA build (redistributable runtime).
- **Installed to:** `bin/` (alongside `llama-server`)
- **License:** NVIDIA CUDA Toolkit End User License Agreement —
  **redistributable components** clause.
- **Terms:** https://docs.nvidia.com/cuda/eula/index.html

NVIDIA permits redistribution of the designated CUDA runtime redistributable
components (such as `cudart`) subject to the CUDA Toolkit EULA. Only those
redistributable components are shipped; the CUDA Toolkit itself is not.
