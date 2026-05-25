import math

from dataclasses import dataclass


@dataclass(frozen=True)
class MosaicLayout:
    columns: int
    rows: int
    tile_width: int
    tile_height: int
    canvas_width: int
    canvas_height: int


def build_mosaic_layout(
    stream_count: int,
    *,
    max_columns: int = 4,
    max_width: int = 1440,
    max_height: int = 900,
    aspect_width: int = 16,
    aspect_height: int = 9,
) -> MosaicLayout:
    """
    Compute a compact grid layout for a multi-stream mosaic window.
    """
    if stream_count <= 0:
        raise ValueError("stream_count must be greater than zero")

    columns = min(max_columns, max(1, math.ceil(math.sqrt(stream_count))))
    rows = math.ceil(stream_count / columns)

    tile_width = max_width // columns
    tile_height = int(tile_width * aspect_height / aspect_width)
    max_tile_height = max_height // rows

    if tile_height > max_tile_height:
        tile_height = max_tile_height
        tile_width = int(tile_height * aspect_width / aspect_height)

    tile_width = max(160, tile_width)
    tile_height = max(90, tile_height)

    return MosaicLayout(
        columns=columns,
        rows=rows,
        tile_width=tile_width,
        tile_height=tile_height,
        canvas_width=tile_width * columns,
        canvas_height=tile_height * rows,
    )
