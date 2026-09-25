"""Selecao da camera pela porta fisica informada pelo libcamera."""

from __future__ import annotations


def camera_index_for_id(camera_info: list[dict], camera_id_contains: str) -> int:
    """Resolve uma unica camera pela ID, independentemente da ordem da lista."""
    matches = [
        index for index, info in enumerate(camera_info)
        if camera_id_contains in str(info.get("Id", ""))
    ]
    if len(matches) != 1:
        available = [str(info.get("Id", "<sem Id>")) for info in camera_info]
        raise RuntimeError(
            f"Camera fisica {camera_id_contains!r} indisponivel ou ambigua; "
            f"IDs detectados: {available}"
        )
    return matches[0]
