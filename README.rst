CAFE
====

Purpose
-------

The new Continuum And Feature Extraction (``CAFE``) is a revamped version of the ``CAFE`` software –originally developed for fitting Spitzer/IRS spectra– that has been updated and optimized to work with the new JWST IFU data. The new ``CAFE`` is composed of two main tools: (1) the ``CAFE`` Region Extraction Tool Automaton (``CRETA``) and (2) the ``CAFE`` the spectral fitting tool. ``CRETA`` performs single-position and full-grid extractions from JWST IFU datasets; that is, from pipeline-processed cubes obtained with the NIRSpec IFU and MIRI MRS instruments. The ``CAFE`` fitter uses the spectra extracted by ``CRETA`` (or spectra provided by the user) and performs a spectral decomposition of the continuum emission (stellar and/or dust), as well as of a variety of common spectral features (in emission and absorption) present in the near- and mid-IR spectra of galaxies. The full dust treatment (size and composition) performed by ``CAFE`` (see Marshall et al. 2007) allows the dust continuum model components to fit not only spectra typical of normal star-forming galaxies but also complex spectral profiles seen in more extreme, heavily dust-obscured starburst galaxies, such as luminous infrared galaxies (LIRGs), active galactic nuclei (AGN), or very luminous quasars.

The current version of ``CAFE`` (v1.0.0) only supports the ``CAFE`` spectral fitting tool. The ``CRETA`` extraction tool will be supported in subsequent versions of the code.

Documentation
-------------
Hosted by readthedocs: <https://goals-cafe.readthedocs.io/en/latest/>


Contributors
------------
* Tanio Diaz-Santos
* Thomas Lai
* Luke Finnerty
