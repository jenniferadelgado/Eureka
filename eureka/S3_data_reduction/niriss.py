# NIRISS specific rountines go here
#
# Written by: Adina Feinstein
# Last updated by: Adina Feinstein
# Last updated date: April 16, 2022
#
####################################
import os
import itertools
import numpy as np
import ccdproc as ccdp
from astropy import units
from astropy.io import fits
import scipy.optimize as so
import matplotlib.pyplot as plt
from astropy.table import Table
from astropy.nddata import CCDData
from scipy.signal import find_peaks
from skimage.morphology import disk
from skimage import filters, feature
from scipy.ndimage import gaussian_filter

from jwst import datamodels
from jwst.pipeline import calwebb_spec2
from jwst.pipeline import calwebb_detector1

from .background import fitbg3
from .niriss_extraction import *

from ..lib import simultaneous_order_fitting as sof
from ..lib import tracing_niriss as tn

# some cute cython code
import pyximport
pyximport.install()
from . import niriss_cython


__all__ = ['read',
           'flag_bg', 'fit_bg', 'wave_NIRISS',
           'mask_method_one', 'mask_method_two',
           'box_extract', 'dirty_mask']


def read(filename, data, meta, f277_filename=None):
    """
    Reads a single FITS file from JWST's NIRISS instrument.
    This takes in the Stage 2 processed files.

    Parameters
    ----------
    filename : str
       Single filename to read. Should be a `.fits` file.
    data : object
       Data object in which the fits data will be stored.

    Returns
    -------
    data : object
       Data object now populated with all of the FITS file
       information.
    meta : astropy.table.Table
       Metadata stored in the FITS file.
    """

    assert(filename, str)

    meta.filename = filename

    hdu = fits.open(filename)
    if f277_filename is not None:
        f277= fits.open(f277_filename)
        data.f277 = f277[1].data + 0.0
        f277.close()

    # loads in all the header data
    data.filename = filename
    data.mhdr = hdu[0].header
    data.shdr = hdu['SCI',1].header

    data.intend = hdu[0].header['NINTS'] + 0.0
    data.time = np.linspace(data.mhdr['EXPSTART'], 
                              data.mhdr['EXPEND'], 
                              int(data.intend))
    meta.time_units = 'BJD_TDB'

    # loads all the data into the data object
    data.data = hdu['SCI',1].data * hdu[0].header['EFFINTTM']
    data.err  = hdu['ERR',1].data + 0.0
    data.dq   = hdu['DQ' ,1].data + 0.0

    data.var  = hdu['VAR_POISSON',1].data * hdu[0].header['EFFINTTM']**2.0
    data.v0   = hdu['VAR_RNOISE' ,1].data * hdu[0].header['EFFINTTM']**2.0

    meta.meta = hdu[-1].data

    # removes NaNs from the data & error arrays
    data.data[np.isnan(data.data)==True] = 0
    data.err[ np.isnan(data.err) ==True] = 0

    data.median = np.nanmedian(data.data, axis=0)
    hdu.close()

    return data, meta


def mask_method_one(data, meta=None, radius=1, gf=4,
                    isplots=0, save=False, inclass=False,
                    outdir=None):
    """
    There are some hard-coded numbers in here right now. The idea
    is that once we know what the real data looks like, nobody will
    have to actually call this function and we'll provide a CSV
    of a good initial guess for each order. This method uses some fun
    image processing to identify the boundaries of the orders and fits
    the edges of the first and second orders with a 4th degree polynomial.

    Parameters  
    ----------  
    data : object
    meta : object
    isplots : int, optional
       Level of plots that should be created in the S3 stage.
       This is set in the .ecf control files. Default is 0.
       This stage will plot if isplots >= 5.
    save : bool, optional
       An option to save the polynomial fits to a CSV. Default
       is True. Output table is saved under `niriss_order_guesses.csv`.

    Returns
    -------
    meta : object
    """

    tab = tn.mask_method_one(data, radius=radius, gf=gf,
                             save=save, outdir=outdir)

    if inclass==False:
        meta.tab1 = tab
        return meta
    else:
        return tab


def mask_method_two(data, meta=None, isplots=0, save=False, inclass=False,
                    outdir=None):
    """
    A second method to extract the masks for the first and
    second orders in NIRISS data. This method uses the vertical
    profile of a summed image to identify the borders of each
    order.
    
    ""
    Parameters
    -----------
    data : object
    meta : object
    isplots : int, optional
       Level of plots that should be created in the S3 stage.
       This is set in the .ecf control files. Default is 0.
       This stage will plot if isplots >= 5.
    save : bool, optional
       Has the option to save the initial guesses for the location
       of the NIRISS orders. This is set in the .ecf control files.
       Default is False.

    Returns
    -------
    meta : object
    """
    tab = tn.mask_method_two(data, save=save, outdir=outdir)

    if inclass == False:
        meta.tab2 = tab
        return meta
    else:
        return tab

def wave_NIRISS(wavefile, meta=None, inclass=False, filename=None):
    """
    Adds the 2D wavelength solutions to the meta object.
    
    Parameters
    ----------
    wavefile : str
       The name of the .FITS file with the wavelength
       solution.
    meta : object
    filename : str, optional
       The flux filename. Default is None. Needs a filename if
       the `meta` class is not provided.

    Returns
    -------
    meta : object
    """
    if meta is not None:
        rampfitting_results = datamodels.open(meta.filename)
    else:
        rampfitting_results = datamodels.open(filename)

    # Run assignwcs step on Stage 1 outputs:
    assign_wcs_results = calwebb_spec2.assign_wcs_step.AssignWcsStep.call(rampfitting_results)

    # Extract 2D wavelenght map for order 1:
    rows, columns = assign_wcs_results.data[0,:,:].shape
    wavelength_map = np.zeros([3, rows, columns])
    
    # Loops through the three orders to retrieve the wavelength maps
    for order in [1,2,3]:
        for row in tqdm(range(rows)):
            for column in range(columns):
                wavelength_map[order-1, row, column] = assign_wcs_results.meta.wcs(column, 
                                                                                   row, 
                                                                                   order)[-1]
    if inclass == False:
        meta.wavelength_order1 = wavelength_map[0] + 0.0
        meta.wavelength_order2 = wavelength_map[1] + 0.0
        meta.wavelength_order3 = wavelength_map[2] + 0.0
        return meta
    else:
        return wavelength_map[0], wavelength_map[1], wavelength_map[2]


def flag_bg(data, meta, readnoise=11, sigclip=[4,4,4], 
            box=(5,2), filter_size=(2,2), bkg_estimator=['median'], isplots=0):
    """ 
    I think this is just a wrapper for fit_bg, because I perform outlier
    flagging at the same time as the background fitting.
    """
    data, bkg, bkg_var = fit_bg(data, meta, readnoise, sigclip, 
                                bkg_estimator=bkg_estimator, box=box, 
                                filter_size=filter_size, isplots=isplots)
    data.bkg = bkg
    data.bkg_var = bkg_var
    return data


def dirty_mask(img, meta=None, boxsize1=70, boxsize2=60, booltype=True,
               return_together=True, pos1=None, pos2=None):
    """Really dirty box mask for background purposes."""
    order1 = np.zeros((boxsize1, len(img[0])))
    order2 = np.zeros((boxsize2, len(img[0])))
    mask = np.zeros(img.shape)

    if meta is not None:
        pos1 = meta.tab2['order_1'] + 0.0
        pos2 = meta.tab2['order_2'] + 0.0
    if meta is None and pos1 is None:
        return('Cannot create box mask without trace.')

    if booltype==True:
        m1, m2 = -1, -1
    else:
        m1, m2 = 1, 2
    
    for i in range(img.shape[1]):
        s,e = int(pos1[i]-boxsize1/2), int(pos1[i]+boxsize1/2)
        order1[:,i] = img[s:e,i]
        mask[s:e,i] += m1
        
        s,e = int(pos2[i]-boxsize2/2), int(pos2[i]+boxsize2/2)
        try:
            order2[:,i] = img[s:e,i]
            mask[s:e,i] += m2
        except:
            pass
        
    if booltype==True:
        mask = ~np.array(mask, dtype=bool)

    if return_together:
        return mask
    else:
        m1, m2 = np.zeros(mask.shape), np.zeros(mask.shape)
        m1[(mask==1) | (mask==3)] = 1
        m2[mask>=2] = 1
        return m1, m2


def box_extract(data, meta, boxsize1=60, boxsize2=50, bkgsub=False):
    """
    Quick & dirty box extraction to use in the optimal extraction routine.
    
    Parameters
    ----------
    data : object
    boxsize1 : int, optional
       Size of the box for the first order. Default is 60 pixels.
    boxsize2 : int, optional
       Size of the box for the second order. Default is 50 pixels.

    Returns
    -------
    spec1 : np.ndarray
       Extracted spectra for the first order.
    spec2 : np.ndarray
       Extracted spectra for the second order.
    """
    spec1 = np.zeros((data.data.shape[0], 
                      data.data.shape[2]))
    spec2 = np.zeros((data.data.shape[0],
                      data.data.shape[2]))
    var1 = np.zeros(spec1.shape)
    var2 = np.zeros(spec2.shape)

    m1,m2 = dirty_mask(data.median, meta,
                       boxsize1, boxsize2,
                       booltype=False, return_together=False)
    
    if bkgsub:
        d=data.bkg_removed+0.0
    else:
        d=data.data+0.0

    for i in range(len(d)):
        spec1[i] = np.nansum(d[i],# * m1, 
                             axis=0)
        spec2[i] = np.nansum(d[i],# * m2
                             axis=0)

        var1[i] = np.nansum(data.var[i] * m1, axis=0)
        var2[i] = np.nansum(data.var[i] * m2, axis=0)

    return spec1, spec2, var1, var2


def fit_bg(data, meta, readnoise=11, sigclip=[4,4,4], box=(5,2), filter_size=(2,2), 
           bkg_estimator=['median'], isplots=0):
    """
    Subtracts background from non-spectral regions.

    Parameters
    ----------
    data : object
    meta : object
    readnoise : float, optional
       An estimation of the readnoise of the detector.
       Default is 5.
    sigclip : list, array, optional
       A list or array of len(n_iiters) corresponding to the
       sigma-level which should be clipped in the cosmic
       ray removal routine. Default is [4,2,3].
    isplots : int, optional
       The level of output plots to display. Default is 0 
       (no plots).

    Returns
    -------
    data : object
    bkg : np.ndarray
    """
    box_mask = dirty_mask(data.median, meta, booltype=True,
                          return_together=True)
    data, bkg, bkg_var = fitbg3(data, np.array(box_mask-1, dtype=bool), 
                                readnoise, sigclip, bkg_estimator=bkg_estimator,
                                box=box, filter_size=filter_size, isplots=isplots)
    return data, bkg, bkg_var


def set_which_table(i, meta):
    """ 
    A little routine to return which table to
    use for the positions of the orders.

    Parameters
    ----------
    i : int
    meta : object

    Returns
    -------
    pos1 : np.array
       Array of locations for first order.
    pos2 : np.array
       Array of locations for second order.
    """
    if i == 2:
        pos1, pos2 = meta.tab2['order_1'], meta.tab2['order_2']
    elif i == 1:
        pos1, pos2 = meta.tab1['order_1'], meta.tab1['order_2']
    return pos1, pos2
