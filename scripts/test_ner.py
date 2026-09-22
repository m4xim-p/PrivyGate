"""Development CLI for manually inspecting PERSON entities detected by NER."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.ner import NERDetector, NERTokenPrediction, TransformersNERBackend

DEFAULT_NER_MODEL = "LLAIMlegal/ru-legal-ner"


class CapturingNERBackend:
    """Record predictions from one delegated inference for optional dev output."""

    def __init__(self, backend: TransformersNERBackend) -> None:
        self._backend = backend
        self.predictions: list[NERTokenPrediction] = []

    def predict(self, text: str) -> list[NERTokenPrediction]:
        self.predictions = list(self._backend.predict(text))
        return self.predictions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the configured local NER detector against one text.",
    )
    parser.add_argument("text", help="Text to inspect for PERSON entities")
    parser.add_argument(
        "--show-tokens",
        action="store_true",
        help="Print token/subtoken spans with their BIO labels and confidence",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    backend = CapturingNERBackend(
        TransformersNERBackend.from_pretrained(
            os.getenv("NER_MODEL", DEFAULT_NER_MODEL),
            device=os.getenv("NER_DEVICE", "cpu"),
            max_length=int(os.getenv("NER_MAX_LENGTH", "512")),
            stride=int(os.getenv("NER_STRIDE", "64")),
        )
    )
    detector = NERDetector(
        backend,
        min_confidence=float(os.getenv("NER_MIN_CONFIDENCE", "0.80")),
    )

    matches = detector.detect(args.text)
    person_matches = [match for match in matches if match.pii_type == "PERSON"]

    if args.show_tokens:
        print(f"Token predictions: {len(backend.predictions)}")
        for prediction in backend.predictions:
            token = json.dumps(
                args.text[prediction.start : prediction.end],
                ensure_ascii=False,
            )
            print(
                f"[TOKEN] {token} label={prediction.label} "
                f"start={prediction.start} end={prediction.end} "
                f"confidence={prediction.confidence:.4f}"
            )

    print(f"PERSON entities: {len(person_matches)}")
    for match in person_matches:
        value = json.dumps(match.value, ensure_ascii=False)
        print(
            f"[PERSON] {value} start={match.start} end={match.end} "
            f"confidence={match.confidence:.4f}"
        )


if __name__ == "__main__":
    main()
