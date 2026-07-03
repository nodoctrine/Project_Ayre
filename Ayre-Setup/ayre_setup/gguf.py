"""Minimal GGUF metadata reader (stdlib only).

The optimizer needs the model's real shape -- layer count, KV-head shape, and
trained context -- to size the GPU/CPU split and the KV cache accurately for ANY
model (universal-model-support), instead of seeding Qwen3-specific constants.

We parse only the GGUF metadata header, never tensor data. Scalar key/values are
read into a dict; large ARRAY values (the tokenizer tables) are skipped by
seeking past them, and we stop as soon as the handful of keys we need are in
hand -- so this stays fast even on multi-GB files.

GGUF spec: magic 'GGUF', uint32 version, then (tensor_count, kv_count) as uint64
(uint32 in the ancient v1), then kv_count typed key/value pairs. Little-endian.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

# GGUF metadata value types
_UINT8, _INT8, _UINT16, _INT16, _UINT32, _INT32, _FLOAT32, _BOOL, \
    _STRING, _ARRAY, _UINT64, _INT64, _FLOAT64 = range(13)

_SCALAR_FMT = {
    _UINT8: "<B", _INT8: "<b", _UINT16: "<H", _INT16: "<h",
    _UINT32: "<I", _INT32: "<i", _FLOAT32: "<f", _BOOL: "<?",
    _UINT64: "<Q", _INT64: "<q", _FLOAT64: "<d",
}
_TYPE_SIZE = {
    _UINT8: 1, _INT8: 1, _BOOL: 1, _UINT16: 2, _INT16: 2,
    _UINT32: 4, _INT32: 4, _FLOAT32: 4, _UINT64: 8, _INT64: 8, _FLOAT64: 8,
}


class GGUFError(Exception):
    """Raised when a file isn't valid GGUF or a needed key is missing."""


@dataclass
class ModelInfo:
    arch: str
    n_layers: int
    n_head: int
    n_head_kv: int
    head_dim: int
    n_embd: int
    n_ctx_train: int
    per_token_kv_bytes_fp16: int       # KV bytes/token @ FP16, across all layers
    source: str = "gguf"

    def to_dict(self) -> dict:
        return {
            "arch": self.arch, "n_layers": self.n_layers, "n_head": self.n_head,
            "n_head_kv": self.n_head_kv, "head_dim": self.head_dim,
            "n_embd": self.n_embd, "n_ctx_train": self.n_ctx_train,
            "per_token_kv_bytes_fp16": self.per_token_kv_bytes_fp16,
            "source": self.source,
        }


def _read(fh, n: int) -> bytes:
    b = fh.read(n)
    if len(b) != n:
        raise GGUFError("unexpected end of file while reading metadata")
    return b


def _read_scalar(fh, vtype: int):
    if vtype == _STRING:
        (length,) = struct.unpack("<Q", _read(fh, 8))
        return _read(fh, length).decode("utf-8", "replace")
    fmt = _SCALAR_FMT.get(vtype)
    if fmt is None:
        raise GGUFError(f"unsupported scalar type {vtype}")
    return struct.unpack(fmt, _read(fh, _TYPE_SIZE[vtype]))[0]


def _skip_array(fh) -> None:
    """Seek past an ARRAY value without materializing it."""
    (elem_type,) = struct.unpack("<I", _read(fh, 4))
    (length,) = struct.unpack("<Q", _read(fh, 8))
    if elem_type == _STRING:
        for _ in range(length):
            (slen,) = struct.unpack("<Q", _read(fh, 8))
            fh.seek(slen, 1)
    elif elem_type == _ARRAY:
        for _ in range(length):           # nested arrays (rare)
            _skip_array(fh)
    else:
        size = _TYPE_SIZE.get(elem_type)
        if size is None:
            raise GGUFError(f"unsupported array element type {elem_type}")
        fh.seek(size * length, 1)


def read_metadata(path: Path) -> dict:
    """All scalar metadata key/values (arrays skipped). Stops early once the
    keys the optimizer needs are present."""
    with open(path, "rb") as fh:
        if _read(fh, 4) != b"GGUF":
            raise GGUFError("not a GGUF file (bad magic)")
        (version,) = struct.unpack("<I", _read(fh, 4))
        count_fmt = "<I" if version == 1 else "<Q"
        count_size = 4 if version == 1 else 8
        struct.unpack(count_fmt, _read(fh, count_size))           # tensor_count (unused)
        (kv_count,) = struct.unpack(count_fmt, _read(fh, count_size))

        meta: dict = {}
        for _ in range(kv_count):
            (klen,) = struct.unpack("<Q", _read(fh, 8))
            key = _read(fh, klen).decode("utf-8", "replace")
            (vtype,) = struct.unpack("<I", _read(fh, 4))
            if vtype == _ARRAY:
                _skip_array(fh)            # tokenizer tables etc. -- not needed
            else:
                meta[key] = _read_scalar(fh, vtype)
            if _have_required(meta):
                break
        return meta


def _have_required(meta: dict) -> bool:
    """All shape keys present? Includes the attention-shape keys (head_count_kv,
    key_length) so stop-early never fires BEFORE them -- otherwise KV math falls
    back to wrong MHA/derived defaults. A model that legitimately omits an
    optional key simply never stops early and gets parsed in full (still correct).
    """
    arch = meta.get("general.architecture")
    if not arch:
        return False
    needed = (f"{arch}.block_count", f"{arch}.embedding_length",
              f"{arch}.context_length", f"{arch}.attention.head_count",
              f"{arch}.attention.head_count_kv", f"{arch}.attention.key_length")
    return all(k in meta for k in needed)


def read_model_info(path: Path) -> ModelInfo:
    """Parse the GGUF and derive the shape the optimizer needs."""
    meta = read_metadata(path)
    arch = meta.get("general.architecture")
    if not arch:
        raise GGUFError("missing general.architecture")

    def need(key: str) -> int:
        if key not in meta:
            raise GGUFError(f"missing required key '{key}'")
        return int(meta[key])

    n_layers = need(f"{arch}.block_count")
    n_embd = need(f"{arch}.embedding_length")
    n_head = need(f"{arch}.attention.head_count")
    n_ctx_train = need(f"{arch}.context_length")
    # GQA: KV heads default to attention heads (MHA) when not specified.
    n_head_kv = int(meta.get(f"{arch}.attention.head_count_kv", n_head))
    # Explicit key length wins; otherwise derive from embedding / heads.
    head_dim = int(meta.get(f"{arch}.attention.key_length", n_embd // n_head if n_head else 0))

    per_token_kv = n_layers * n_head_kv * head_dim * 2 * 2  # (K+V) x fp16 bytes

    return ModelInfo(
        arch=arch, n_layers=n_layers, n_head=n_head, n_head_kv=n_head_kv,
        head_dim=head_dim, n_embd=n_embd, n_ctx_train=n_ctx_train,
        per_token_kv_bytes_fp16=per_token_kv,
    )
