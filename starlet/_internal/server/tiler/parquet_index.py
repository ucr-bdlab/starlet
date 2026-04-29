"""Filename-based spatial index for GeoParquet tiles.

Parquet tiles are named ``tile_XXXXXX__minx_miny_maxx_maxy.parquet``.  The
bounding box is extracted from the filename to enable fast MBR intersection
filtering without reading file metadata.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

import geopandas as gpd

logger = logging.getLogger(__name__)


def parse_parquet_bbox(fname: str) -> Optional[Tuple[float, float, float, float]]:
    """Parse bounding box from a parquet tile filename.

    Expected format: ``tile_XXXXXX__minx_miny_maxx_maxy.parquet``
    where coordinates are encoded as ``int_decimal`` pairs (e.g. ``-97_123``
    becomes ``-97.123``).

    Returns (minx, miny, maxx, maxy) or None if parsing fails.
    """
    try:
        base = fname.replace(".parquet", "")
        parts = base.split("__")
        if len(parts) != 2:
            return None
        coord_parts = parts[1].split("_")
        nums: list[float] = []
        temp: list[str] = []
        for p in coord_parts:
            temp.append(p)
            if len(temp) == 2:
                nums.append(float(temp[0] + "." + temp[1]))
                temp = []
        if len(nums) != 4:
            return None
        return (nums[0], nums[1], nums[2], nums[3])
    except (ValueError, IndexError):
        return None


def intersects_bbox(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    """Check if two (minx, miny, maxx, maxy) bounding boxes intersect."""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


class ParquetIndex:
    """Spatial index that parses bounding boxes from Parquet filenames.

    File list and bboxes are parsed once at construction time and cached
    in memory so that ``find_intersecting_files`` never hits the filesystem.
    """

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)
        self._entries: List[Tuple[Path, Tuple[float, float, float, float]]] = []
        if self.folder.exists():
            for pf in self.folder.glob("*.parquet"):
                bbox = parse_parquet_bbox(pf.name)
                if bbox is not None:
                    self._entries.append((pf, bbox))
        logger.debug("ParquetIndex: cached %d entries from %s", len(self._entries), self.folder)

    def find_intersecting_files(self, bbox_4326: Tuple[float, float, float, float]) -> List[Path]:
        return [pf for pf, pbbox in self._entries if intersects_bbox(pbbox, bbox_4326)]

    @staticmethod
    def load_and_reproject(path: Path) -> gpd.GeoDataFrame:
        gdf = gpd.read_parquet(path)
        if gdf.crs is None:
            gdf = gdf.set_crs(4326)
        if gdf.crs.to_epsg() != 3857:
            gdf = gdf.to_crs(3857)
        return gdf
