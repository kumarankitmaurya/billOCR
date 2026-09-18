"""Download the image-embedding model weights.

The single place that knows which model, which revision and which file —
the Dockerfile calls this at build time so the weights ship inside the
image, and it's the same call for local development.

    python scripts/fetch_model.py --dest ./models

Changing the model here is not enough on its own: the vectors already in
`product_image` came from whatever model produced them, and vectors from
two different models are not comparable. Change the matching settings in
app/config.py too, and run scripts/reembed_catalog.py. The app refuses to
serve visual search if the two disagree.
"""

import argparse
from pathlib import Path

# Pinned so a change upstream can't silently alter the vectors.
REPO_ID = "Marqo/marqo-fashionSigLIP"
REVISION = "main"  # TODO: pin to a commit sha once verified against real photos
FILES = [
    # Vision tower only — this app never embeds text, and the int8 export is
    # ~94MB against ~813MB for the full fp32 PyTorch weights.
    "onnx/vision_model_int8.onnx",
    # Preprocessing constants (224px, bicubic, mean/std 0.5) are read from
    # these rather than hardcoded, so they can't drift from the checkpoint.
    "preprocessor_config.json",
    "open_clip_config.json",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="./models", help="directory to download into")
    parser.add_argument("--revision", default=REVISION)
    args = parser.parse_args()

    from huggingface_hub import hf_hub_download

    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    for filename in FILES:
        path = hf_hub_download(
            repo_id=REPO_ID,
            filename=filename,
            revision=args.revision,
            local_dir=dest,
        )
        size_mb = Path(path).stat().st_size / 1_000_000
        print(f"{filename:<38} {size_mb:>7.1f} MB  -> {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
