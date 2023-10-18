import warnings

import cv2
import numpy as np
import torch
from packaging import version

try:
    import pycolmap
except ImportError:
    pycolmap = None

from .utils import Extractor


def sift_to_rootsift(x: torch.Tensor, eps=1e-6) -> torch.Tensor:
    x = torch.nn.functional.normalize(x, p=1, dim=-1, eps=eps)
    x.clip_(min=eps).sqrt_()
    return torch.nn.functional.normalize(x, p=2, dim=-1, eps=eps)


def run_opencv_sift(features: cv2.Feature2D, image: np.ndarray) -> np.ndarray:
    """
    Detect keypoints using OpenCV Detector.
    Optionally, perform description.
    Args:
        features: OpenCV based keypoints detector and descriptor
        image: Grayscale image of uint8 data type
    Returns:
        keypoints: 1D array of detected cv2.KeyPoint
        scores: 1D array of responses
        descriptors: 1D array of descriptors
    """
    detections, descriptors = features.detectAndCompute(image, None)
    points = np.array([k.pt for k in detections], dtype=np.float32)
    scores = np.array([k.response for k in detections], dtype=np.float32)
    scales = np.array([k.size for k in detections], dtype=np.float32)
    angles = np.deg2rad(np.array([k.angle for k in detections], dtype=np.float32))
    return points, scores, scales, angles, descriptors


class SIFT(Extractor):
    default_conf = {
        "rootsift": True,
        "nms_radius": None,
        "max_num_keypoints": 4096,
        "backend": "opencv",  # in {opencv, pycolmap, pycolmap_cpu, pycolmap_cuda}
        "detection_threshold": 0.0066667,  # from COLMAP
        "edge_threshold": 10,
        "first_octave": -1,  # only used by pycolmap, the default of COLMAP
        "num_octaves": 4,
    }

    preprocess_conf = {
        "resize": 1024,
        "grayscale": True,
    }

    required_data_keys = ["image"]

    def __init__(self, **conf):
        super().__init__(**conf)  # Update with default configuration.
        backend = self.conf.backend
        if backend.startswith("pycolmap"):
            if pycolmap is None:
                raise ImportError(
                    "Cannot find module pycolmap: install it with pip"
                    "or use backend=opencv."
                )
            options = {
                "peak_threshold": self.conf.detection_threshold,
                "edge_threshold": self.conf.edge_threshold,
                "first_octave": self.conf.first_octave,
                "num_octaves": self.conf.num_octaves,
                "normalization": pycolmap.Normalization.L2,  # L1_ROOT is buggy.
            }
            device = (
                "auto" if backend == "pycolmap" else backend.replace("pycolmap_", "")
            )
            if (
                backend == "pycolmap_cpu" or not pycolmap.has_cuda
            ) and pycolmap.__version__ < "0.5.0":
                warnings.warn(
                    "The pycolmap CPU SIFT is buggy in version < 0.5.0, "
                    "consider upgrading pycolmap or use the CUDA version.",
                    stacklevel=1,
                )
            else:
                options["max_num_features"] = self.conf.max_num_keypoints
            self.sift = pycolmap.Sift(options=options, device=device)
        elif backend == "opencv":
            self.sift = cv2.SIFT_create(
                contrastThreshold=self.conf.detection_threshold,
                nfeatures=self.conf.max_num_keypoints,
                edgeThreshold=self.conf.edge_threshold,
                nOctaveLayers=self.conf.num_octaves,
            )
        else:
            backends = {"opencv", "pycolmap", "pycolmap_cpu", "pycolmap_cuda"}
            raise ValueError(
                f"Unknown backend: {backend} not in " f"{{{','.join(backends)}}}."
            )

    def extract_single_image(self, image: torch.Tensor):
        image_np = image.cpu().numpy().squeeze(0)

        if self.conf.backend.startswith("pycolmap"):
            if version.parse(pycolmap.__version__) >= version.parse("0.5.0"):
                detections, descriptors = self.sift.extract(image_np)
                scores = None  # Scores are not exposed by COLMAP anymore.
            else:
                detections, scores, descriptors = self.sift.extract(image_np)
            keypoints = detections[:, :2]  # Keep only (x, y).
            scales, angles = detections[:, -2:].T
            if scores is not None and (
                self.conf.backend == "pycolmap_cpu" or not pycolmap.has_cuda
            ):
                # Set the scores as a combination of abs. response and scale.
                scores = np.abs(scores) * scales
        elif self.conf.backend == "opencv":
            # TODO: Check if opencv keypoints are already in corner convention
            keypoints, scores, scales, angles, descriptors = run_opencv_sift(
                self.sift, (image_np * 255.0).astype(np.uint8)
            )

        pred = {
            "keypoints": keypoints,
            "scales": scales,
            "oris": angles,
            "descriptors": descriptors,
        }
        pred = {k: torch.from_numpy(v) for k, v in pred.items()}
        if scores is not None:
            pred["keypoint_scores"] = scores = torch.from_numpy(scores)
            # Keep the k keypoints with highest score
            num_points = self.conf.max_num_keypoints
            if num_points is not None and len(keypoints) > num_points:
                indices = torch.topk(scores, num_points).indices
                pred = {k: v[indices] for k, v in pred.items()}

        return pred

    def forward(self, data: dict) -> dict:
        device = data["image"].device
        image = data["image"].cpu()
        pred = []
        for k in range(len(image)):
            img = image[k]
            if "image_size" in data.keys():
                # avoid extracting points in padded areas
                w, h = data["image_size"][k]
                img = img[:, :h, :w]
            p = self.extract_single_image(img)
            pred.append(p)
        pred = {k: torch.stack([p[k] for p in pred], 0).to(device) for k in pred[0]}
        if self.conf.rootsift:
            pred["descriptors"] = sift_to_rootsift(pred["descriptors"])
        return pred
