"""Turn an image into a vector with Marqo-FashionSigLIP (ONNX, CPU).

The int8 vision-only export, run through onnxruntime, rather than the
PyTorch checkpoint via open_clip: ~94MB against ~813MB, no torch wheel, and
a session that loads in a second or so. On a scale-to-zero host every cold
start pays that load, so the smaller runtime is what keeps this affordable.

Preprocessing is read from the checkpoint's own config files rather than
hardcoded, since getting it wrong doesn't throw — it just quietly produces
vectors that don't match anything.
"""

import io
import json
import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)


class ImageDecodeError(ValueError):
    """The uploaded bytes aren't an image we can read."""


class ImageEmbedder:
    """A loaded embedding model. Build one at startup and reuse it.

    Not safe to construct per request: the session holds the weights, and
    loading them is the expensive part.
    """

    def __init__(self, model_path: str, dim: int, intra_op_threads: int = 1) -> None:
        self.model_path = model_path
        self.dim = dim

        options = ort.SessionOptions()
        # Pinned rather than left to onnxruntime's default: in a container it
        # reads the host's core count, not the CPU limit, and spawns far too
        # many threads for a 1-vCPU instance.
        options.intra_op_num_threads = intra_op_threads
        options.inter_op_num_threads = 1
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL

        self._session = ort.InferenceSession(
            model_path, sess_options=options, providers=["CPUExecutionProvider"]
        )

        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if len(inputs) != 1:
            raise RuntimeError(f"expected 1 model input, found {[i.name for i in inputs]}")
        self._input_name = inputs[0].name
        self._output_name = outputs[0].name

        output_dim = outputs[0].shape[-1]
        if isinstance(output_dim, int) and output_dim != dim:
            raise RuntimeError(
                f"model at {model_path} outputs {output_dim}-dim vectors but config says {dim}; "
                "the vector column and every stored embedding assume the configured value"
            )

        # Preprocessing constants come from the checkpoint, not from here.
        config_dir = Path(model_path).resolve().parent.parent
        preprocessor = json.loads((config_dir / "preprocessor_config.json").read_text())
        self._size = (preprocessor["size"]["width"], preprocessor["size"]["height"])
        self._mean = np.array(preprocessor["image_mean"], dtype=np.float32).reshape(3, 1, 1)
        self._std = np.array(preprocessor["image_std"], dtype=np.float32).reshape(3, 1, 1)
        self._rescale = float(preprocessor["rescale_factor"])

        logger.info(
            "embedder loaded: %s (%s -> %s, %dx%d, dim=%d)",
            model_path,
            self._input_name,
            self._output_name,
            self._size[0],
            self._size[1],
            dim,
        )

    def preprocess(self, image: Image.Image) -> np.ndarray:
        """Image -> (1, 3, H, W) float32, matching the checkpoint's recipe.

        Squash-resize, not centre-crop: open_clip_config.json says
        `resize_mode: squash`, so the whole garment is kept and the aspect
        ratio is distorted, which is what the model was trained on.
        """
        image = ImageOps.exif_transpose(image).convert("RGB")
        image = image.resize(self._size, Image.BICUBIC)

        array = np.asarray(image, dtype=np.float32) * self._rescale
        array = array.transpose(2, 0, 1)  # HWC -> CHW
        array = (array - self._mean) / self._std
        return array[np.newaxis, ...]

    def embed(self, image: Image.Image) -> np.ndarray:
        """Image -> a single L2-normalised (dim,) float32 vector.

        The model does not normalise its own output, and pgvector's cosine
        operator assumes unit vectors here, so this is not optional.
        """
        outputs = self._session.run([self._output_name], {self._input_name: self.preprocess(image)})
        vector = np.asarray(outputs[0][0], dtype=np.float32)

        norm = float(np.linalg.norm(vector))
        if norm == 0.0:
            raise RuntimeError("model returned a zero vector; refusing to store it")
        return vector / norm

    def warmup(self) -> None:
        """Run one throwaway inference so the first real request isn't the slowest."""
        self.embed(Image.new("RGB", self._size, color=(127, 127, 127)))


def load_image(data: bytes, max_bytes: int, max_px: int = 1600) -> Image.Image:
    """Decode uploaded bytes into an image, with the size guards.

    Raises ImageDecodeError for anything unreadable, and ValueError if the
    upload is over the byte limit — the router turns those into 422 and 413.
    """
    if len(data) > max_bytes:
        raise ValueError(f"image is {len(data) / 1_000_000:.1f}MB, limit is {max_bytes / 1_000_000:.0f}MB")

    try:
        image = Image.open(io.BytesIO(data))
        # draft() lets the JPEG decoder downscale while reading, so a 12MP
        # phone photo never becomes a full-size bitmap in memory. Matters on
        # a 1GiB instance.
        image.draft("RGB", (max_px, max_px))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageDecodeError(f"couldn't read that as an image: {exc}") from exc

    return image
