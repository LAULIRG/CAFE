import numpy as np 
import copy
import matplotlib.pyplot as plt 
from matplotlib.backends.backend_pdf import PdfPages
from specutils import Spectrum1D, SpectrumList
from astropy.nddata import StdDevUncertainty
import lmfit as lm # https://dx.doi.org/10.5281/zenodo.11813
import time, datetime
import warnings
import astropy.units as u
from astropy import constants as const
from astropy.stats import mad_std
import pandas as pd

import cafe_io
from cafe_io import *
cafeio = cafe_io()
import cafe_lib
from cafe_lib import *
import cafe_helper
from cafe_helper import *

import asdf
from asdf import AsdfFile
from astropy.io import fits 

import astropy
import pdb, ipdb


def cafe_grinder(self, params, wave, flux, flux_unc, weight):

    ### Read in global fit settings
    ftol = self.inopts['FIT OPTIONS']['FTOL']
    nproc = self.inopts['FIT OPTIONS']['NPROC']
    
    acceptFit = False
    ### Limit the number of times the fit can be rerun to avoid infinite loop
    niter = 1
    show = False

    ini_params = copy.copy(params) # Initial parameters
    
    while acceptFit is False:
        
        start = time.time()
        print('Iteration '+str(niter)+'/'+str(self.inopts['FIT OPTIONS']['MAX_LOOPS'])+'(max):',datetime.datetime.now(),'-----------------')
        
        #method = 'leastsq'
        method = 'least_squares' #'nelder' if len(wave) >= 10000 else 'least_squares'
        fitter = lm.Minimizer(chisquare, params, fcn_args=(wave, flux, flux_unc, weight, self.cont_profs, show))
        ### Note that which fitting method is faster here is pretty uncertain, changes by target
        result = fitter.minimize(method=method, ftol=ftol) #, method='leastsq')
        #print(lm.fit_report(result))
        
        if result.success:
            ### Print to terminal and log file
            print(result.success, 'in', result.nfev, 'steps')
            end = time.time()
            print(np.round(end-start, 2), 'seconds elapsed')
            #logFile.write(str(result.success) +  ' in ' +  str(result.nfev) + ' iterations\n')
            #logFile.write(str(np.round(end-start1,2)) + ' seconds elapsed\n')
            
            old_params = copy.copy(params) # Parameters of the previous iteration
            fit_params = copy.copy(result.params) # parameters of the current iteration that will not be changed
            params = result.params # Both params AND result.params will be modified by check_fit_parameters

            # Do checks on parameters and rerun fit if no errors are returned
            if self.inopts['FIT OPTIONS']['FIT_CHK'] and niter < self.inopts['FIT OPTIONS']['MAX_LOOPS']:
                acceptFit = check_fit_pars(self, wave, flux_unc, fit_params, params, old_params, result.errorbars)
                
                if acceptFit is False:
                    print('Rerunning fit')
                    #logFile.write('Rerunning fit\n')
                    niter+=1
                    
                    # Perturbe the values of parameters that are scaling parameters
                    for par in params.keys():
                        if params[par].vary == True:
                            parnames = par.split('_')
                            if parnames[-1] == 'Peak' \
                               or parnames[-1] == 'FLX' or parnames[-1] == 'TMP' \
                               or parnames[-1] == 'TAU' or parnames[-1] == 'RAT':
                                params[par].value *= 1.01
            else:
                print('Hit maximum number of refitting loops without LMFIT returning errors. Continuing to next spaxel (if any left).')
                acceptFit = True
                
    #print(lm.fit_report(result))
    if niter < self.inopts['FIT OPTIONS']['MAX_LOOPS']: print('Successful fit ----------------------------------------------------')

    return result




class cubemod:

    def __init__(self, cafe_dir='../CAFE/'):

        self.cafe_dir = cafe_dir


    def read_parcube_file(self, file_name, file_dir='output/'):

        if file_dir == 'output/': file_dir = self.cafe_dir + file_dir
        parcube = fits.open(file_dir+file_name)
        parcube.info()
        self.parcube = parcube
        #parcube.close()



    def read_cube(self, file_name, file_dir='input/data/', extract='Flux_st', trim=True, keep_next=False, z=None):

        if file_dir == 'input/data/': file_dir = self.cafe_dir + file_dir

        try:
            cube = cafeio.read_cretacube(file_dir+file_name, extract)
        except:
            raise IOError('Could not open fits file')
        else:
            if cube.cube['FLUX'].header['CUNIT3'] != 'um':
                raise ValueError("The cube wavelength units are not micron")
        
        
        self.file_name = file_name #cube.cube.filename().split('/')[-1]
        self.extract = extract
        
        # Remove the overlapping wavelengths between the spectral modules
        val_inds = trim_overlapping(cube.bandnames, keep_next) if trim == True else np.full(len(cube.waves),True)
        waves = cube.waves[val_inds]
        fluxes = cube.fluxes[val_inds,:,:]
        flux_uncs = cube.flux_uncs[val_inds,:,:]
        masks = cube.masks[val_inds,:,:]
        bandnames = cube.bandnames[val_inds]
        header = cube.header
        
        # Warning if z=0
        if z == 0.0: print('WARNING: No redshfit provided. Assuming object is already in rest-frame (z=0).')        
        
        self.z = z
        self.waves = waves / (1+z)
        self.fluxes = fluxes / (1+z)
        self.flux_uncs = flux_uncs / (1+z)
        self.masks = masks
        self.bandnames = bandnames
        self.header = header
        self.nx, self.ny, self.nz = cube.nx, cube.ny, cube.nz
        self.cube = cube
        
    
    
    def get_fit_sequence(self):
        
        # Create SNR image based on the first ten channels of the cube
        snr_image = np.nansum(self.fluxes[0:10,:,:], axis=0) #/ np.sqrt(np.sum(self.flux_uncs[0:11,:,:]**2, axis=0))
        
        # Store the 2D indices of the SNR image ranked from max to min SNR
        snr_ind_seq = np.unravel_index(np.flip(np.argsort(snr_image, axis=None)), snr_image.shape)
        
        # Initialize dictionary containing a matrix that will store, in each pixel, the indices of the spaxel
        # to be used for the parameter initialization, as well as a tracking image used to know what spaxels
        # have been already "fitted" in previous steps
        param_ind_seq = {'parini_spx_ind': np.full((2,)+snr_image.shape, np.nan), 'track': np.full(snr_image.shape, False)}
        
        x, y = np.meshgrid(np.arange(snr_image.shape[1]), np.arange(snr_image.shape[0]))
        
        # For each spaxel
        for snr_ind in zip(snr_ind_seq[0], snr_ind_seq[1]): # (y,x)
            # First spaxel
            if snr_ind == (snr_ind_seq[0][0], snr_ind_seq[1][0]):
                param_ind_seq['parini_spx_ind'][:,snr_ind[0],snr_ind[1]] = snr_ind
                param_ind_seq['track'][snr_ind] = True
            else:
                # Mask with closest neighbors
                neighbors = np.sqrt((x-snr_ind[1])**2 + (y-snr_ind[0])**2) <= 1.5
                # Mask with closest neighbors that have been already fitted
                fitted_neighbor_inds = np.logical_and(param_ind_seq['track'], neighbors)
                # If there is any
                if fitted_neighbor_inds.any():
                    # Chose the one with the highest SNR
                    max_snr_fitted_neighbor_ind = np.where(snr_image == np.nanmax(snr_image[fitted_neighbor_inds]))
                else:
                    # Chose the first, highest SNR spaxel in the image
                    max_snr_fitted_neighbor_ind = (np.array([snr_ind_seq[0][0]]), np.array([snr_ind_seq[1][0]]))
                    ## Assign its own index
                    #max_snr_fitted_neighbor_ind = (np.array([snr_ind[0]]), np.array([snr_ind[1]]))
                # Assign the indices to the sequence
                param_ind_seq['parini_spx_ind'][:,snr_ind[0],snr_ind[1]] = np.concatenate(max_snr_fitted_neighbor_ind)
                param_ind_seq['track'][snr_ind] = True
                
        # Returns the indices of the SNR sorted image, and the initiaziation indices of each spaxel
        return snr_ind_seq, param_ind_seq['parini_spx_ind'].astype(int) # (y,x)
    
    
    
    def fit_cube(self,
                 inparfile,
                 optfile,
                 fit_pattern = 'default',
                 cont_profs = None,
                 ):
        """
        Output result from lmfit
        """
        
        cube = self.cube # cube is in observed-frame and not trimmed
        #cube_flux = self.fluxes # (z,y,x) = (lambda,dec,RA) in rest-frame
        #cube_flux_unc = self.flux_uncs # (z,y,x) = (lambda,dec,RA) in rest-frame
        
        print('Generating fitting sequence')
        snr_ind_seq, param_ind_seq = self.get_fit_sequence()
        print('Highest SNR spaxel is:',snr_ind_seq[0][0],snr_ind_seq[1][0])
        
        # Convert the highest SNR spaxel to a spectrum1D
        wave, flux, flux_unc, bandname, mask = mask_spec(self,snr_ind_seq[1][0],snr_ind_seq[0][0])
        spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)
        
        # Initialize CAFE param generator for the highest SNR spaxel
        print('Generating initial/full parameter object with all potential lines')        
        param_gen = CAFE_param_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
        self.inpars = param_gen.inpars
        self.inopts = param_gen.inopts
        _, outPath = cafeio.init_paths(param_gen.inopts, cafe_path=self.cafe_dir, file_name=''.join(self.file_name.split('.')[0:-1]))

        print('Generating parameter cube with initial/full parameter object')
        # Make parameter object with all features available
        all_params = param_gen.make_parobj(get_all=True)
        # Parcube is initialized with all possible parameters
        # Then only the ones fitted will be injected in the appropiate keys
        parcube_gen = CAFE_parcube_generator(self, all_params, inparfile, optfile)
        parcube = parcube_gen.make_parcube()        

        ## Initiate CAFE profile loader
        #print('Generating continuum profiles')
        #prof_gen = CAFE_prof_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
        #
        #if cont_profs is None: # Use default run option file
        #    start = time.time()
        #    self.cont_profs = prof_gen.make_cont_profs()
        #    end = time.time()
        #    print(np.round(end-start,2), 'seconds to make continnum profiles')
        
        # ### Create logfile
        # if not self.inopts['OUTPUT FILE OPTIONS']['OVERWRITE']:
        #     logFile = open(outpath+obj+'.log', 'a')
        # else:
        #     logFile = open(outpath+obj+'.log', 'w+')        
        # self.inpars['METADATA']['OUTDIR'] = outpath
        # self.inpars['METADATA']['LOGFILE'] = outpath+obj+'.log'
        # ### FIXME - RA/DEC/Spaxel info should go here, once we have a spectrum format that uses it
                
        spax = 0
        for snr_ind in zip(snr_ind_seq[0], snr_ind_seq[1]): # (y,x)

            wave, flux, flux_unc, bandname, mask = mask_spec(self, x=snr_ind[1], y=snr_ind[0])
            weight = 1./flux_unc**2
            
            spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)

            print('Regenerating parameter object for current spaxel',np.flip(snr_ind))        
            param_gen = CAFE_param_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
            params = param_gen.make_parobj()
            
            print('Regenerating continuum profiles')
            prof_gen = CAFE_prof_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
            self.cont_profs = prof_gen.make_cont_profs()
            
            if snr_ind != (snr_ind_seq[0][0], snr_ind_seq[1][0]):
                print('Current spaxel',np.flip(snr_ind),'will be initialized with results from spaxel',
                      np.flip((param_ind_seq[0,snr_ind[0],snr_ind[1]], param_ind_seq[1,snr_ind[0],snr_ind[1]])))
                
                # We inject the common params in the parameter cube of the reference (fitted) spaxel
                # assigned for the initialization of the current spaxel to the current spaxel params
                cube_params = parcube2parobj(parcube, 
                                             param_ind_seq[1,snr_ind[0],snr_ind[1]],
                                             param_ind_seq[0,snr_ind[0],snr_ind[1]],
                                             parobj=params) # indexation is (x,y)
                
                # The params file is regenerated but with the VARY, LIMS and ARG reset based on the new VALUES injected
                params = param_gen.make_parobj(parobj_update=cube_params, get_all=True)
                
            # Fit the spectrum
            result = cafe_grinder(self, params, wave, flux, flux_unc, weight)

            # Inject the result into the parameter cube
            parcube = parobj2parcube(result.params, parcube, snr_ind[1], snr_ind[0]) # indexation is (x,y)
            
        self.parcube = parcube
        
        # Save to disk
        source_fn = ''.join(self.file_name.split('.')[0:-1])
        parcube.writeto(outPath+source_fn+'_parcube.fits', overwrite=True)
        
        return self



    def plot_cube_ini(self, x, y,
                      inparfile, 
                      optfile, 
                      cont_profs=None):
        """
        Plot the SED generated by the inital parameters
        """

        wave, flux, flux_unc, bandname, mask = mask_spec(self,x,y)
        spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)
        
        # Plot features based on inital intput parameters
        # -----------------------------------------------
        
        # Initiate CAFE param generator and make parameter file
        print('Generating continuum profiles for guess model')
        param_gen = CAFE_param_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
        params = param_gen.make_parobj()

        # Initiate CAFE profile loader and make cont_profs
        prof_gen = CAFE_prof_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
        cont_profs = prof_gen.make_cont_profs()

        # Scale continuum profiles with parameters and get spectra
        CompFluxes, CompFluxes_0, extComps, e0, tau0 = get_model_fluxes(params, wave, cont_profs, comps=True)
        
        # Get spectrum out of the feature parameters
        gauss, drude, gauss_opc = get_feat_pars(params)

        cafefig = cafeplot(wave, flux, flux_unc, CompFluxes, gauss, drude, plot_drude=True, pahext=extComps['extPAH'])
        


    def plot_cube_fit(self, x, y,
                      inparfile, 
                      optfile,
                      savefig=None):
        """
        Plot the spectrum itself. If fitPars already exists, plot the fitted results as well.
        """
        if hasattr(self, 'parcube') is False:
            raise ValueError('The spectrum is not fitted yet.')
        else:
            fitPars = parcube2parobj(self.parcube, x, y)

            wave, flux, flux_unc, bandname, mask = mask_spec(self, x, y)
            spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)

            prof_gen = CAFE_prof_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
            cont_profs = prof_gen.make_cont_profs()

            CompFluxes, CompFluxes_0, extComps, e0, tau0 = get_model_fluxes(fitPars, wave, cont_profs, comps=True)

            gauss, drude, gauss_opc = get_feat_pars(fitPars)  # fitPars consisting all the fitted parameters

            #sedfig, chiSqrFin = sedplot(wave, flux, flux_unc, CompFluxes, weights=weight, npars=result.nvarys)
            cafefig = cafeplot(wave, flux, flux_unc, CompFluxes, gauss, drude, plot_drude=True, pahext=extComps['extPAH'])

            # figs = [sedfig, cafefig]
            
            # with PdfPages(outpath+obj+'_fitplots'+tstamp+'.pdf') as pdf:
            #     for fig in figs:
            #         plt.figure(fig.number)
            #         pdf.savefig(bbox_inches='tight')

            if savefig is not None:
                cafefig.savefig(savefig)

            return cafefig



    def make_map(self,feat_name,
                 savefig=None):

        if hasattr(self, 'parcube') is False:
            raise ValueError('The spectrum is not fitted yet.')
        else:
            fitPars = parcube2parobj(self.parcube, x, y)

        #TBD


# ================================================================
class specmod:
    """ 
    CAFE model. When initialized, it contains the spec
    """
    def __init__(self, cafe_dir='../CAFE/'):
                
        self.cafe_dir = cafe_dir


    def read_parcube_file(self, file_name, file_dir='output/'):

        if file_dir == 'output/': file_dir = self.cafe_dir + file_dir
        parcube = fits.open(file_dir+file_name)
        parcube.info()
        self.parcube = parcube
        #parcube.close()


    def read_spec(self, file_name, file_dir='input/data/', extract='Flux_st', trim=True, keep_next=False, z=0.):

        if file_dir == 'input/data/': file_dir = self.cafe_dir + file_dir

        try:
            cube = cafeio.read_cretacube(file_dir+file_name, extract)
        except:
            print('Load data:',file_dir+file_name)
            hdu = fits.PrimaryHDU()
            dummy = fits.ImageHDU(np.full(1,np.nan), name='Flux')
            dummy.header['EXTNAME'] = 'FLUX'
            hdulist = fits.HDUList([hdu, dummy])
            hdulist.writeto('./dummy.fits', overwrite=True)
            cube = fits.open('./dummy.fits')
            try:
                spec = cafeio.customFITSReader(file_dir+file_name, extract) # Returns a spectrum1D
            except:
                try:
                    from astropy.table import Table
                    tab = Table.read(file_dir+file_name, format='ascii.basic', names=['wave', 'flux', 'flux_unc'])
                except:
                    raise IOError('The file is not a valid .txt (column-based) or .fits (CRETA output) file. Or maybe the data are not there.')
                else:
                    spec = Spectrum1D(spectral_axis=tab['wave']*u.micron, flux=tab['flux']*u.Jy, uncertainty=StdDevUncertainty(tab['flux_unc']), redshift=z)
                    spec.mask = np.full(len(spec.flux), 0)
                    spec.meta = {'bandname':[None]}

            else:
                cube.bandnames = spec.meta['band_name'][0]
            #finally:
            if spec.spectral_axis.unit != 'micron':
                raise ValueError("Make sure wavelength is in micron")                            
            cube.waves = spec.spectral_axis.value
            cube.fluxes = spec.flux.value
            cube.flux_uncs = spec.uncertainty.quantity.value
            cube.masks = spec.mask
            cube.nx = 1
            cube.ny = 1
            cube.nz = spec.flux.shape
            cube.header = dummy.header #spec.meta
            cube.bandnames = np.full(len(cube.waves),'UNKNOWN')
            trim = False
            cube.close()
            os.remove('./dummy.fits')
        else:
            cube.header = cube.cube['FLUX'].header
            if cube.cube['FLUX'].header['CUNIT3'] != 'um':
                raise ValueError("The cube wavelength units are not micron")


        self.file_name = file_name #cube.cube.filename().split('/')[-1]
        self.extract = extract
        
        # Remove the overlapping wavelengths between the spectral modules
        val_inds = trim_overlapping(cube.bandnames, keep_next) if trim == True else np.full(len(cube.waves),True)
        waves = cube.waves[val_inds]
        fluxes = cube.fluxes[val_inds]
        flux_uncs = cube.flux_uncs[val_inds]
        masks = cube.masks[val_inds]
        bandnames = cube.bandnames[val_inds]
        header = cube.header
        
        # Warning if z=0
        if z == 0.0: print('WARNING: No redshift provided. Assuming object is already in rest-frame (z=0).')
        
        self.z = z
        self.waves = waves / (1+z)
        self.fluxes = fluxes / (1+z)
        self.flux_uncs = flux_uncs / (1+z)   
        self.masks = masks
        self.bandnames = bandnames
        self.header = header
        self.nx, self.ny, self.nz = cube.nx, cube.ny, cube.nz
        self.cube = cube



    def fit_spec(self, 
                 inparfile,
                 optfile,
                 ini_parcube=False,
                 cont_profs=None,
                 ):
        """
        Output result from lmfit
        """

        wave, flux, flux_unc, bandname, mask = mask_spec(self)
        weight = 1./flux_unc**2
        spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)
        
        # Initiate CAFE param generator
        param_gen = CAFE_param_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
        self.inpars = param_gen.inpars
        self.inopts = param_gen.inopts
        _, outPath = cafeio.init_paths(param_gen.inopts, cafe_path=self.cafe_dir, file_name=''.join(self.file_name.split('.')[0:-1]))

        print('Generating parameter cube with initial/full parameter object')
        all_params = param_gen.make_parobj(get_all=True)
        parcube_gen = CAFE_parcube_generator(self, all_params, inparfile, optfile)
        parcube = parcube_gen.make_parcube()

        print('Generating parameter object')        
        params = param_gen.make_parobj()

        if ini_parcube is not False:
            print('The params will be set to the parameters of the parcube provided for initialization')
            ini_params = parcube2parobj(ini_parcube, parobj=params)
            params = param_gen.make_parobj(parobj_update=ini_params, get_all=True)

        # Initiate CAFE profile loader
        print('Generating continuum profiles')
        prof_gen = CAFE_prof_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)

        if cont_profs is None: # Use default run option file
            start = time.time()
            self.cont_profs = prof_gen.make_cont_profs()
            end = time.time()
            print(np.round(end-start,2), 'seconds to make continnum profiles')
            
        print('Fitting',len(params),'parameters')
        # Fit the spectrum
        result = cafe_grinder(self, params, wave, flux, flux_unc, weight)

        #print(lm.fit_report(result))
        # Inject the result into the parameter cube
        parcube = parobj2parcube(result.params, parcube)
            
        self.parcube = parcube
        
        # Save parcube to disk
        source_fn = ''.join(self.file_name.split('.')[0:-1])
        parcube.writeto(outPath+source_fn+'_parcube.fits', overwrite=True)
        # Save .asdf to disk
        self.save_asdf(inparfile, optfile, file_name=source_fn)

        return self

    

    def plot_spec_ini(self,
                      inparfile, 
                      optfile, 
                      ini_parcube=False,
                      cont_profs=None):
        """
        Plot the SED generated by the inital parameters
        """

        wave, flux, flux_unc, bandname, mask = mask_spec(self)
        spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)
 
        # Plot features based on inital intput parameters
        # -----------------------------------------------

        # Initiate CAFE param generator and make parameter file
        print('Generating continuum profiles for guess model')
        param_gen = CAFE_param_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
        params = param_gen.make_parobj()
        
        if ini_parcube is not False:
            print('The initial parameters will be set to the values from the parameter cube provided')
            ini_params = parcube2parobj(ini_parcube, parobj=params)
            params = param_gen.make_parobj(parobj_update=ini_params, get_all=True)

        # Initiate CAFE profile loader and make cont_profs
        prof_gen = CAFE_prof_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
        cont_profs = prof_gen.make_cont_profs()

        # Scale continuum profiles with parameters and get spectra
        CompFluxes, CompFluxes_0, extComps, e0, tau0 = get_model_fluxes(params, wave, cont_profs, comps=True)

        # Get spectrum out of the feature parameters
        gauss, drude, gauss_opc = get_feat_pars(params)
        
        cafefig = cafeplot(wave, flux, flux_unc, CompFluxes, gauss, drude, plot_drude=True, pahext=extComps['extPAH'])



    def plot_spec_fit(self,
                      inparfile, 
                      optfile, 
                      savefig=None):
        """
        Plot the spectrum itself. If fitPars already exists, plot the fitted results as well.
        """
        if hasattr(self, 'parcube') is False:
            raise ValueError('The spectrum is not fitted yet.')
        else:
            fitPars = parcube2parobj(self.parcube)

            wave, flux, flux_unc, bandname, mask = mask_spec(self)
            spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)

            prof_gen = CAFE_prof_generator(spec, inparfile, optfile, cafe_path=self.cafe_dir)
            cont_profs = prof_gen.make_cont_profs()

            CompFluxes, CompFluxes_0, extComps, e0, tau0 = get_model_fluxes(fitPars, wave, cont_profs, comps=True)

            gauss, drude, gauss_opc = get_feat_pars(fitPars)  # fitPars consisting all the fitted parameters
                    
            #sedfig, chiSqrFin = sedplot(wave, flux, flux_unc, CompFluxes, weights=weight, npars=result.nvarys)
            cafefig = cafeplot(wave, flux, flux_unc, CompFluxes, gauss, drude, plot_drude=True, pahext=extComps['extPAH'])

            # figs = [sedfig, cafefig]
            
            # with PdfPages(outpath+obj+'_fitplots'+tstamp+'.pdf') as pdf:
            #     for fig in figs:
            #         plt.figure(fig.number)
            #         pdf.savefig(bbox_inches='tight')

            if savefig is not None:
                cafefig.savefig(savefig)

            return cafefig



    def plot_spec(self, savefig=None):

        wave, flux, flux_unc, bandname, mask = mask_spec(self)
        spec = Spectrum1D(spectral_axis=wave*u.micron, flux=flux*u.Jy, uncertainty=StdDevUncertainty(flux_unc), redshift=self.z)
        
        fig = plt.figure()
        ax = fig.add_subplot(111)
        ax.plot(spec.spectral_axis, spec.flux, linewidth=1, color='k', alpha=0.8)
        ax.scatter(spec.spectral_axis, spec.flux, marker='o', s=8, color='k', alpha=0.7)
        ax.errorbar(spec.spectral_axis.value, spec.flux.value, yerr=spec.uncertainty.quantity.value, fmt='none', ecolor='gray', alpha=0.4)

        ax.set_xlabel('Wavelength (' + spec.spectral_axis.unit.to_string() + ')')
        ax.set_ylabel('Flux (' + spec.flux.unit.to_string() + ')')
        ax.set_xscale('log')
        ax.set_yscale('log')

        if savefig is not None:
            fig.savefig(savefig)

        return ax


    def save_result(self, asdf=True, pah_tbl=True, line_tbl=True, output_dirc=None):
        if hasattr(self, 'parcube') is False:
            raise AttributeError('The spectrum is not fitted yet. Missing fitted result - parcube.')

        fitPars = self.parcube.params
        wave = self.spec.spectral_axis.value

        if asdf is True:
            fitPars_dict = fitPars.valuesdict()
            
            # Get fitted results
            gauss, drude = get_feat_pars(fitPars)  # fitPars consisting all the fitted parameters
            CompFluxes, CompFluxes_0, extComps, e0, tau0 = get_model_fluxes(fitPars, wave, self.cont_profs, comps=True)

            # Get PAH powers (intrinsic/extinguished)
            pah_power_int = drude_int_fluxes(CompFluxes['wave'], drude)
            pah_power_ext = drude_int_fluxes(CompFluxes['wave'], drude, ext=extComps['extPAH'])

            # Quick hack for output PAH and line results
            output_gauss = {'wave':gauss[0], 'width':gauss[1], 'peak':gauss[2], 'name':gauss[3], 'strength':np.zeros(len(gauss[3]))} #  Should add integrated gauss
            output_drude = {'wave':drude[0], 'width':drude[1], 'peak':drude[2], 'name':drude[3], 'strength':pah_power_int.value}

            # Make dict to save in .asdf
            obsspec = {'wave': self.wave, 'flux': self.flux, 'flux_unc': self.flux_unc}
            cafefit = {'cafefit': {'obsspec': obsspec,
                                   'fitPars': fitPars_dict,
                                   'CompFluxes': CompFluxes,
                                   'CompFluxes_0': CompFluxes_0,
                                   'extComps': extComps,
                                   'e0': e0,
                                   'tau0': tau0,
                                   'gauss': output_gauss,
                                   'drude': output_drude
                                   }
                       }

            # Save output result to .asdf file
            target = AsdfFile(cafefit)
            if output_dirc is None:
                target.write_to('./output_cafefit.asdf')
            else:
                target.write_to(output_dirc)


    def pah_table(self, all_pah=False):
        """
        Output the table of PAH integrated powers

        Parameters
        ----------
            allpah : bool (default: False)
                If allpah is True, return measurements of ALL the 
                individual PAH features. Otherwise, return the complex 
                PAH measurements. 
        """
        fitPars = self.parcube.params

        df = self.parobj2df(fitPars)

        pah_parname = [i[0]=='d' for i in df.index]

        pah_name = list(set([n.split('_')[1] for n in df[pah_parname].index]))

        pah_wave_list = []
        pah_strength_list = []
        pah_strength_unc_list = []
        for n in pah_name:
            p = df.filter(like=n, axis=0)

            # --------------
            # Flux estimates
            # --------------
            wav = p.filter(like='Wave', axis=0).value[0] * u.micron
            
            gamma = p.filter(like='Gamma', axis=0).value[0]
            
            peak = p.filter(like='Peak', axis=0).value[0] * u.Jy

            x = np.linspace(2.5, 38, 200) * u.micron
            y = peak * gamma**2 / ((x/wav - wav/x)**2 + gamma**2)

            # integrated intensity (strength) -- in unit of W/m^2
            pah_strength = (np.pi * const.c.to('micron/s') / 2) * (peak * gamma / wav)# * 1e-26 # * u.watt/u.m**2
            
            pah_wave_list.append(wav.value)
            # Make unit to appear as W/m^2
            pah_strength_list.append(pah_strength.to(u.Watt/u.m**2).value)

            # --------------------------
            # Flux uncertainty estimates
            # --------------------------
            _wave_unc = p.filter(like='Wave', axis=0).stderr[0]
            _gamma_unc = p.filter(like='Gamma', axis=0).stderr[0]
            _peak_unc = p.filter(like='Peak', axis=0).stderr[0]

            # Only proceed if uncertainties exist
            if (_wave_unc is not None) & (_gamma_unc is not None) & (_peak_unc is not None):
                wav_unc = _wave_unc * u.micron
                gamma_unc = _gamma_unc
                peak_unc = _peak_unc * u.Jy

                g_over_w_unc = gamma / wav * np.sqrt((gamma_unc/gamma)**2 + (wav_unc/wav)**2) # uncertainty of gamma/wav
                
                pah_strength_unc = (np.pi * const.c.to('micron/s') / 2) * \
                                    (peak * gamma / wav) * np.sqrt((peak_unc/peak)**2 + (g_over_w_unc/(gamma / wav))**2)# * 1e-26# * u.watt/u.m**2

                # Make unit to appear as W/m^2
                pah_strength_unc_list.append(pah_strength_unc.to(u.Watt/u.m**2).value)
            else:
                pah_strength_unc_list.append(np.nan)

        # Define main PAH band dictionary
        mainpah_dict = {'PAH33': {'range': [3.25, 3.32]},
                        'ali34': {'range': [3.35, 3.44]},
                        'ali345': {'range': [3.45, 3.5]},
                        'PAH62': {'range': [6.2, 6.3]},
                        'PAH77_C': {'range': [7.3, 7.9]},
                        'PAH83': {'range': [8.3, 8.4]},
                        'PAH86': {'range': [8.6, 8.7]},
                        'PAH113_C': {'range': [11.2, 11.4]},
                        'PAH120': {'range': [11.9, 12.1]},
                        'PAH126_C': {'range': [12.6, 12.7]},
                        'PAH136': {'range': [13.4, 13.6]},
                        'PAH142': {'range': [14.1, 14.2]},
                        'PAH164': {'range': [16.4, 16.5]},
                        'PAH170_C': {'range': [16.4, 17.9]},
                        'PAH174': {'range': [17.35, 17.45]}
                        }

        #if output_unc is True:
        all_pah_df = pd.DataFrame({'pah_name': pah_name, 
                                   'pah_wave': pah_wave_list, 
                                   'pah_strength': pah_strength_list,
                                   'pah_strength_unc': pah_strength_unc_list,}
                                 ).sort_values('pah_wave')
        all_pah_df.set_index('pah_name', inplace=True)

        # Generate PAH complex table
        pah_complex_list = []                                                                                                                  
        for pah_wave in all_pah_df.pah_wave:
            match = False
            for mainpah_key in mainpah_dict.keys():
                if (pah_wave >= mainpah_dict[mainpah_key]['range'][0]) & (pah_wave < mainpah_dict[mainpah_key]['range'][1]):
                    pah_complex_list.append(mainpah_key)
                    match = True
                    break
            if match is False:
                pah_complex_list.append(None)
                    
        all_pah_df['pah_complex'] = pah_complex_list

        pah_complex_strength = all_pah_df.groupby('pah_complex')['pah_strength'].sum()

        # if output_unc is True:
        pah_complex_strength_unc = all_pah_df.groupby('pah_complex')['pah_strength_unc'].apply(lambda x: np.sqrt(np.sum(x**2)))

        pah_complex_df = pd.merge(pah_complex_strength, pah_complex_strength_unc, left_index=True, right_index=True)

        if all_pah is False:
            return pah_complex_df
        else:
            return all_pah_df


    def line_table(self):
        """
        Output the table of line integrated powers
        """
        fitPars = self.parcube.params

        df = self.parobj2df(fitPars)

        line_parname = [i[0]=='g' for i in df.index]

        line_name = list(set([n.split('_')[1] for n in df[line_parname].index]))

        line_wave_list = []
        line_strength_list = []
        line_strength_unc_list = []
        for n in line_name:
            p = df.filter(like=n, axis=0)

            wav = p.filter(like='Wave', axis=0).value[0] * u.micron
            wav_unc = p.filter(like='Wave', axis=0).stderr[0] * u.micron
            
            gamma = p.filter(like='Gamma', axis=0).value[0]
            gamma_unc = p.filter(like='Gamma', axis=0).stderr[0]
            
            peak = p.filter(like='Peak', axis=0).value[0] * u.Jy
            peak_unc = p.filter(like='Peak', axis=0).stderr[0] * u.Jy

            x = np.linspace(2.5, 38, 200) * u.micron
            y = peak * gamma**2 / ((x/wav - wav/x)**2 + gamma**2)

            # integrated intensity (strength) -- in unit of W/m^2
            # Gauss = 1 / np.sqrt(np.pi * np.log(2)) * Drude
            # 1 / np.sqrt(np.pi * np.log(2)) ~ 0.678
            line_strength = 1 / np.sqrt(np.pi * np.log(2)) * (np.pi * const.c.to('micron/s') / 2) * (peak * gamma / wav)# * 1e-26 # * u.watt/u.m**2
            
            g_over_w_unc = gamma / wav * np.sqrt((gamma_unc/gamma)**2 + (wav_unc/wav)**2) # uncertainty of gamma/wav
            
            line_strength_unc = 1 / np.sqrt(np.pi * np.log(2)) * (np.pi * const.c.to('micron/s') / 2) * \
                                (peak * gamma / wav) * np.sqrt((peak_unc/peak)**2 + (g_over_w_unc/(gamma / wav))**2)# * 1e-26# * u.watt/u.m**2

            line_wave_list.append(wav.value)
            # Make unit to appear as W/m^2
            line_strength_list.append(line_strength.to(u.Watt/u.m**2).value)
            line_strength_unc_list.append(line_strength_unc.to(u.Watt/u.m**2).value)

        all_line_df = pd.DataFrame({'line_name': line_name, 
                                    'line_wave': line_wave_list, 
                                    'line_strength': line_strength_list,
                                    'line_strength_unc': line_strength_unc_list,}
                                  ).sort_values('line_wave')
        all_line_df.set_index('line_name', inplace=True)

        return all_line_df


def plot_cafefit(asdf_fn):
    """ Recover the CAFE plot based on the input asdf file
        INPUT:
            asdf_fn: the asdf file that store the CAFE fitted parameters

        OUTPUT:
            A mpl axis object that can be modified for making the figure
    """
    af = asdf.open(asdf_fn)

    wave = np.asarray(af.tree['cafefit']['obsspec']['wave'])
    flux = np.asarray(af['cafefit']['obsspec']['flux'])
    flux_unc = np.asarray(af['cafefit']['obsspec']['flux_unc'])

    comps = af['cafefit']['CompFluxes']
    extPAH = af['cafefit']['extComps']['extPAH']
    g = af['cafefit']['gauss']
    d = af['cafefit']['drude']

    gauss = [g['wave'], g['width'], g['peak']]
    drude = [d['wave'], d['width'], d['peak']]
    (cafefig, ax1, ax2) = pycafe_lib.irsplot(wave, flux, flux_unc, comps, gauss, drude, plot_drude=True, pahext=extPAH)

    return (cafefig, ax1, ax2)
