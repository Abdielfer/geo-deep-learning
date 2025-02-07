import collections
import logging
from distutils.version import LooseVersion
from pathlib import Path
from typing import List, Union, Sequence

import pyproj
from fiona._err import CPLE_OpenFailedError
from fiona.errors import DriverError
import geopandas as gpd
import numpy as np
import pystac
import rasterio
from hydra.utils import to_absolute_path
from pandas.io.common import is_url
from rasterio import MemoryFile, DatasetReader
from rasterio.plot import reshape_as_raster
from rasterio.shutil import copy as riocopy
import xml.etree.ElementTree as ET

from shapely.geometry import box, Polygon

logger = logging.getLogger(__name__)


def create_new_raster_from_base(input_raster, output_raster, write_array):
    """Function to use info from input raster to create new one.
    Args:
        input_raster: input raster path and name
        output_raster: raster name and path to be created with info from input
        write_array (optional): array to write into the new raster

    Return:
        none
    """
    src = check_rasterio_im_load(input_raster)
    if len(write_array.shape) == 2:  # 2D array
        count = 1
    elif len(write_array.shape) == 3:  # 3D array
        if write_array.shape[0] > 100:
            logging.warning(f"\nGot {write_array.shape[0]} bands. "
                            f"\nMake sure array follows rasterio's channels first convention")
            write_array = reshape_as_raster(write_array)
        count = write_array.shape[0]
    else:
        raise ValueError(f'Array with {len(write_array.shape)} dimensions cannot be written by rasterio.')

    if write_array.shape[1:] != (src.height, src.width):
        raise ValueError(f"Output array's width and height should be identical to dimensions of input reference raster")

    # Cannot write to 'VRT' driver
    driver = 'GTiff' if src.driver == 'VRT' else src.driver

    with rasterio.open(output_raster, 'w',
                       driver=driver,
                       width=src.width,
                       height=src.height,
                       count=count,
                       crs=src.crs,
                       dtype=np.uint8,
                       transform=src.transform,
                       compress='lzw') as dst:
        dst.write(write_array)


def get_key_recursive(key, config):
    """Returns a value recursively given a dictionary key that may contain multiple subkeys."""
    if not isinstance(key, list):
        key = key.split("/")  # subdict indexing split using slash
    assert key[0] in config, f"missing key '{key[0]}' in metadata dictionary: {config}"
    val = config[key[0]]
    if isinstance(val, (dict, collections.OrderedDict)):
        assert len(key) > 1, "missing keys to index metadata subdictionaries"
        return get_key_recursive(key[1:], val)
    return int(val)


def is_stac_item(path: str) -> bool:
    """Checks if an input string or object is a valid stac item"""
    if isinstance(path, pystac.Item):
        return True
    else:
        try:
            pystac.Item.from_file(str(path))
            return True
        # with .tif as url, pystac/stac_io.py/read_test_from_href() returns Exception, not HTTPError
        except Exception:
            return False


def stack_singlebands_vrt(srcs: List, band: int = 1):
    """
    Stacks multiple single-band raster into a single multiband virtual raster
    Source: https://gis.stackexchange.com/questions/392695/is-it-possible-to-build-a-vrt-file-from-multiple-files-with-rasterio
    @param srcs:
        List of paths/urls to single-band rasters
    @param band:
        Index of band from source raster to stack into multiband VRT (index starts at 1 per GDAL convention)
    @return:
        RasterDataset object containing VRT
    """
    vrt_bands = []
    for srcnum, src in enumerate(srcs, start=1):
        with check_rasterio_im_load(src) as ras, MemoryFile() as mem:
            riocopy(ras, mem.name, driver='VRT')
            vrt_xml = mem.read().decode('utf-8')
            vrt_dataset = ET.fromstring(vrt_xml)
            for bandnum, vrt_band in enumerate(vrt_dataset.iter('VRTRasterBand'), start=1):
                if bandnum == band:
                    vrt_band.set('band', str(srcnum))
                    vrt_bands.append(vrt_band)
                    vrt_dataset.remove(vrt_band)
    for vrt_band in vrt_bands:
        vrt_dataset.append(vrt_band)

    return ET.tostring(vrt_dataset).decode('UTF-8')


def subset_multiband_vrt(src: Union[str, Path], band_request: Sequence = []):
    """
    Creates a multiband virtual raster containing a subset of all available bands in a source multiband raster
    @param src:
        Path/url to a multiband raster
    @param band_request:
        Indices of bands from source raster to subset from source multiband (index starts at 1 per GDAL convention).
        Order matters, i.e. if source raster is BGR, "[3,2,1]" will create a VRT with bands as RGB
    @return:
        RasterDataset object containing VRT
    """
    if not isinstance(src, (str, Path)) and not Path(src).is_file():
        raise ValueError(f"Invalid source multiband raster.\n"
                         f"Got {src}")
    with rasterio.open(src) as ras, MemoryFile() as mem:
        riocopy(ras, mem.name, driver='VRT')
        vrt_xml = mem.read().decode('utf-8')
        vrt_dataset = ET.fromstring(vrt_xml)
        vrt_dataset_dict = {int(band.get('band')): band for band in vrt_dataset.iter("VRTRasterBand")}
        for band in vrt_dataset_dict.values():
            vrt_dataset.remove(band)

        for dest_band_idx, src_band_idx in enumerate(band_request, start=1):
            vrt_band = vrt_dataset_dict[src_band_idx]
            vrt_band.set('band', str(dest_band_idx))
            vrt_dataset.append(vrt_band)

    return ET.tostring(vrt_dataset).decode('UTF-8')


def check_rasterio_im_load(im):
    """
    Check if `im` is already loaded in; if not, load it in.
    Copied from: https://github.com/CosmiQ/solaris/blob/main/solaris/utils/core.py#L17
    """
    if isinstance(im, (str, Path)):
        if not is_url(im) and 'VRTDataset' not in str(im):
            im = to_absolute_path(str(im))
        return rasterio.open(im)
    elif isinstance(im, rasterio.DatasetReader):
        return im
    else:
        raise ValueError("{} is not an accepted image format for rasterio.".format(im))


def check_gdf_load(gdf):
    """
    Check if `gdf` is already loaded in, if not, load from geojson.
    Copied from: https://github.com/CosmiQ/solaris/blob/main/solaris/utils/core.py#L52
    """
    if isinstance(gdf, (str, Path)):
        if not is_url(gdf):
            gdf = to_absolute_path(str(gdf))
        # as of geopandas 0.6.2, using the OGR CSV driver requires some add'nal
        # kwargs to create a valid geodataframe with a geometry column. see
        # https://github.com/geopandas/geopandas/issues/1234
        if str(gdf).lower().endswith("csv"):
            return gpd.read_file(
                gdf, GEOM_POSSIBLE_NAMES="geometry", KEEP_GEOM_COLUMNS="NO"
            )
        try:
            return gpd.read_file(gdf)
        except (DriverError, CPLE_OpenFailedError):
            logging.warning(
                f"GeoDataFrame couldn't be loaded: either {gdf} isn't a valid"
                " path or it isn't a valid vector file. Returning an empty"
                " GeoDataFrame."
            )
            return gpd.GeoDataFrame()
    elif isinstance(gdf, gpd.GeoDataFrame):
        return gdf
    else:
        raise ValueError(f"{gdf} is not an accepted GeoDataFrame format.")


def check_crs(input_crs, return_rasterio=False):
    """Convert CRS to the ``pyproj.CRS`` object passed by ``solaris``."""
    if not isinstance(input_crs, pyproj.CRS) and input_crs is not None:
        out_crs = pyproj.CRS(input_crs)
    else:
        out_crs = input_crs

    if return_rasterio:
        if LooseVersion(rasterio.__gdal_version__) >= LooseVersion("3.0.0"):
            out_crs = rasterio.crs.CRS.from_wkt(out_crs.to_wkt())
        else:
            out_crs = rasterio.crs.CRS.from_wkt(out_crs.to_wkt("WKT1_GDAL"))

    return out_crs


def bounds_riodataset(raster: DatasetReader) -> box:
    """Returns bounds of a rasterio DatasetReader as shapely box instance"""
    return box(*list(raster.bounds))


def bounds_gdf(gdf: gpd.GeoDataFrame) -> box:
    """Returns bounds of a GeoDataFrame as shapely box instance"""
    if gdf.empty:
        return Polygon()
    gdf_bounds = gdf.total_bounds
    gdf_bounds_box = box(*gdf_bounds.tolist())
    return gdf_bounds_box


def overlap_poly1_rto_poly2(polygon1: Polygon, polygon2: Polygon) -> float:
    """Calculate intersection of extents from polygon 1 and 2 over extent of a polygon 2"""
    intersection = polygon1.intersection(polygon2).area
    return intersection / (polygon2.area + 1e-30)


def multi2poly(returned_vector_pred, layer_name=None):
    """
    Convert shapely multipolygon to polygon. If fail return a logging error.
    This function will read an PATH string create an geodataframe and explode
    all multipolygon to polygon and save the geodataframe at the same PATH.
    Side note, if you use this function without a layer name, be sure that the
    GPKG dont have a layer otherwise a new layer will be created with the 
    resulting Polygon.
    Args:
        returned_vector_pred: string, geopackage PATH where the post-processing
                              results are saved
        layer_name (optional): string, the name of layer to look into for multipolygon, the name
                    represente the classes post-processed. Default None.
                    
    Return:
        none
    """
    try: # Try to convert multipolygon to polygon
        df = gpd.read_file(returned_vector_pred, layer=layer_name)
        if 'MultiPolygon' in df['geometry'].geom_type.values:
            logging.info("\nConverting multiPolygon to Polygon...")
            gdf_exploded = df.explode(index_parts=True, ignore_index=True)
            gdf_exploded.to_file(returned_vector_pred, layer=layer_name) # overwrite the layer readed
    except Exception as e:
        logging.error(f"\nSomething went wrong during the conversion of Polygon. \nError {type(e)}: {e}")
        