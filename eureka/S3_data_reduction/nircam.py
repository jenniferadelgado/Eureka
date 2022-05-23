# NIRCam specific rountines go here
# import numpy as np
from astropy.io import fits
import astraeus.xarrayIO as xrio
from . import sigrej, background
import numpy as np


def read(filename, data, meta):
    '''Reads single FITS file from JWST's NIRCam instrument.

    Parameters
    ----------
    filename : str
        Single filename to read.
    data : Xarray Dataset
        The Dataset object in which the fits data will stored.
    meta : eureka.lib.readECF.MetaClass
        The metadata object.

    Returns
    -------
    data : Xarray Dataset
        The updated Dataset object with the fits data stored inside.
    meta : eureka.lib.readECF.MetaClass
        The updated metadata object.

    Notes
    -----
    History:

    - November 2012 Kevin Stevenson
        Initial version
    - May 2021 KBS
        Updated for NIRCam
    - July 2021
        Moved bjdtdb into here
    - Apr 20, 2022 Kevin Stevenson
        Convert to using Xarray Dataset
    '''
    hdulist = fits.open(filename)

    # Load master and science headers
    data.attrs['filename'] = filename
    data.attrs['mhdr'] = hdulist[0].header
    data.attrs['shdr'] = hdulist['SCI', 1].header
    data.attrs['intstart'] = data.attrs['mhdr']['INTSTART']
    data.attrs['intend'] = data.attrs['mhdr']['INTEND']

    sci = hdulist['SCI', 1].data
    err = hdulist['ERR', 1].data
    dq = hdulist['DQ', 1].data
    v0 = hdulist['VAR_RNOISE', 1].data
    if hdulist[0].header['CHANNEL'] == 'LONG':
        wave_2d = hdulist['WAVELENGTH', 1].data
    elif hdulist[0].header['CHANNEL'] == 'SHORT' and hdulist[0].header['FILTER'] == 'F210M':
        data.wave = np.ones_like(sci[0]) * 2.1e4  # should have shape (#ypix, #xpix)
    int_times = hdulist['INT_TIMES', 1].data[data.attrs['intstart']-1:
                                             data.attrs['intend']]

    # Record integration mid-times in BJD_TDB
    time = int_times['int_mid_BJD_TDB']

    # Record units
    flux_units = data.attrs['shdr']['BUNIT']
    time_units = 'BJD_TDB'
    wave_units = 'microns'

    data['flux'] = xrio.makeFluxLikeDA(sci, time, flux_units, time_units,
                                       name='flux')
    data['err'] = xrio.makeFluxLikeDA(err, time, flux_units, time_units,
                                      name='err')
    data['dq'] = xrio.makeFluxLikeDA(dq, time, "None", time_units,
                                     name='dq')
    data['v0'] = xrio.makeFluxLikeDA(v0, time, flux_units, time_units,
                                     name='v0')
    data['wave_2d'] = (['y', 'x'], wave_2d)
    data['wave_2d'].attrs['wave_units'] = wave_units

    return data, meta


def phot_arrays(data, meta):

    data.x_centroid = np.zeros(meta.n_int)
    data.y_centroid = np.zeros(meta.n_int)
    data.sx_centroid = np.zeros(meta.n_int)
    data.sy_centroid = np.zeros(meta.n_int)

    data.aplev = np.zeros(meta.n_int)  # aperture flux
    data.aperr = np.zeros(meta.n_int)  # aperture error
    data.nappix = np.zeros(meta.n_int)  # number of aperture  pixels
    data.skylev = np.zeros(meta.n_int)  # background sky flux level
    data.skyerr = np.zeros(meta.n_int)  # sky error
    data.nskypix = np.zeros(meta.n_int)  # number of sky pixels
    data.nskyideal = np.zeros(meta.n_int)  # ideal number of sky pixels
    data.status = np.zeros(meta.n_int)  # apphot return status
    data.good = np.zeros(meta.n_int)  # good flag
    data.betaper = np.zeros(meta.n_int)  # beta aperture

    return data


def flag_bg(data, meta):
    '''Outlier rejection of sky background along time axis.

    Parameters
    ----------
    data : Xarray Dataset
        The Dataset object in which the fits data will stored.
    meta : eureka.lib.readECF.MetaClass
        The metadata object.

    Returns
    -------
    data : Xarray Dataset
        The updated Dataset object with outlier background pixels flagged.
    '''
    y1, y2, bg_thresh = meta.bg_y1, meta.bg_y2, meta.bg_thresh

    bgdata1 = data.flux[:, :y1]
    bgmask1 = data.mask[:, :y1]
    bgdata2 = data.flux[:, y2:]
    bgmask2 = data.mask[:, y2:]
    # bgerr1 = np.median(data.err[:, :y1])
    # bgerr2 = np.median(data.err[:, y2:])
    # estsig1 = [bgerr1 for j in range(len(bg_thresh))]
    # estsig2 = [bgerr2 for j in range(len(bg_thresh))]
    # FINDME: KBS removed estsig from inputs to speed up outlier detection.
    # Need to test performance with and without estsig on real data.
    data['mask'][:, :y1] = sigrej.sigrej(bgdata1, bg_thresh, bgmask1)  # ,
    #                                      estsig1)
    data['mask'][:, y2:] = sigrej.sigrej(bgdata2, bg_thresh, bgmask2)  # ,
    #                                     estsig2)

    return data


def fit_bg(dataim, datamask, n, meta, isplots=0):
    """Fit for a non-uniform background.

    Parameters
    ----------
    dataim : ndarray (2D)
        The 2D image array.
    datamask : ndarray (2D)
        An array of which data should be masked.
    n : int
        The current integration.
    meta : eureka.lib.readECF.MetaClass
        The metadata object.
    isplots : int; optional
        The plotting verbosity, by default 0.

    Returns
    -------
    bg : ndarray (2D)
        The fitted background level.
    mask : ndarray (2D)
        The updated mask after background subtraction.
    n : int
        The current integration number.
    """
    bg, mask = background.fitbg(dataim, meta, datamask, meta.bg_y1,
                                meta.bg_y2, deg=meta.bg_deg,
                                threshold=meta.p3thresh, isrotate=2,
                                isplots=isplots)

    return bg, mask, n


def flag_bg_phot(data, meta):
    '''Outlier rejection of sky background along time axis.

    Parameters
    ----------
    data:   DataClass
        The data object in which the fits data will stored
    meta:   MetaClass
        The metadata object

    Returns
    -------
    data:   DataClass
        The updated data object with outlier background pixels flagged.
    '''
    bg_thresh = meta.bg_thresh

    data1 = data.subdata
    mask1 = data.submask
    err1 = np.median(data.suberr[:, :])
    estsig1 = [err1 for j in range(len(bg_thresh))]

    data.submask = sigrej.sigrej(data1, bg_thresh, mask1, estsig1)

    npixels = np.prod(data.subdata.shape)
    print('npixels:', npixels)
    outliers = npixels - np.sum(data.submask)
    print('outliers:', outliers)

    return data

