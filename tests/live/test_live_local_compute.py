"""This machine runs no GPU code: models run on the Ollama server.

Only Docling's layout models run locally, on the CPU, from a CPU-only build of
PyTorch. Its model weights are kept inside the harness folder.
"""

from __future__ import annotations

from importlib.metadata import distributions

import pytest

from rai.config import HARNESS_ROOT, MODEL_CACHE, ollama_host

pytestmark = pytest.mark.live


def test_no_cuda_packages_installed():
    found = sorted(
        d.metadata["Name"]
        for d in distributions()
        if (d.metadata["Name"] or "").lower().startswith(("nvidia-", "onnxruntime-gpu"))
    )
    assert not found, (
        f"CUDA packages are installed: {', '.join(found)}. Run `uv sync` to remove "
        "them (the CPU-only PyTorch index is set in pyproject.toml)."
    )


def test_torch_is_a_cpu_only_build():
    torch = pytest.importorskip("torch", reason="run `uv sync`")
    assert torch.version.cuda is None, f"PyTorch is a CUDA build ({torch.__version__})"
    assert getattr(torch.version, "hip", None) is None, "PyTorch is a ROCm build"
    assert not torch.cuda.is_available()


def test_docling_models_are_cached_inside_the_harness(tmp_path):
    pytest.importorskip("docling", reason="run `uv sync`")
    import os

    import pymupdf

    from rai.extract import extract

    assert os.environ.get("HF_HOME") == str(MODEL_CACHE), (
        "HF_HOME is set elsewhere in your shell; Docling's models will be cached "
        "there instead of inside the harness folder"
    )
    path = tmp_path / "one.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "A short page of text. " * 20)
    doc.save(path)
    assert extract(path, pdf_engine="docling").blocks
    assert MODEL_CACHE.is_relative_to(HARNESS_ROOT) and MODEL_CACHE.exists()


def test_ollama_is_reached_over_the_network(ollama, project):
    """Informational: confirms which Ollama the harness talks to."""
    version = ollama.get("/api/version").json().get("version", "?")
    print(f"Ollama {version} at {ollama_host(project)}")
