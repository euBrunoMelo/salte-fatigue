"""Serialize typed runtime assessments into stable observability records."""

from __future__ import annotations

from typing import Any

from runtime_models import RuntimeAssessment


def frame_log_record(result: RuntimeAssessment) -> dict[str, Any]:
    """Return raw observation fields for one frame log record.

    Example: ``frame_log_record(runtime.evaluate(observation))``.
    """
    observation = result.observation
    pose = observation.pose
    return {
        "frame_idx": observation.frame_idx,
        "timestamp_s": observation.timestamp_s,
        "face_detected": observation.face_detected,
        "ear_left": observation.left_ear,
        "ear_right": observation.right_ear,
        "pose_available": pose is not None,
        "pitch": pose.pitch if pose else None,
        "yaw": pose.yaw if pose else None,
        "roll": pose.roll if pose else None,
    }


def assessment_log_record(result: RuntimeAssessment) -> dict[str, Any]:
    """Return derived quality, fatigue, PERCLOS, and attention fields.

    Example: ``assessment_log_record(runtime.evaluate(observation))``.
    """
    quality, fatigue, attention = result.quality, result.fatigue, result.attention
    return {
        "frame_idx": result.observation.frame_idx,
        "timestamp_s": result.observation.timestamp_s,
        "quality": quality.reason,
        "left_valid": quality.left_valid,
        "right_valid": quality.right_valid,
        "pose_usable": quality.pose_usable,
        "valid_eye_count": quality.valid_eye_count,
        "fatigue_state": fatigue.state.value,
        "observation_state": fatigue.observation_state.value,
        "closure_active": fatigue.closure.active,
        "closure_ms": fatigue.closure.duration_ms,
        "closure_event": fatigue.closure.event_type.value,
        "closure_valid_eye_count": fatigue.closure.valid_eye_count,
        "using_fallback": fatigue.closure.using_fallback,
        "perclos_p80": fatigue.perclos.value,
        "perclos_coverage": fatigue.perclos.coverage,
        "perclos_valid_seconds": fatigue.perclos.valid_seconds,
        "perclos_window_seconds": fatigue.perclos.window_seconds,
        "attention_state": attention.state.value,
        "attention_duration_ms": attention.duration_ms,
        "attention_pitch_delta": attention.pitch_delta,
        "attention_yaw_delta": attention.yaw_delta,
        "attention_roll_delta": attention.roll_delta,
    }
