"""Best-effort capability detection from a model id.

Used by providers that have no authoritative capability surface (llama.cpp
serves one GGUF and `/v1/models` returns just `{"id": ...}`) and as a
fallback for providers whose capability surface is silent (older Ollama
builds don't include the `capabilities` field in `/api/show`).

Returns a set of capability tags from the closed vocabulary used by
`autoconfig` to decide which non-text roles a model can fill:

    text       — every model
    vision     — image-to-text (multimodal vision-language)
    audio      — audio input (ASR / audio understanding)
    image_gen  — image generation (diffusion etc; almost never an LLM)

The match is substring-based on the lowercased id. Missing a hint is fine
— a vision-capable model that doesn't match any keyword will simply not
be assigned to the vision role automatically; the user can still bind it
manually with `/roles set vision <provider> <model>`.
"""
from __future__ import annotations


# Substrings that indicate a vision-capable model. Drawn from common
# multimodal model name conventions across HuggingFace / Ollama / llama.cpp.
_VISION_HINTS: tuple[str, ...] = (
    "llava",
    "bakllava",
    "moondream",
    "minicpm-v",
    "minicpmv",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "pixtral",
    "idefics",
    "internvl",
    "cogvlm",
    "phi-3-vision",
    "phi-3.5-vision",
    "phi-4-vision",
    "phi-4-multimodal",
    "llama-3.2-vision",
    "llama3.2-vision",
    "llama-4",            # llama 4 family is multimodal
    "smolvlm",
    "gemma-3",            # gemma 3 family is multimodal
    "gemma3",
    "mistral-small-3.1",  # 3.1+ added vision
    "molmo",
    "deepseek-vl",
    "yi-vl",
    "fuyu",
    "kosmos",
    "-vl-",
    "-vlm-",
    "vision",
    "multimodal",
    "mmproj",             # llama.cpp multimodal projector signal
)

# Substrings that indicate an audio-input-capable model.
_AUDIO_HINTS: tuple[str, ...] = (
    "whisper",
    "qwen2-audio",
    "qwen-audio",
    "audio-",
    "voxtral",
    "ultravox",
)

# Substrings that indicate an image-generation model. Rare for chat
# providers — diffusion pipelines run elsewhere — but listed for
# completeness.
_IMAGE_GEN_HINTS: tuple[str, ...] = (
    "stable-diffusion",
    "sdxl",
    "flux",
    "kandinsky",
    "playground-v",
    "dall-e",
    "imagegen",
)


def infer_capabilities_from_id(model_id: str) -> set[str]:
    """Return the capability tags inferred from substring matches on the id.

    Always includes ``"text"`` — every chat model is presumed to do text.
    """
    out: set[str] = {"text"}
    if not model_id:
        return out
    lowered = model_id.lower()
    if any(h in lowered for h in _VISION_HINTS):
        out.add("vision")
    if any(h in lowered for h in _AUDIO_HINTS):
        out.add("audio")
    if any(h in lowered for h in _IMAGE_GEN_HINTS):
        out.add("image_gen")
    return out
