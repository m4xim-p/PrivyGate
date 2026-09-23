"""Optional local NER adapter with no import-time ML dependencies."""

import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.pii import PIIDetector, PIIMatch, is_known_person

logger = logging.getLogger("privygate.ner")

DEFAULT_NER_MODEL_REVISION = "924a4b1912ec6e55a4be959cab215ad8ff32a750"


@dataclass(frozen=True, slots=True)
class NERTokenPrediction:
    """One model token prediction using offsets into the original text."""

    label: str
    start: int
    end: int
    confidence: float


class NERBackend(Protocol):
    def predict(self, text: str) -> Sequence[NERTokenPrediction]: ...


class NERDetector:
    """Convert token-level PER/PERSON predictions to PIIMatch spans."""

    pii_type = "PERSON"

    def __init__(
        self,
        backend: NERBackend,
        *,
        min_confidence: float = 0.80,
        accepted_labels: Sequence[str] = ("PER", "PERSON"),
        precheck: PIIDetector | None = None,
    ) -> None:
        self._backend = backend
        self._min_confidence = min_confidence
        self._accepted_labels = frozenset(label.upper() for label in accepted_labels)
        self._precheck = precheck

    def detect(self, text: str) -> list[PIIMatch]:
        started_at = time.perf_counter()
        status = "completed"
        predictions: Sequence[NERTokenPrediction] = ()
        matches: list[PIIMatch] = []
        suspicious_person_span = False
        precheck_skipped = False
        try:
            if self._precheck is not None and self._precheck.detect(text):
                # The rule-based pre-check already found names; skip the
                # expensive NER inference for this text.
                precheck_skipped = True
                return matches
            predictions = self._backend.predict(text)
            matches, suspicious_person_span = self._person_matches(text, predictions)
            return matches
        except Exception:
            status = "error"
            raise
        finally:
            logger.info(
                "ner_inference_finished status=%s latency_ms=%.1f "
                "token_predictions=%d person_entities=%d "
                "suspicious_person_span=%s precheck_skipped=%s",
                status,
                (time.perf_counter() - started_at) * 1000,
                len(predictions),
                len(matches),
                str(suspicious_person_span).lower(),
                str(precheck_skipped).lower(),
            )

    def _person_matches(
        self,
        text: str,
        predictions: Sequence[NERTokenPrediction],
    ) -> tuple[list[PIIMatch], bool]:
        entities: list[PIIMatch] = []
        current_tokens: list[tuple[NERTokenPrediction, str]] = []
        current_end = 0
        suspicious_person_span = False

        def finish_current_run() -> None:
            nonlocal current_tokens, current_end, suspicious_person_span
            if current_tokens:
                run_start = current_tokens[0][0].start
                run_end = current_tokens[-1][0].end
                is_suspicious = _word_count(text[run_start:run_end]) > 4
                suspicious_person_span = suspicious_person_span or is_suspicious

                groups = _split_at_bio_starts(current_tokens)
                for group in groups:
                    start = group[0][0].start
                    end = group[-1][0].end
                    confidence = min(token.confidence for token, _ in group)
                    if confidence >= self._min_confidence:
                        value = text[start:end]
                        if is_known_person(value):
                            continue
                        entities.append(
                            PIIMatch(
                                pii_type=self.pii_type,
                                value=value,
                                start=start,
                                end=end,
                                confidence=confidence,
                            )
                        )
            current_tokens = []
            current_end = 0

        ordered = sorted(predictions, key=lambda item: (item.start, item.end))
        for prediction in ordered:
            prefix, entity_label = _split_bio_label(prediction.label)
            is_person = entity_label in self._accepted_labels
            valid_offset = 0 <= prediction.start < prediction.end <= len(text)
            if not is_person or not valid_offset:
                finish_current_run()
                continue

            gap = text[current_end : prediction.start] if current_tokens else ""
            continues_run = (
                bool(current_tokens)
                and prediction.start >= current_end
                and all(character.isspace() or character == "-" for character in gap)
            )
            if not continues_run:
                finish_current_run()
            current_tokens.append((prediction, prefix))
            current_end = max(current_end, prediction.end)

        finish_current_run()
        return entities, suspicious_person_span


class TransformersNERBackend:
    """Lazy-importing Hugging Face token-classification backend."""

    def __init__(
        self,
        tokenizer: Any,
        model: Any,
        torch_module: Any,
        *,
        device: str = "cpu",
        max_length: int = 512,
        stride: int = 64,
    ) -> None:
        if not getattr(tokenizer, "is_fast", False):
            raise ValueError("NER requires a fast tokenizer with offset mapping")
        self._tokenizer = tokenizer
        self._model = model
        self._torch = torch_module
        self._device = torch_module.device(device)
        self._max_length = max_length
        self._stride = stride
        self._model.to(self._device)
        self._model.eval()

    @classmethod
    def from_pretrained(
        cls,
        model_name: str,
        *,
        revision: str = DEFAULT_NER_MODEL_REVISION,
        device: str = "cpu",
        max_length: int = 512,
        stride: int = 64,
        offline: bool = True,
        model_path: str | None = None,
    ) -> "TransformersNERBackend":
        try:
            import torch
            from transformers import AutoModelForTokenClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "NER dependencies are missing; install PrivyGate with the 'ner' extra"
            ) from exc

        if offline:
            # Force loading from the local model cache only; never download from
            # the Hugging Face Hub at runtime (security.md: offline/pinned NER).
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        # Prefer a pre-downloaded local model directory (e.g. /models/ner baked
        # into the image at build time). local_files_only=True forbids any
        # network download at runtime.
        source = model_path or model_name
        local_only = offline or model_path is not None

        tokenizer = AutoTokenizer.from_pretrained(
            source,
            revision=revision,
            use_fast=True,
            local_files_only=local_only,
        )
        model = AutoModelForTokenClassification.from_pretrained(
            source,
            revision=revision,
            local_files_only=local_only,
        )
        return cls(
            tokenizer,
            model,
            torch,
            device=device,
            max_length=max_length,
            stride=stride,
        )

    def predict(self, text: str) -> list[NERTokenPrediction]:
        encoded = self._tokenizer(
            text,
            return_tensors="pt",
            return_offsets_mapping=True,
            truncation=True,
            max_length=self._max_length,
            return_overflowing_tokens=True,
            stride=self._stride,
        )
        offsets = encoded.pop("offset_mapping").tolist()
        encoded.pop("overflow_to_sample_mapping", None)
        model_inputs = {
            name: tensor.to(self._device) for name, tensor in encoded.items()
        }

        with self._torch.inference_mode():
            logits = self._model(**model_inputs).logits
            probabilities = self._torch.softmax(logits, dim=-1)
            confidences, label_ids = probabilities.max(dim=-1)

        id_to_label = self._model.config.id2label
        candidates: dict[tuple[int, int], NERTokenPrediction] = {}
        for window_offsets, window_label_ids, window_confidences in zip(
            offsets,
            label_ids.detach().cpu().tolist(),
            confidences.detach().cpu().tolist(),
            strict=True,
        ):
            for (start, end), label_id, confidence in zip(
                window_offsets,
                window_label_ids,
                window_confidences,
                strict=True,
            ):
                if start == end:
                    continue
                label = id_to_label.get(int(label_id), id_to_label.get(str(label_id)))
                if label is None:
                    raise ValueError("NER model returned an unknown label id")
                prediction = NERTokenPrediction(
                    label=str(label),
                    start=int(start),
                    end=int(end),
                    confidence=float(confidence),
                )
                key = (prediction.start, prediction.end)
                previous = candidates.get(key)
                if previous is None or prediction.confidence > previous.confidence:
                    candidates[key] = prediction

        return sorted(candidates.values(), key=lambda item: (item.start, item.end))


def _split_bio_label(label: str) -> tuple[str, str]:
    normalized = label.upper()
    if "-" not in normalized:
        return "", normalized
    prefix, entity_label = normalized.split("-", 1)
    return prefix, entity_label


def _word_count(value: str) -> int:
    return len(re.findall(r"\w+", value, flags=re.UNICODE))


def _split_at_bio_starts(
    tokens: Sequence[tuple[NERTokenPrediction, str]],
) -> list[list[tuple[NERTokenPrediction, str]]]:
    """Split only at explicit BIO starts; otherwise preserve the broad span."""

    groups: list[list[tuple[NERTokenPrediction, str]]] = []
    current: list[tuple[NERTokenPrediction, str]] = []
    for token in tokens:
        _, prefix = token
        if prefix == "B" and current:
            groups.append(current)
            current = []
        current.append(token)
    if current:
        groups.append(current)
    return groups
