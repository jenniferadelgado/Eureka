#! /usr/bin/env python

# Eureka! Stage 3 reduction pipeline

# Proposed Steps
# --------------
# 1.  Read in all data frames and header info from Stage 2 data products DONE
# 2.  Record JD and other relevant header information DONE
# 3.  Apply light-time correction (if necessary) DONE
# 4.  Calculate trace and 1D+2D wavelength solutions (if necessary)
# 5.  Make flats, apply flat field correction (Stage 2)
# 6.  Manually mask regions DONE
# 7.  Compute difference frames OR slopes (Stage 1)
# 8.  Perform outlier rejection of BG region DONE
# 9.  Background subtraction DONE
# 10. Compute 2D drift, apply rough (integer-pixel) correction
# 11. Full-frame outlier rejection for time-series stack of NDRs
# 12. Apply sub-pixel 2D drift correction
# 13. Extract spectrum through summation DONE
# 14. Compute median frame DONE
# 15. Optimal spectral extraction DONE
# 16. Save Stage 3 data products
# 17. Produce plots DONE

import time as time_pkg
import numpy as np
import astraeus.xarrayIO as xrio
from astropy.io import fits
from tqdm import tqdm
from . import optspex
from . import plots_s3, source_pos
from . import background as bg
from . import bright2flux as b2f
from ..lib import logedit
from ..lib import readECF
from ..lib import manageevent as me
from ..lib import util
from ..lib import centerdriver
from ..lib import apphot
from . import eureka_badmask, eureka_chunkbad


def reduce(eventlabel, ecf_path=None, s2_meta=None):
    '''Reduces data images and calculates optimal spectra.

    Parameters
    ----------
    eventlabel : str
        The unique identifier for these data.
    ecf_path : str; optional
        The absolute or relative path to where ecfs are stored.
        Defaults to None which resolves to './'.
    s2_meta : eureka.lib.readECF.MetaClass; optional
        The metadata object from Eureka!'s S2 step (if running S2 and S3
        sequentially). Defaults to None.

    Returns
    -------
    meta : eureka.lib.readECF.MetaClass
        The metadata object with attributes added by S3.

    Notes
    -----
    History:

    - May 2021 Kevin Stevenson
        Initial version
    - October 2021 Taylor Bell
        Updated to allow for inputs from S2
    '''

    # Load Eureka! control file and store values in Event object
    ecffile = 'S3_' + eventlabel + '.ecf'
    meta = readECF.MetaClass(ecf_path, ecffile)
    meta.eventlabel = eventlabel

    if s2_meta is None:
        # Locate the old MetaClass savefile, and load new ECF into
        # that old MetaClass
        s2_meta, meta.inputdir, meta.inputdir_raw = \
            me.findevent(meta, 'S2', allowFail=True)
    else:
        # Running these stages sequentially, so can safely assume
        # the path hasn't changed
        meta.inputdir = s2_meta.outputdir
        meta.inputdir_raw = meta.inputdir[len(meta.topdir):]

    if s2_meta is None:
        # Attempt to find subdirectory containing S2 FITS files
        meta = util.find_fits(meta)
    else:
        meta = me.mergeevents(meta, s2_meta)

    # check for range of spectral apertures
    if isinstance(meta.spec_hw, list):
        meta.spec_hw_range = range(meta.spec_hw[0],
                                   meta.spec_hw[1]+meta.spec_hw[2],
                                   meta.spec_hw[2])
    else:
        meta.spec_hw_range = [meta.spec_hw]

    # check for range of background apertures
    if isinstance(meta.bg_hw, list):
        meta.bg_hw_range = range(meta.bg_hw[0],
                                 meta.bg_hw[1]+meta.bg_hw[2],
                                 meta.bg_hw[2])
    else:
        meta.bg_hw_range = [meta.bg_hw]

    # create directories to store data
    # run_s3 used to make sure we're always looking at the right run for
    # each aperture/annulus pair
    meta.run_s3 = None
    for spec_hw_val in meta.spec_hw_range:

        for bg_hw_val in meta.bg_hw_range:

            meta.eventlabel = eventlabel

            meta.run_s3 = util.makedirectory(meta, 'S3', meta.run_s3,
                                             ap=spec_hw_val, bg=bg_hw_val)

    # begin process
    for spec_hw_val in meta.spec_hw_range:
        for bg_hw_val in meta.bg_hw_range:

            t0 = time_pkg.time()

            meta.spec_hw = spec_hw_val
            meta.bg_hw = bg_hw_val

            meta.outputdir = util.pathdirectory(meta, 'S3', meta.run_s3,
                                                ap=spec_hw_val, bg=bg_hw_val)

            event_ap_bg = (meta.eventlabel+"_ap"+str(spec_hw_val)+'_bg' +
                           str(bg_hw_val))

            # Open new log file
            meta.s3_logname = meta.outputdir + 'S3_' + event_ap_bg + ".log"
            if s2_meta is not None:
                log = logedit.Logedit(meta.s3_logname, read=s2_meta.s2_logname)
            else:
                log = logedit.Logedit(meta.s3_logname)
            log.writelog("\nStarting Stage 3 Reduction\n")
            log.writelog(f"Input directory: {meta.inputdir}")
            log.writelog(f"Output directory: {meta.outputdir}")
            log.writelog(f"Using ap={spec_hw_val}, bg={bg_hw_val}")

            # Copy ecf
            log.writelog('Copying S3 control file', mute=(not meta.verbose))
            meta.copy_ecf()

            # Create list of file segments
            meta = util.readfiles(meta)
            meta.num_data_files = len(meta.segment_list)
            if meta.num_data_files == 0:
                log.writelog(f'Unable to find any "{meta.suffix}.fits" files '
                             f'in the inputdir: \n"{meta.inputdir}"!',
                             mute=True)
                raise AssertionError(f'Unable to find any "{meta.suffix}.fits"'
                                     f' files in the inputdir: \n'
                                     f'"{meta.inputdir}"!')
            else:
                log.writelog(f'\nFound {meta.num_data_files} data file(s) '
                             f'ending in {meta.suffix}.fits',
                             mute=(not meta.verbose))

            with fits.open(meta.segment_list[-1]) as hdulist:
                # Figure out which instrument we are using
                meta.inst = hdulist[0].header['INSTRUME'].lower()
            # Load instrument module
            if meta.inst == 'miri':
                from . import miri as inst
            elif meta.inst == 'nircam':
                from . import nircam as inst
            elif meta.inst == 'nirspec':
                from . import nirspec as inst
                log.writelog('WARNING: Are you using real JWST data? If so, '
                             'you should edit the flag_bg() function in '
                             'nirspec.py and look at Issue #193 on Github!')
            elif meta.inst == 'niriss':
                raise ValueError('NIRISS observations are currently '
                                 'unsupported!')
            elif meta.inst == 'wfc3':
                from . import wfc3 as inst
                meta, log = inst.preparation_step(meta, log)
            else:
                raise ValueError('Unknown instrument {}'.format(meta.inst))

            datasets = []
            # Loop over each segment
            # Only reduce the last segment/file if testing_S3 is set to
            # True in ecf
            if meta.testing_S3:
                istart = meta.num_data_files - 1
            else:
                istart = 0
            for m in range(istart, meta.num_data_files):
                # Initialize data object
                data = xrio.makeDataset()

                # Keep track if this is the first file - otherwise MIRI will
                # keep swapping x and y windows
                meta.firstFile = (m == istart and
                                  meta.spec_hw == meta.spec_hw_range[0] and
                                  meta.bg_hw == meta.bg_hw_range[0])
                # Report progress
                if meta.verbose:
                    log.writelog(f'Reading file {m + 1} of '
                                 f'{meta.num_data_files}')
                else:
                    log.writelog(f'Reading file {m + 1} of '
                                 f'{meta.num_data_files}', end='\r')

                # Read in data frame and header
                data, meta = inst.read(meta.segment_list[m], data, meta)

                # Get number of integrations and frame dimensions
                meta.n_int, meta.ny, meta.nx = data.flux.shape
                if meta.testing_S3:
                    # Only process the last 5 integrations when testing
                    meta.int_start = np.max((0, meta.n_int-5))
                else:
                    meta.int_start = 0

                ### NEW PHOTOMETRY ###
                if hdulist[0].header['CHANNEL'] == 'SHORT':
                    meta.ywindow = [4, 255]  #TODO:  MOVE TO ecf
                    meta.xwindow = [4, 2044]  #TODO:  MOVE TO ecf
                    # Trim data to subarray region of interest
                    data, meta = util.trim_phot(data, meta)

                    # Convert flux units to electrons (eg. MJy/sr -> DN -> Electrons)
                    data, meta = b2f.convert_to_e(data, meta, log)

                    # Create bad pixel mask (1 = good, 0 = bad)
                    data.submask = np.ones(data.subdata.shape)

                    # Check if arrays have NaNs
                    data.submask = util.check_nans(data.subdata, data.submask, log, name='SUBDATA')
                    data.submask = util.check_nans(data.suberr, data.submask, log, name='SUBERR')
                    data.submask = util.check_nans(data.subv0, data.submask, log, name='SUBV0')

                    data = inst.phot_arrays(data, meta)

                    photap = 80 #TODO: MOVE TO ecf
                    skyin = 130 #TODO: MOVE TO ecf
                    skyout = 160 #TODO: MOVE TO ecf

                    method = "fgc" #TODO: MOVE TO ecf
                    guess = [200, 1100] #TODO: MOVE TO ecf

                    data = inst.flag_bg_phot(data, meta)

                    for i in range(data.data.shape[0]):
                        position, extra = centerdriver.centerdriver('fgc', data.subdata[i], guess, 0, 0, 0,
                                     mask=None, uncd=None, fitbg=1, maskstar=True,
                                     expand=1.0, psf=None, psfctr=None)
                        log.writelog("Center position of Centroid for Frame {0}:\n".format(i) + str(np.transpose(position)))
                        data.y_centroid[i], data.x_centroid[i] = position
                        if method == "fgc":
                            data.sy_centroid[i] = extra[0]
                            data.sx_centroid[i] = extra[1]

                        aphot = apphot.apphot(i, meta, image=data.subdata[i],
                        ctr = (data.y_centroid[i], data.x_centroid[i]),
                        photap = photap, skyin = skyin, skyout = skyout,
                        betahw = 1, targpos = position,
                        mask = data.submask[i],
                        imerr = data.suberr[i],
                        skyfrac = 0.1, med = False,
                        expand = 1, isbeta = False,
                        nochecks = False, aperr = True, nappix = True,
                        skylev = True, skyerr = True, nskypix = True,
                        nskyideal = True, status = True, betaper = True)

                        data.aplev[i], data.aperr[i], data.nappix[i], data.skylev[i], \
                        data.skyerr[i], data.nskypix[i], data.nskyideal[i], data.status[i], data.betaper[i] = aphot

                        print(data.aperr[i])
                        print(data.status[i])

                        # TODO: MOVE PLOTS TO plots_s3.py
                        import matplotlib.pyplot as plt
                        fig, ax = plt.subplots()
                        ax.imshow(data.subdata[i], vmax=100, origin='lower')
                        ax.scatter(data.x_centroid[i], data.y_centroid[i], marker='x', s=25, c='r')
                        fig.savefig(meta.outputdir + '/figs/frame_{0}.png'.format(i), dpi=250)

                    fig, ax = plt.subplots(1,1)
                    ax.plot(range(len(data.aplev)), data.aplev)
                    fig.savefig(meta.outputdir + '/figs/lc_{0}.png'.format(m), dpi=250)

                    fig, ax = plt.subplots(1,1)
                    ax.errorbar(data.time, data.aplev, yerr=data.aperr, c='k', fmt='.')
                    fig.savefig(meta.outputdir + '/figs/new_lc_{0}.png'.format(m), dpi=250)

                    # TODO: DONT REPEAT THE SAME
                    # TODO: MAKE astraeus compatible
                    # Append results
                    if meta.firstFile:
                        aplev = data.aplev
                        aperr = data.aperr
                        x_centroid = data.x_centroid
                        y_centroid  = data.y_centroid
                        sx_centroid = data.sx_centroid
                        sy_centroid  = data.sy_centroid
                        time    = data.time
                    else:
                        aplev = np.append(aplev, data.aplev, axis=0)
                        aperr  = np.append(aperr, data.aperr, axis=0)
                        x_centroid = np.append(x_centroid, data.x_centroid, axis=0)
                        y_centroid  = np.append(y_centroid, data.y_centroid, axis=0)
                        sx_centroid = np.append(sx_centroid, data.sx_centroid, axis=0)
                        sy_centroid = np.append(sy_centroid, data.sy_centroid, axis=0)
                        time    = np.append(time, data.time, axis=0)

                    fig, ax = plt.subplots(1,1)
                    ax.scatter(time, aplev)
                    fig.savefig(meta.outputdir + '/figs/lc1_{0}.png'.format(m), dpi=250)

                    fig, ax = plt.subplots(1,1)
                    ax.errorbar(time, aplev, yerr=aperr, c='k', fmt='.')
                    fig.savefig(meta.outputdir + '/figs/new_lc1_{0}.png'.format(m), dpi=250)

                    fig, ax = plt.subplots(4,1)
                    ax[0].plot(range(len(x_centroid)), x_centroid, label='x')
                    ax[1].plot(range(len(y_centroid)), y_centroid, label='y')
                    ax[2].plot(range(len(sx_centroid)), sx_centroid, label='sx')
                    ax[3].plot(range(len(sy_centroid)), sy_centroid, label='sy')
                    plt.legend()
                    fig.savefig(meta.outputdir + '/figs/xysxsy_{0}.png'.format(m), dpi=250)
                else:
                    # Trim data to subarray region of interest
                    # Dataset object no longer contains untrimmed data
                    data, meta = util.trim(data, meta)

                    # Locate source postion
                    meta.src_ypos = source_pos.source_pos(
                        data, meta, m, header=('SRCYPOS' in data.attrs['shdr']))
                    log.writelog(f'  Source position on detector is row '
                                 f'{meta.src_ypos}.', mute=(not meta.verbose))

                    # Compute 1D wavelength solution
                    if 'wave_2d' in data:
                        data['wave_1d'] = (['x'],
                                           data.wave_2d[meta.src_ypos].values)
                        data['wave_1d'].attrs['wave_units'] = \
                            data.wave_2d.attrs['wave_units']

                    # Convert flux units to electrons
                    # (eg. MJy/sr -> DN -> Electrons)
                    data, meta = b2f.convert_to_e(data, meta, log)

                    # Compute median frame
                    data['medflux'] = (['y', 'x'], np.median(data.flux.values,
                                                             axis=0))
                    data['medflux'].attrs['flux_units'] = \
                        data.flux.attrs['flux_units']

                    # Create bad pixel mask (1 = good, 0 = bad)
                    # FINDME: Will want to use DQ array in the future
                    # to flag certain pixels
                    data['mask'] = (['time', 'y', 'x'], np.ones(data.flux.shape,
                                                                dtype=bool))

                    # Check if arrays have NaNs
                    data['mask'] = util.check_nans(data['flux'], data['mask'],
                                                   log, name='FLUX')
                    data['mask'] = util.check_nans(data['err'], data['mask'],
                                                   log, name='ERR')
                    data['mask'] = util.check_nans(data['v0'], data['mask'],
                                                   log, name='V0')

                    # Manually mask regions [colstart, colend, rowstart, rowend]
                    if hasattr(meta, 'manmask'):
                        log.writelog("  Masking manually identified bad pixels",
                                     mute=(not meta.verbose))
                        for i in range(len(meta.manmask)):
                            colstart, colend, rowstart, rowend = meta.manmask[i]
                            data['mask'][rowstart:rowend, colstart:colend] = 0

                    # Perform outlier rejection of sky background along time axis
                    log.writelog('  Performing background outlier rejection',
                                 mute=(not meta.verbose))
                    meta.bg_y2 = int(meta.src_ypos + bg_hw_val)
                    meta.bg_y1 = int(meta.src_ypos - bg_hw_val)
                    data = inst.flag_bg(data, meta)

                    data = bg.BGsubtraction(data, meta, log, meta.isplots_S3)

                    if meta.isplots_S3 >= 3:
                        log.writelog('  Creating figures for background '
                                     'subtraction', mute=(not meta.verbose))
                        iterfn = range(meta.int_start, meta.n_int)
                        if meta.verbose:
                            iterfn = tqdm(iterfn)
                        for n in iterfn:
                            # make image+background plots
                            plots_s3.image_and_background(data, meta, n, m)

                    # Calulate and correct for 2D drift
                    if hasattr(inst, 'correct_drift2D'):
                        log.writelog('  Correcting for 2D drift',
                                     mute=(not meta.verbose))
                        inst.correct_drift2D(data, meta, m)

                    # Select only aperture region
                    ap_y1 = int(meta.src_ypos-spec_hw_val)
                    ap_y2 = int(meta.src_ypos+spec_hw_val)
                    apdata = data.flux[:, ap_y1:ap_y2].values
                    aperr = data.err[:, ap_y1:ap_y2].values
                    apmask = data.mask[:, ap_y1:ap_y2].values
                    apbg = data.bg[:, ap_y1:ap_y2].values
                    apv0 = data.v0[:, ap_y1:ap_y2].values
                    # Compute median frame
                    medapdata = np.median(apdata, axis=0)

                    # Extract standard spectrum and its variance
                    data['stdspec'] = (['time', 'x'], np.sum(apdata, axis=1))
                    data['stdvar'] = (['time', 'x'], np.sum(aperr ** 2, axis=1))
                    data['stdspec'].attrs['flux_units'] = \
                        data.flux.attrs['flux_units']
                    data['stdspec'].attrs['time_units'] = \
                        data.flux.attrs['time_units']
                    data['stdvar'].attrs['flux_units'] = \
                        data.flux.attrs['flux_units']
                    data['stdvar'].attrs['time_units'] = \
                        data.flux.attrs['time_units']
                    # FINDME: stdvar >> stdspec, which is a problem

                    # Extract optimal spectrum with uncertainties
                    log.writelog("  Performing optimal spectral extraction",
                                 mute=(not meta.verbose))
                    data['optspec'] = (['time', 'x'], np.zeros(data.stdspec.shape))
                    data['opterr'] = (['time', 'x'], np.zeros(data.stdspec.shape))
                    data['optspec'].attrs['flux_units'] = \
                        data.flux.attrs['flux_units']
                    data['optspec'].attrs['time_units'] = \
                        data.flux.attrs['time_units']
                    data['opterr'].attrs['flux_units'] = \
                        data.flux.attrs['flux_units']
                    data['opterr'].attrs['time_units'] = \
                        data.flux.attrs['time_units']

                    # Already converted DN to electrons, so gain = 1 for optspex
                    gain = 1
                    intstart = data.attrs['intstart']
                    iterfn = range(meta.int_start, meta.n_int)
                    if meta.verbose:
                        iterfn = tqdm(iterfn)
                    for n in iterfn:
                        data['optspec'][n], data['opterr'][n], mask = \
                            optspex.optimize(meta, apdata[n], apmask[n], apbg[n],
                                             data.stdspec[n].values, gain, apv0[n],
                                             p5thresh=meta.p5thresh,
                                             p7thresh=meta.p7thresh,
                                             fittype=meta.fittype,
                                             window_len=meta.window_len,
                                             deg=meta.prof_deg, n=intstart+n,
                                             meddata=medapdata)

                    # Mask out NaNs and Infs
                    optspec_ma = np.ma.masked_invalid(data.optspec.values)
                    opterr_ma = np.ma.masked_invalid(data.opterr.values)
                    optmask = np.logical_or(np.ma.getmaskarray(optspec_ma),
                                            np.ma.getmaskarray(opterr_ma))
                    data['optmask'] = (['time', 'x'], optmask)
                    # data['optspec'] = np.ma.masked_where(mask, data.optspec)
                    # data['opterr'] = np.ma.masked_where(mask, data.opterr)

                    # Plot results
                    if meta.isplots_S3 >= 3:
                        log.writelog('  Creating figures for optimal spectral '
                                     'extraction', mute=(not meta.verbose))
                        iterfn = range(meta.int_start, meta.n_int)
                        if meta.verbose:
                            iterfn = tqdm(iterfn)
                        for n in iterfn:
                            # make optimal spectrum plot
                            plots_s3.optimal_spectrum(data, meta, n, m)

                    if meta.save_output:
                        # Save flux data from current segment
                        filename_xr = (meta.outputdir+'S3_'+event_ap_bg +
                                       "_FluxData_seg"+str(m+1).zfill(4)+".h5")
                        success = xrio.writeXR(filename_xr, data, verbose=False,
                                               append=False)
                        if success == 0:
                            del(data.attrs['filename'])
                            del(data.attrs['mhdr'])
                            del(data.attrs['shdr'])
                            success = xrio.writeXR(filename_xr, data,
                                                   verbose=meta.verbose,
                                                   append=False)

                    # Remove large 3D arrays from Dataset
                    del(data['flux'], data['err'], data['dq'], data['v0'],
                        data['bg'], data['mask'], data.attrs['intstart'],
                        data.attrs['intend'])

                    # Append results for future concatenation
                    datasets.append(data)

            if meta.inst == 'wfc3':
                # WFC3 needs a conclusion step to convert lists into
                # arrays before saving
                meta, log = inst.conclusion_step(meta, log)

            # Concatenate results along time axis (default)
            spec = xrio.concat(datasets)

            # Calculate total time
            total = (time_pkg.time() - t0) / 60.
            log.writelog('\nTotal time (min): ' + str(np.round(total, 2)))

            # Save Dataset object containing time-series of 1D spectra
            meta.filename_S3_SpecData = (meta.outputdir+'S3_'+event_ap_bg +
                                         "_SpecData.h5")
            success = xrio.writeXR(meta.filename_S3_SpecData, spec,
                                   verbose=True)

            # Compute MAD value
            meta.mad_s3 = util.get_mad(meta, spec.wave_1d, spec.optspec)
            log.writelog(f"Stage 3 MAD = "
                         f"{np.round(meta.mad_s3, 2).astype(int)} ppm")

            if meta.isplots_S3 >= 1:
                log.writelog('Generating figure')
                # 2D light curve without drift correction
                plots_s3.lc_nodriftcorr(meta, spec.wave_1d, spec.optspec)

            # Save results
            if meta.save_output:
                log.writelog('Saving Metadata')
                fname = meta.outputdir + 'S3_' + event_ap_bg + "_Meta_Save"
                me.saveevent(meta, fname, save=[])

            log.closelog()

    return spec, meta
