"""rio-tiler.reader: low level reader."""

import math
import warnings
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy
from affine import Affine
from rasterio import windows
from rasterio.crs import CRS
from rasterio.enums import ColorInterp, Resampling
from rasterio.io import DatasetReader, DatasetWriter
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform as transform_coords
from rasterio.warp import transform_bounds

from .constants import WGS84_CRS
from .errors import AlphaBandWarning, PointOutsideBounds, TileOutsideBounds
from .types import BBox, DataMaskType, Indexes, NoData
from .utils import _requested_tile_aligned_with_internal_tile as is_aligned
from .utils import get_vrt_transform, has_alpha_band, non_alpha_indexes


def read(
    src_dst: Union[DatasetReader, DatasetWriter, WarpedVRT],
    height: Optional[int] = None,
    width: Optional[int] = None,
    indexes: Optional[Indexes] = None,
    window: Optional[windows.Window] = None,
    force_binary_mask: bool = True,
    nodata: Optional[NoData] = None,
    unscale: bool = False,
    resampling_method: Resampling = "nearest",
    vrt_options: Optional[Dict] = None,
    post_process: Optional[
        Callable[[numpy.ndarray, numpy.ndarray], DataMaskType]
    ] = None,
) -> DataMaskType:
    """Low level read function.

    Args:
        src_dst (rasterio.io.DatasetReader or rasterio.io.DatasetWriter or rasterio.vrt.WarpedVRT): Rasterio dataset.
        height (int, optional): Output height of the array.
        width (int, optional): Output width of the array.
        indexes (sequence of int or int, optional): Band indexes.
        window (rasterio.windows.Window, optional): Window to read.
        force_binary_mask (bool, optional): Cast returned mask to binary values (0 or 255). Defaults to `True`.
        nodata (int or float, optional): Overwrite dataset internal nodata value.
        unscale (bool, optional): Apply 'scales' and 'offsets' on output data value. Defaults to `False`.
        resampling_method (rasterio.enums.Resampling, optional): Rasterio's resampling algorithm. Defaults to `nearest`.
        vrt_options (dict, optional): Options to be passed to the rasterio.warp.WarpedVRT class.
        post_process (callable, optional): Function to apply on output data and mask values.

    Returns:
        tuple: Data (numpy.ndarray) and Mask (numpy.ndarray) values.

    """
    if isinstance(indexes, int):
        indexes = (indexes,)

    vrt_params = dict(add_alpha=True, resampling=Resampling[resampling_method])
    nodata = nodata if nodata is not None else src_dst.nodata
    if nodata is not None:
        vrt_params.update(dict(nodata=nodata, add_alpha=False, src_nodata=nodata))

    if has_alpha_band(src_dst):
        vrt_params.update(dict(add_alpha=False))

    if indexes is None:
        indexes = non_alpha_indexes(src_dst)
        if indexes != src_dst.indexes:
            warnings.warn(
                "Alpha band was removed from the output data array", AlphaBandWarning
            )

    out_shape = (len(indexes), height, width) if height and width else None
    mask_out_shape = (height, width) if height and width else None
    resampling = Resampling[resampling_method]

    if vrt_options:
        vrt_params.update(vrt_options)

    with WarpedVRT(src_dst, **vrt_params) as vrt:
        if ColorInterp.alpha in vrt.colorinterp:
            idx = vrt.colorinterp.index(ColorInterp.alpha) + 1
            indexes = tuple(indexes) + (idx,)
            if out_shape:
                out_shape = (len(indexes), height, width)

            data = vrt.read(
                indexes=indexes,
                window=window,
                out_shape=out_shape,
                resampling=resampling,
            )
            data, mask = data[0:-1], data[-1].astype("uint8")
        else:
            data = vrt.read(
                indexes=indexes,
                window=window,
                out_shape=out_shape,
                resampling=resampling,
            )
            mask = vrt.dataset_mask(
                window=window,
                out_shape=mask_out_shape,
                resampling=resampling,
            )

        if force_binary_mask:
            mask = numpy.where(mask != 0, numpy.uint8(255), numpy.uint8(0))

        if unscale:
            data = data.astype("float32", casting="unsafe")
            numpy.multiply(data, vrt.scales[0], out=data, casting="unsafe")
            numpy.add(data, vrt.offsets[0], out=data, casting="unsafe")

    if post_process:
        data, mask = post_process(data, mask)

    return data, mask


def part(
    src_dst: Union[DatasetReader, DatasetWriter, WarpedVRT],
    bounds: BBox,
    height: Optional[int] = None,
    width: Optional[int] = None,
    padding: int = 0,
    dst_crs: Optional[CRS] = None,
    bounds_crs: Optional[CRS] = None,
    minimum_overlap: Optional[float] = None,
    vrt_options: Optional[Dict] = None,
    max_size: Optional[int] = None,
    **kwargs: Any,
) -> DataMaskType:
    """Read part of a dataset.

    Args:
        src_dst (rasterio.io.DatasetReader or rasterio.io.DatasetWriter or rasterio.vrt.WarpedVRT): Rasterio dataset.
        bounds (tuple): Output bounds (left, bottom, right, top). By default the coordinates are considered to be in either the dataset CRS or in the `dst_crs` if set. Use `bounds_crs` to set a specific CRS.
        height (int, optional): Output height of the array.
        width (int, optional): Output width of the array.
        padding (int, optional): Padding to apply to each edge of the tile when retrieving data to assist in reducing resampling artefacts along edges. Defaults to `0`.
        dst_crs (rasterio.crs.CRS, optional): Target coordinate reference system.
        bounds_crs (rasterio.crs.CRS, optional): Overwrite bounds Coordinate Reference System.
        minimum_overlap (float, optional): Minimum % overlap for which to raise an error with dataset not covering enough of the tile.
        vrt_options (dict, optional): Options to be passed to the rasterio.warp.WarpedVRT class.
        max_size (int, optional): Limit output size array if not width and height.
        kwargs (optional): Additional options to forward to `rio_tiler.reader.read`.

    Returns:
        tuple: Data (numpy.ndarray) and Mask (numpy.ndarray) values.

    """
    if not dst_crs:
        dst_crs = src_dst.crs

    if max_size and width and height:
        warnings.warn(
            "'max_size' will be ignored with with 'height' and 'width' set.",
            UserWarning,
        )

    if bounds_crs:
        bounds = transform_bounds(bounds_crs, dst_crs, *bounds, densify_pts=21)

    if minimum_overlap:
        src_bounds = transform_bounds(
            src_dst.crs, dst_crs, *src_dst.bounds, densify_pts=21
        )
        x_overlap = max(
            0, min(src_bounds[2], bounds[2]) - max(src_bounds[0], bounds[0])
        )
        y_overlap = max(
            0, min(src_bounds[3], bounds[3]) - max(src_bounds[1], bounds[1])
        )
        cover_ratio = (x_overlap * y_overlap) / (
            (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        )

        if cover_ratio < minimum_overlap:
            raise TileOutsideBounds(
                "Dataset covers less than {:.0f}% of tile".format(cover_ratio * 100)
            )

    vrt_transform, vrt_width, vrt_height = get_vrt_transform(
        src_dst, bounds, height, width, dst_crs=dst_crs
    )

    window = windows.Window(col_off=0, row_off=0, width=vrt_width, height=vrt_height)

    if max_size and not (width and height):
        if max(vrt_width, vrt_height) > max_size:
            ratio = vrt_height / vrt_width
            if ratio > 1:
                height = max_size
                width = math.ceil(height / ratio)
            else:
                width = max_size
                height = math.ceil(width * ratio)

    out_height = height or vrt_height
    out_width = width or vrt_width
    if padding > 0 and not is_aligned(src_dst, bounds, out_height, out_width, dst_crs):
        vrt_transform = vrt_transform * Affine.translation(-padding, -padding)
        orig_vrt_height = vrt_height
        orig_vrt_width = vrt_width
        vrt_height = vrt_height + 2 * padding
        vrt_width = vrt_width + 2 * padding
        window = windows.Window(
            col_off=padding,
            row_off=padding,
            width=orig_vrt_width,
            height=orig_vrt_height,
        )

    vrt_options = vrt_options or {}
    vrt_options.update(
        {
            "crs": dst_crs,
            "transform": vrt_transform,
            "width": vrt_width,
            "height": vrt_height,
        }
    )

    return read(
        src_dst,
        out_height,
        out_width,
        window=window,
        vrt_options=vrt_options,
        **kwargs,
    )


def preview(
    src_dst: Union[DatasetReader, DatasetWriter, WarpedVRT],
    max_size: int = 1024,
    height: int = None,
    width: int = None,
    **kwargs: Any,
) -> DataMaskType:
    """Read decimated version of a dataset.

    Args:
        src_dst (rasterio.io.DatasetReader or rasterio.io.DatasetWriter or rasterio.vrt.WarpedVRT): Rasterio dataset.
        max_size (int, optional): Limit output size array if not width and height. Defaults to `1024`.
        height (int, optional): Output height of the array.
        width (int, optional): Output width of the array.
        kwargs (optional): Additional options to forward to `rio_tiler.reader.read`.

    Returns:
        tuple: Data (numpy.ndarray) and Mask (numpy.ndarray) values.

    """
    if not height and not width:
        if max(src_dst.height, src_dst.width) < max_size:
            height, width = src_dst.height, src_dst.width
        else:
            ratio = src_dst.height / src_dst.width
            if ratio > 1:
                height = max_size
                width = math.ceil(height / ratio)
            else:
                width = max_size
                height = math.ceil(width * ratio)

    return read(src_dst, height, width, **kwargs)


def point(
    src_dst: Union[DatasetReader, DatasetWriter, WarpedVRT],
    coordinates: Tuple[float, float],
    indexes: Optional[Indexes] = None,
    coord_crs: CRS = WGS84_CRS,
    masked: bool = True,
    nodata: Optional[NoData] = None,
    unscale: bool = False,
    resampling_method: Resampling = "nearest",
    vrt_options: Optional[Dict] = None,
    post_process: Optional[
        Callable[[numpy.ndarray, numpy.ndarray], DataMaskType]
    ] = None,
) -> List:
    """Read a pixel value for a point.

    Args:
        src_dst (rasterio.io.DatasetReader or rasterio.io.DatasetWriter or rasterio.vrt.WarpedVRT): Rasterio dataset.
        coordinates (tuple): Coordinates in form of (X, Y).
        indexes (sequence of int or int, optional): Band indexes.
        coord_crs (rasterio.crs.CRS, optional): Coordinate Reference System of the input coords. Defaults to `epsg:4326`.
        masked (bool): Mask samples that fall outside the extent of the dataset. Defaults to `True`.
        nodata (int or float, optional): Overwrite dataset internal nodata value.
        unscale (bool, optional): Apply 'scales' and 'offsets' on output data value. Defaults to `False`.
        resampling_method (rasterio.enums.Resampling, optional): Rasterio's resampling algorithm. Defaults to `nearest`.
        vrt_options (dict, optional): Options to be passed to the rasterio.warp.WarpedVRT class.
        post_process (callable, optional): Function to apply on output data and mask values.

    Returns:
        list: Pixel value per band indexes.

    """
    if isinstance(indexes, int):
        indexes = (indexes,)

    lon, lat = transform_coords(
        coord_crs, src_dst.crs, [coordinates[0]], [coordinates[1]]
    )
    if not (
        (src_dst.bounds[0] < lon[0] < src_dst.bounds[2])
        and (src_dst.bounds[1] < lat[0] < src_dst.bounds[3])
    ):
        raise PointOutsideBounds("Point is outside dataset bounds")

    indexes = indexes if indexes is not None else src_dst.indexes

    vrt_params: Dict[str, Any] = {
        "add_alpha": True,
        "resampling": Resampling[resampling_method],
    }
    nodata = nodata if nodata is not None else src_dst.nodata
    if nodata is not None:
        vrt_params.update({"nodata": nodata, "add_alpha": False, "src_nodata": nodata})

    if has_alpha_band(src_dst):
        vrt_params.update({"add_alpha": False})

    if vrt_options:
        vrt_params.update(vrt_options)

    with WarpedVRT(src_dst, **vrt_params) as vrt_dst:
        values = list(
            vrt_dst.sample([(lon[0], lat[0])], indexes=indexes, masked=masked)
        )[0]
        point_values = values.data
        mask = values.mask * 255 if masked else numpy.zeros(point_values.shape)

        if unscale:
            point_values = point_values.astype("float32", casting="unsafe")
            numpy.multiply(
                point_values, vrt_dst.scales[0], out=point_values, casting="unsafe"
            )
            numpy.add(
                point_values, vrt_dst.offsets[0], out=point_values, casting="unsafe"
            )

    if post_process:
        point_values, _ = post_process(point_values, mask)

    return point_values.tolist()
